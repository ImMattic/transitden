"""Home-game carousel: ESPN parsing, visibility rules, and the simulator.

No network anywhere — ESPN payloads are hand-built fixtures shaped like the real
scoreboard response, and the simulator is a pure function of virtual time, so
every phase can be asserted by moving a datetime rather than by waiting.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.middleware.rate_limit import _match_bucket
from app.schemas.sports import GameSlide
from app.services import sports, sports_sim

NOW = datetime(2026, 9, 10, 2, 0, tzinfo=timezone.utc)


# ── Fixtures shaped like ESPN's scoreboard ─────────────────────────────────


def espn_event(
    *,
    event_id: str = "401",
    home_name: str = "Colorado Avalanche",
    away_name: str = "Vegas Golden Knights",
    home_abbr: str = "COL",
    away_abbr: str = "VGK",
    state: str = "pre",
    short_detail: str = "7:00 PM MT",
    home_score: str = "",
    away_score: str = "",
    winner: str | None = None,
    date: str = "2026-09-10T01:00Z",
    color: str = "6f263d",
) -> dict:
    def competitor(side: str, name: str, abbr: str, score: str) -> dict:
        entry: dict = {
            "homeAway": side,
            "score": score,
            "team": {
                "displayName": name,
                "shortDisplayName": name.split()[-1],
                "abbreviation": abbr,
                "color": color if side == "home" else "333f48",
                "alternateColor": "236192",
            },
        }
        if winner is not None:
            entry["winner"] = winner == side
        return entry

    return {
        "id": event_id,
        "date": date,
        "competitions": [
            {
                "id": f"c{event_id}",
                "date": date,
                "venue": {"fullName": "Ball Arena"},
                "competitors": [
                    competitor("home", home_name, home_abbr, home_score),
                    competitor("away", away_name, away_abbr, away_score),
                ],
                "status": {"type": {"state": state, "shortDetail": short_detail}},
            }
        ],
    }


AVS = sports.TEAMS_BY_KEY["avalanche"]
ROCKIES = sports.TEAMS_BY_KEY["rockies"]


# ── Parsing ────────────────────────────────────────────────────────────────


def test_home_game_parses_into_a_slide():
    slide = sports._parse_event(espn_event(), AVS, NOW)

    assert slide is not None
    assert slide.team_key == "avalanche"
    assert slide.team_abbr == "COL"
    assert slide.opponent_abbr == "VGK"
    assert slide.state == "pre"
    assert slide.sport_emoji == "🏒"
    assert slide.venue == "Ball Arena"
    assert slide.start == datetime(2026, 9, 10, 1, 0, tzinfo=timezone.utc)


def test_away_game_is_dropped():
    """The entire premise: an away game doesn't move Denver ridership."""
    event = espn_event(home_name="Vegas Golden Knights", away_name="Colorado Avalanche")

    assert sports._parse_event(event, AVS, NOW) is None


def test_team_matched_by_name_not_abbreviation():
    """Abbreviations collide (COL is both the Avalanche and the Rockies) and
    change; the club name is what survives an expansion season."""
    event = espn_event(home_name="Colorado Rockies", home_abbr="COL")

    assert sports._parse_event(event, AVS, NOW) is None
    assert sports._parse_event(event, ROCKIES, NOW) is not None


def test_final_game_records_result_and_end_time():
    event = espn_event(
        state="post", short_detail="Final", home_score="4", away_score="2", winner="home"
    )

    slide = sports._parse_event(event, AVS, NOW)

    assert slide is not None
    assert slide.state == "post"
    assert slide.result == "win"
    assert (slide.team_score, slide.opponent_score) == (4, 2)
    # ESPN never says when a game ended, so the first poll that saw it final is
    # the end time we keep.
    assert slide.end == NOW


def test_end_time_is_pinned_to_first_sighting_not_the_latest_poll():
    event = espn_event(state="post", short_detail="Final", home_score="4", away_score="2")

    first = sports._parse_event(event, AVS, NOW)
    later = sports._parse_event(event, AVS, NOW + timedelta(minutes=30))

    assert first is not None and later is not None
    assert later.end == NOW, "the game didn't end again just because we polled again"


