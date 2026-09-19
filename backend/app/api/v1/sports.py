"""Home-game carousel feed, plus the password-gated simulator that drives it.

``GET /sports/games`` is the only public surface: it answers with whatever
Denver home games deserve a turn in the map's status carousel right now, which
on most days is an empty list.

Everything under ``/sports/sim`` is the test harness.  It does not exist unless
``SPORTS_SIM_PASSWORD`` is set — the routes 404 rather than 403 so a deployment
that never opted in doesn't advertise a control surface it isn't using.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Response

from app.config import get_settings
from app.schemas.sports import (
    GameSlide,
    GamesResponse,
    SimSessionRequest,
    SimSessionResponse,
    SimStartRequest,
    SimStatusResponse,
)
from app.services import sports, sports_sim

router = APIRouter(prefix="/sports", tags=["sports"])

_settings = get_settings()


# ── Public feed ────────────────────────────────────────────────────────────


@router.get("/games", response_model=GamesResponse)
async def get_games(response: Response) -> GamesResponse:
    """Denver home games worth showing, newest phase first.

    Returns an empty list on a day with no home game, which is the intended
    resting state — the carousel then shows only its own map slides.
    """
    now = datetime.now(tz=timezone.utc)

    if not _settings.sports_enabled:
        response.headers["Cache-Control"] = "public, max-age=300"
        return GamesResponse(games=[], generated_at=now)

    sim = sports_sim.current(now)
    games: list[GameSlide] = []

    # A running simulation puts the whole payload on its own timeline, so every
    # age and countdown in it — and the client's, via `generated_at` — is
    # measured against the virtual clock rather than the wall one.
    reference = sim.virtual_now(now) if sim is not None else now

    if sim is not None:
        games.extend(sports_sim.render(sim, now))

    if sim is None or not sim.hide_real:
        # Only reachable at 1× speed, where the two timelines coincide; the
        # control endpoint refuses the combination otherwise.
        games.extend(await sports.get_slides(now))

    window = _settings.sports_postgame_window_minutes
    games = [g for g in games if sports.is_visible(g, reference, window)]
    games.sort(key=sports.sort_key)

    # A finished game whose real end time we don't trust (see _record_final)
    # is sent to the client with a resolved estimate rather than a bare
    # ``null`` — the frontend says how long ago from whatever `end` it's
    # given, and shouldn't have to re-derive the same estimate itself.
    for g in games:
        if g.state == "post" and g.end is None:
            g.end = sports.end_time(g)

    # A simulated payload must never be cached: its clock moves far faster than
    # any max-age we'd pick, and a stale frame would freeze the countdown.
    response.headers["Cache-Control"] = (
        "no-store" if sim is not None else "public, max-age=15"
    )

    return GamesResponse(
        games=games,
        generated_at=reference,
        clock_rate=sim.speed if sim is not None else 1.0,
        simulated=sim is not None,
        sim_expires_at=sim.expires_at if sim is not None else None,
    )


@router.get("/teams")
async def get_teams() -> dict:
    """The tracked clubs, for the simulator's team picker."""
    return {
        "teams": [
            {
                "key": t.key,
                "name": t.name,
                "abbr": t.abbr,
                "league": t.league,
                "emoji": t.emoji,
                "color": t.color,
                "alt_color": t.alt_color,
            }
            for t in sports.TEAMS
        ]
    }


# ── Simulator ──────────────────────────────────────────────────────────────


def _require_enabled() -> None:
    if not sports_sim.is_enabled():
        # 404, not 403: without a configured password there is nothing here.
        raise HTTPException(status_code=404, detail="not found")


def _require_session(token: str | None) -> None:
    _require_enabled()
    if not sports_sim.valid_session(token):
        raise HTTPException(status_code=401, detail="invalid or expired simulator session")


@router.post("/sim/session", response_model=SimSessionResponse)
async def create_sim_session(
    body: SimSessionRequest,
    response: Response,
) -> SimSessionResponse:
    """Exchange the shared password for a short-lived token.

    Rate-limited hard by the ``sim_auth`` bucket in the rate-limit middleware —
    this is the only guessable thing on the server.
    """
    _require_enabled()
    response.headers["Cache-Control"] = "no-store"
    session = sports_sim.create_session(body.password)
    if session is None:
        raise HTTPException(status_code=401, detail="incorrect password")
    token, expires_at = session
    return SimSessionResponse(token=token, expires_at=expires_at)


@router.get("/sim", response_model=SimStatusResponse)
async def get_sim(
    response: Response,
    x_sim_token: Annotated[str | None, Header()] = None,
) -> SimStatusResponse:
    _require_session(x_sim_token)
    response.headers["Cache-Control"] = "no-store"
    return _status()


@router.post("/sim", response_model=SimStatusResponse)
async def start_sim(
    body: SimStartRequest,
    response: Response,
    x_sim_token: Annotated[str | None, Header()] = None,
) -> SimStatusResponse:
    """Start (or replace) a simulation.

    Validation is strict about team keys and generous about everything else —
    durations and speed are clamped rather than rejected, since the failure mode
    of a silly number is a confusing screen, not a broken server.
    """
    _require_session(x_sim_token)
    response.headers["Cache-Control"] = "no-store"

    if not body.games:
        raise HTTPException(status_code=400, detail="at least one game is required")
    if len(body.games) > sports_sim.MAX_GAMES:
        raise HTTPException(
            status_code=400,
            detail=f"at most {sports_sim.MAX_GAMES} simulated games",
        )
    if not body.hide_real and body.speed > 1:
        # Real games sit on the wall clock; a sped-up simulation publishes the
        # payload on its own faster one. Mixing them would race a real game's
        # countdown at the simulation's rate. The two timelines only coincide
        # at 1×, so that's the only speed this combination is honest at.
        raise HTTPException(
            status_code=400,
            detail="showing real games alongside a simulation requires 1× speed",
        )

    games = []
    for entry in body.games:
        if entry.team_key not in sports.TEAMS_BY_KEY:
            raise HTTPException(status_code=400, detail=f"unknown team: {entry.team_key}")
        games.append(
            sports_sim.SimGame(
                team_key=entry.team_key,
                opponent_abbr=(entry.opponent_abbr or "OPP")[:6],
                opponent_name=(entry.opponent_name or "Opponent")[:40],
                lead_minutes=max(0.0, entry.lead_minutes),
                game_minutes=max(1.0, entry.game_minutes),
                final_team_score=max(0, entry.final_team_score),
                final_opponent_score=max(0, entry.final_opponent_score),
            )
        )

    sports_sim.start(
        games=games,
        speed=body.speed,
        duration_minutes=body.duration_minutes,
        hide_real=body.hide_real,
    )
    return _status()


@router.delete("/sim", response_model=SimStatusResponse)
async def stop_sim(
    response: Response,
    x_sim_token: Annotated[str | None, Header()] = None,
) -> SimStatusResponse:
    _require_session(x_sim_token)
    response.headers["Cache-Control"] = "no-store"
    sports_sim.stop()
    return _status()


def _status() -> SimStatusResponse:
    now = datetime.now(tz=timezone.utc)
    state = sports_sim.current(now)
    if state is None:
        return SimStatusResponse(active=False)
    return SimStatusResponse(
        active=True,
        started_at=state.started_at,
        expires_at=state.expires_at,
        speed=state.speed,
        hide_real=state.hide_real,
        elapsed_virtual_minutes=round(state.elapsed_virtual_seconds(now) / 60, 2),
        games=sports_sim.render(state, now),
    )