def test_result_falls_back_to_the_scoreline_without_a_winner_flag():
    event = espn_event(state="post", short_detail="FT", home_score="1", away_score="1")

    slide = sports._parse_event(event, AVS, NOW)

    assert slide is not None and slide.result == "draw"


def test_our_brand_colour_wins_over_espns():
    """ESPN lists whichever colour the league puts first — navy for the Broncos,
    not the orange anyone pictures, and dark for three of the six clubs. Our
    curated hex is what keeps the badges recognisable and distinct."""
    slide = sports._parse_event(espn_event(color="ABCDEF"), AVS, NOW)

    assert slide is not None and slide.color == AVS.color


def test_a_club_with_unknown_colours_defers_to_espn():
    summit = sports.TEAMS_BY_KEY["summit"]
    event = espn_event(home_name="Denver Summit FC", color="ABCDEF")

    slide = sports._parse_event(event, summit, NOW)

    assert summit.defer_to_espn_color is True
    assert slide is not None and slide.color == "ABCDEF"


def test_deferred_colour_still_falls_back_when_espn_sends_junk():
    summit = sports.TEAMS_BY_KEY["summit"]
    event = espn_event(home_name="Denver Summit FC", color="not-a-colour")

    slide = sports._parse_event(event, summit, NOW)

    assert slide is not None and slide.color == summit.color


def test_every_clubs_badge_colour_is_distinct():
    """Two clubs at home on the same day get adjacent turns in the carousel."""
    colours = [t.color.upper() for t in sports.TEAMS]

    assert len(set(colours)) == len(colours)


def test_event_without_competitions_is_skipped():
    assert sports._parse_event({"id": "1", "date": "2026-09-10T01:00Z"}, AVS, NOW) is None


def test_fetch_dates_covers_yesterday_and_today():
    """A 9pm game that ends after midnight must still be inside its post window."""
    assert sports._fetch_dates(datetime(2026, 9, 10).date()) == (
        datetime(2026, 9, 9).date(),
        datetime(2026, 9, 10).date(),
    )


# ── Visibility and ordering ────────────────────────────────────────────────


def slide(state: str, *, start: datetime = NOW, end: datetime | None = None) -> GameSlide:
    return GameSlide(
        id=f"{state}-{start.isoformat()}",
        league="NHL",
        sport_emoji="🏒",
        team_key="avalanche",
        team_abbr="COL",
        team_name="Avalanche",
        opponent_abbr="VGK",
        opponent_name="Golden Knights",
        color="6F263D",
        alt_color="236192",
        state=state,  # type: ignore[arg-type]
        start=start,
        end=end,
    )


@pytest.mark.parametrize(
    "minutes_ago,visible",
    [(5, True), (119, True), (121, False)],
)
def test_finished_games_age_out_after_the_post_window(minutes_ago, visible):
    finished = slide("post", end=NOW - timedelta(minutes=minutes_ago))

    assert sports.is_visible(finished, NOW, 120) is visible


def test_upcoming_games_never_age_out():
    assert sports.is_visible(slide("pre", start=NOW + timedelta(hours=9)), NOW, 120) is True


def test_end_time_estimated_when_the_game_was_already_final_on_first_poll():
    """Restarts lose the observed end time; a typical game length stands in."""
    orphan = slide("post", start=NOW)

    assert sports.end_time(orphan) == NOW + timedelta(minutes=AVS.typical_minutes)


def test_ordering_puts_live_first_then_upcoming_then_finished():
    live = slide("in", start=NOW - timedelta(minutes=30))
    upcoming = slide("pre", start=NOW + timedelta(hours=3))
    sooner = slide("pre", start=NOW + timedelta(hours=1))
    done = slide("post", end=NOW - timedelta(minutes=10))

    ordered = sorted([done, upcoming, live, sooner], key=sports.sort_key)

    assert [s.state for s in ordered] == ["in", "pre", "pre", "post"]
    assert ordered[1] is sooner


def test_ttl_tightens_when_a_game_is_live():
    live = sports._ttl_seconds([slide("in")], NOW)
    soon = sports._ttl_seconds([slide("pre", start=NOW + timedelta(minutes=20))], NOW)
    idle = sports._ttl_seconds([slide("pre", start=NOW + timedelta(hours=8))], NOW)

    assert live < soon < idle
    assert sports._ttl_seconds([], NOW) == idle


# ── Simulator: the virtual clock ───────────────────────────────────────────


def sim_game(**kwargs) -> sports_sim.SimGame:
    base = dict(
        team_key="avalanche",
        opponent_abbr="VGK",
        opponent_name="Golden Knights",
        lead_minutes=60.0,
        game_minutes=150.0,
        final_team_score=4,
        final_opponent_score=2,
    )
    base.update(kwargs)
    return sports_sim.SimGame(**base)  # type: ignore[arg-type]


def test_simulation_walks_pre_then_live_then_final():
    state = sports_sim.start([sim_game()], speed=60.0, duration_minutes=30, hide_real=True, now=NOW)

    # speed 60 => one real second is one virtual minute.
    def at(real_seconds: float) -> GameSlide:
        return sports_sim.render(state, NOW + timedelta(seconds=real_seconds))[0]

    assert at(0).state == "pre"
    assert at(30).state == "pre"
    assert at(61).state == "in"
    assert at(200).state == "in"
    assert at(211).state == "post"


def test_kickoff_time_never_moves():
    """A game's start time is a fixed fact; only the distance to it changes.

    Regression: start was once re-derived as an offset from the current moment
    on every poll, which republished a different absolute timestamp each time —
    so the displayed clock time slid backwards toward the present as you watched.
    """
    state = sports_sim.start([sim_game()], speed=60.0, duration_minutes=30, hide_real=True, now=NOW)

    starts = {
        sports_sim.render(state, NOW + timedelta(seconds=s))[0].start
        for s in (0, 5, 17, 40, 59, 100, 205)
    }

    assert len(starts) == 1, f"kickoff drifted across polls: {sorted(starts)}"
    # 60 virtual minutes of pre-game from the moment it started.
    assert starts.pop() == NOW + timedelta(minutes=60)


def test_countdown_shrinks_against_the_virtual_clock():
    """The fixed start still has to read as "45 minutes away" 15 virtual
    minutes in — that's what the client subtracts to get its countdown."""
    state = sports_sim.start([sim_game()], speed=60.0, duration_minutes=30, hide_real=True, now=NOW)

    at = NOW + timedelta(seconds=15)  # 15 virtual minutes in, 45 to go
    rendered = sports_sim.render(state, at)[0]

    assert rendered.state == "pre"
    assert rendered.start - state.virtual_now(at) == timedelta(minutes=45)


def test_virtual_now_matches_real_time_at_1x():
    """Which is what lets real ESPN games be shown alongside a simulation."""
    state = sports_sim.start([sim_game()], speed=1.0, duration_minutes=30, hide_real=False, now=NOW)

    at = NOW + timedelta(minutes=7)

    assert state.virtual_now(at) == at


def test_end_time_never_moves_either():
    state = sports_sim.start([sim_game()], speed=60.0, duration_minutes=60, hide_real=True, now=NOW)

    ends = {
        sports_sim.render(state, NOW + timedelta(seconds=s))[0].end
        for s in (215, 230, 260)
    }

    assert len(ends) == 1
    assert ends.pop() == NOW + timedelta(minutes=60 + 150)


def test_finished_simulation_ages_out_through_the_same_rule_as_a_real_game():
    state = sports_sim.start([sim_game()], speed=60.0, duration_minutes=60, hide_real=True, now=NOW)

    # 210 virtual min = game over; +130 more virtual min puts it past the window.
    at = NOW + timedelta(seconds=340)
    rendered = sports_sim.render(state, at)[0]

    assert rendered.state == "post"
    # Measured on the simulation's clock, which is the one the payload carries.
    assert sports.is_visible(rendered, state.virtual_now(at), 120) is False


def test_simulated_scores_climb_and_never_regress():
    state = sports_sim.start([sim_game()], speed=60.0, duration_minutes=30, hide_real=True, now=NOW)

    scores = [
        sports_sim.render(state, NOW + timedelta(seconds=s))[0].team_score
        for s in range(61, 210, 5)
    ]

    assert scores == sorted(scores)
    assert max(s for s in scores if s is not None) <= 4


def test_final_score_matches_the_script():
    state = sports_sim.start([sim_game()], speed=60.0, duration_minutes=30, hide_real=True, now=NOW)

    final = sports_sim.render(state, NOW + timedelta(seconds=215))[0]

    assert (final.team_score, final.opponent_score) == (4, 2)
    assert final.result == "win"
    assert final.detail == "Final"
    assert final.simulated is True


def test_simulation_expires_and_hands_back_to_espn():
    sports_sim.start([sim_game()], speed=60.0, duration_minutes=10, hide_real=True, now=NOW)

    assert sports_sim.current(NOW + timedelta(minutes=9)) is not None
    assert sports_sim.current(NOW + timedelta(minutes=11)) is None
    # And it stays gone.
    assert sports_sim.current(NOW + timedelta(minutes=9)) is None


def test_duration_is_clamped_to_the_configured_ceiling():
    state = sports_sim.start(
        [sim_game()], speed=60.0, duration_minutes=10_000, hide_real=True, now=NOW
    )

    assert state.expires_at <= NOW + timedelta(minutes=sports_sim._settings.sports_sim_max_minutes)


def test_speed_is_clamped():
    state = sports_sim.start([sim_game()], speed=99_999, duration_minutes=10, hide_real=True, now=NOW)

    assert state.speed == sports_sim.MAX_SPEED


@pytest.mark.parametrize(
    "league,progress,expected",
    [
        ("MLB", 0.0, "Top 1st"),
        ("MLB", 0.7, "Top 7th"),
        ("MLB", 0.75, "Bot 7th"),
        ("NHL", 0.5, "10:00 - 2nd"),
        ("MLS", 0.5, "45'"),
        ("NWSL", 1.0, "90'"),
    ],
)
def test_phase_detail_mimics_espn_phrasing(league, progress, expected):
    assert sports_sim._phase_detail(league, progress) == expected


def test_nba_phase_detail_names_the_quarter():
    assert sports_sim._phase_detail("NBA", 0.6).endswith("3rd Quarter")


# ── Simulator: authentication ──────────────────────────────────────────────


@pytest.fixture
def sim_password(monkeypatch):
    monkeypatch.setattr(sports_sim._settings, "sports_sim_password", "hunter2")
    return "hunter2"


@pytest.fixture
def sim_disabled(monkeypatch):
    """Force the password off.

    Settings loads the repo's own .env, so a developer who has set
    SPORTS_SIM_PASSWORD for local use would otherwise silently flip these
    assertions — the tests have to state the condition they're testing.
    """
    monkeypatch.setattr(sports_sim._settings, "sports_sim_password", "")


def test_simulator_is_disabled_without_a_configured_password(sim_disabled):
    assert sports_sim.is_enabled() is False
    assert sports_sim.create_session("anything") is None


def test_correct_password_mints_a_working_token(sim_password):
    session = sports_sim.create_session(sim_password, now=NOW)

    assert session is not None
    token, expires = session
    assert sports_sim.valid_session(token, NOW) is True
    assert expires > NOW


def test_wrong_password_is_rejected(sim_password):
    assert sports_sim.create_session("hunter3", now=NOW) is None


def test_token_expires(sim_password):
    token, _ = sports_sim.create_session(sim_password, now=NOW)  # type: ignore[misc]

    assert sports_sim.valid_session(token, NOW + timedelta(hours=1)) is True
    assert sports_sim.valid_session(token, NOW + timedelta(hours=3)) is False


def test_unknown_token_is_rejected(sim_password):
    assert sports_sim.valid_session("made-up", NOW) is False
    assert sports_sim.valid_session(None, NOW) is False


def test_sim_login_has_its_own_rate_limit_bucket():
    assert _match_bucket("/api/v1/sports/sim/session") == "sim_auth"
    assert _match_bucket("/api/v1/sports/games") == "default"


# ── API ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_games_endpoint_is_empty_on_a_day_with_no_home_game(client, monkeypatch):
    async def no_games(now=None):
        return []

    monkeypatch.setattr(sports, "get_slides", no_games)

    resp = await client.get("/api/v1/sports/games")

    assert resp.status_code == 200
    body = resp.json()
    assert body["games"] == []
    assert body["simulated"] is False
    assert body["clock_rate"] == 1.0


@pytest.mark.asyncio
async def test_games_endpoint_survives_espn_being_down(client, monkeypatch):
    """A missing carousel slide must never take the map down with it."""
    async def boom(*args, **kwargs):
        raise RuntimeError("ESPN unreachable")

    monkeypatch.setattr(sports, "_fetch_all", boom)

    resp = await client.get("/api/v1/sports/games")

    assert resp.status_code == 200
    assert resp.json()["games"] == []


@pytest.mark.asyncio
async def test_sim_routes_do_not_exist_without_a_password(client, sim_disabled):
    assert (await client.post("/api/v1/sports/sim/session", json={"password": "x"})).status_code == 404
    assert (await client.get("/api/v1/sports/sim")).status_code == 404


@pytest.mark.asyncio
async def test_sim_requires_a_token(client, sim_password):
    assert (await client.get("/api/v1/sports/sim")).status_code == 401
    assert (
        await client.get("/api/v1/sports/sim", headers={"X-Sim-Token": "nope"})
    ).status_code == 401


@pytest.mark.asyncio
async def test_sim_wrong_password_is_401(client, sim_password):
    resp = await client.post("/api/v1/sports/sim/session", json={"password": "wrong"})

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_full_simulator_round_trip(client, sim_password, monkeypatch):
    async def no_games(now=None):
        return []

    monkeypatch.setattr(sports, "get_slides", no_games)

    login = await client.post("/api/v1/sports/sim/session", json={"password": sim_password})
    assert login.status_code == 200
    token = login.json()["token"]
    headers = {"X-Sim-Token": token}

    started = await client.post(
        "/api/v1/sports/sim",
        headers=headers,
        json={
            "games": [{"team_key": "nuggets", "opponent_abbr": "LAL", "lead_minutes": 30}],
            "speed": 60,
            "duration_minutes": 10,
            "hide_real": True,
        },
    )
    assert started.status_code == 200
    assert started.json()["active"] is True

    # The simulated game now shows on the public feed, for every device.
    games = await client.get("/api/v1/sports/games")
    body = games.json()
    assert body["simulated"] is True
    assert body["clock_rate"] == 60
    assert len(body["games"]) == 1
    assert body["games"][0]["team_key"] == "nuggets"
    assert body["games"][0]["simulated"] is True
    assert games.headers["cache-control"] == "no-store"

    stopped = await client.delete("/api/v1/sports/sim", headers=headers)
    assert stopped.status_code == 200
    assert stopped.json()["active"] is False

    assert (await client.get("/api/v1/sports/games")).json()["simulated"] is False


@pytest.mark.asyncio
async def test_sim_rejects_an_unknown_team(client, sim_password):
    login = await client.post("/api/v1/sports/sim/session", json={"password": sim_password})
    headers = {"X-Sim-Token": login.json()["token"]}

    resp = await client.post(
        "/api/v1/sports/sim", headers=headers, json={"games": [{"team_key": "yankees"}]}
    )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_sim_refuses_to_mix_real_games_into_a_sped_up_clock(client, sim_password):
    login = await client.post("/api/v1/sports/sim/session", json={"password": sim_password})
    headers = {"X-Sim-Token": login.json()["token"]}
    body = {"games": [{"team_key": "nuggets"}], "hide_real": False}

    fast = await client.post("/api/v1/sports/sim", headers=headers, json={**body, "speed": 60})
    real_time = await client.post("/api/v1/sports/sim", headers=headers, json={**body, "speed": 1})

    assert fast.status_code == 400
    # At 1× the simulated and wall clocks coincide, so the mix is honest.
    assert real_time.status_code == 200


@pytest.mark.asyncio
async def test_sim_rejects_an_empty_game_list(client, sim_password):
    login = await client.post("/api/v1/sports/sim/session", json={"password": sim_password})
    headers = {"X-Sim-Token": login.json()["token"]}

    resp = await client.post("/api/v1/sports/sim", headers=headers, json={"games": []})

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_teams_endpoint_lists_all_six_clubs(client):
    resp = await client.get("/api/v1/sports/teams")

    keys = {t["key"] for t in resp.json()["teams"]}
    assert keys == {"avalanche", "nuggets", "rockies", "broncos", "rapids", "summit"}
