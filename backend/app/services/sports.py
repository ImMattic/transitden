"""Denver home games, read from ESPN's public (unofficial) scoreboard API.

Why this exists: a Nuggets tip-off empties into Ball Arena's light-rail
platforms for half an hour, and a rider looking at the map deserves to know
that before they plan around it.  Only *home* games are tracked — an away game
doesn't move Denver ridership, so it isn't worth a carousel turn.

Shape of the thing:

    ESPN scoreboard (two requests per league, yesterday and today)
      → _parse_event()  keeps only events where our team is the home side
      → GameSlide
      → cached with a TTL that tightens as a game gets closer

The cache TTL is deliberately adaptive.  Nothing happening in Denver sport for
the next six hours is the common case, and re-asking ESPN about it every thirty
seconds would be rude for no gain; once a game is actually in progress we want
the score inside a minute.

There is no database involvement at all.  If ESPN is down the carousel simply
loses its game slides and the map's own status cycles carry on.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import aiohttp

from app.config import get_settings
from app.schemas.sports import GameSlide

logger = logging.getLogger(__name__)

_settings = get_settings()

_ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
_TIMEOUT = aiohttp.ClientTimeout(total=8)

try:  # pragma: no cover - depends on the image's tz database
    from zoneinfo import ZoneInfo

    _DENVER = ZoneInfo("America/Denver")
except Exception:  # pragma: no cover - fall back to fixed MST
    logger.warning("tzdata unavailable; falling back to fixed UTC-7 for Denver")
    _DENVER = timezone(timedelta(hours=-7))


@dataclass(frozen=True)
class TeamConfig:
    """One tracked club, and how to recognise it in an ESPN payload."""

    key: str
    league: str
    # ESPN's path segment pair, e.g. "hockey/nhl" or "soccer/usa.1".
    espn_path: str
    name: str
    abbr: str
    # Matched case-insensitively against the competitor's display name.  Names
    # survive rebrands and expansion far better than abbreviations do, which is
    # what matters for a club playing its first season.
    keywords: tuple[str, ...]
    emoji: str
    # Brand colours (hex, no '#'), and these win over ESPN's.
    #
    # Two reasons.  ESPN's "color" is whichever the league lists first, which for
    # the Broncos is navy, not the orange anyone actually pictures — and three of
    # the six clubs list a dark navy/purple first, so going along with ESPN would
    # render Broncos, Nuggets and Rockies badges as three near-identical dark
    # rectangles on a day when more than one of them is at home.  So `color` is
    # the club's most *identifying* colour, chosen to stay distinct from the
    # others in the rotation, and `alt_color` is its partner.
    color: str
    alt_color: str
    # Rough wall-clock length, used only to guess an end time for a game that was
    # already final the first time we saw it.
    typical_minutes: int
    # Set for a club whose brand hexes we don't actually know, where whatever
    # ESPN reports beats our guess.  See Denver Summit FC below.
    defer_to_espn_color: bool = False


TEAMS: tuple[TeamConfig, ...] = (
    TeamConfig(
        key="avalanche",
        league="NHL",
        espn_path="hockey/nhl",
        name="Colorado Avalanche",
        abbr="COL",
        keywords=("avalanche",),
        emoji="🏒",
        color="6F263D",
        alt_color="236192",
        typical_minutes=150,
    ),
    TeamConfig(
        key="nuggets",
        league="NBA",
        espn_path="basketball/nba",
        name="Denver Nuggets",
        abbr="DEN",
        keywords=("nuggets",),
        emoji="🏀",
        # Gold over the navy the NBA lists first: it's the colour that reads as
        # "Nuggets" at badge size, and it's the only light badge in the set.
        color="FEC524",
        alt_color="0E2240",
        typical_minutes=145,
    ),
    TeamConfig(
        key="rockies",
        league="MLB",
        espn_path="baseball/mlb",
        name="Colorado Rockies",
        abbr="COL",
        keywords=("rockies",),
        emoji="⚾",
        color="33006F",
        alt_color="C4CED4",
        typical_minutes=180,
    ),
    TeamConfig(
        key="broncos",
        league="NFL",
        espn_path="football/nfl",
        name="Denver Broncos",
        abbr="DEN",
        keywords=("broncos",),
        emoji="🏈",
        color="FB4F14",
        alt_color="002244",
        typical_minutes=195,
    ),
    TeamConfig(
        key="rapids",
        league="MLS",
        espn_path="soccer/usa.1",
        name="Colorado Rapids",
        abbr="COL",
        keywords=("rapids",),
        emoji="⚽",
        # Close to the Avalanche's burgundy, but the sport emoji and the club
        # abbreviation carry the distinction — both clubs abbreviate to COL.
        color="960A2C",
        alt_color="8BB8E8",
        typical_minutes=115,
    ),
    TeamConfig(
        key="summit",
        league="NWSL",
        espn_path="soccer/usa.nwsl",
        name="Denver Summit FC",
        abbr="DEN",
        keywords=("summit",),
        emoji="⚽",
        # The crest is green and white under a red-and-gold sky, but the club
        # has published no hex values, so this only approximates it — the one
        # case where ESPN's reported colours are better than our guess.
        color="0B4D3B",
        alt_color="F2C14E",
        defer_to_espn_color=True,
        typical_minutes=115,
    ),
)

TEAMS_BY_KEY: dict[str, TeamConfig] = {t.key: t for t in TEAMS}


def _teams_by_path() -> dict[str, list[TeamConfig]]:
    """Group clubs by ESPN path so one request covers a whole league."""
    grouped: dict[str, list[TeamConfig]] = {}
    for team in TEAMS:
        grouped.setdefault(team.espn_path, []).append(team)
    return grouped


# ── Observed end times ─────────────────────────────────────────────────────
# ESPN reports that a game is final but not *when* it went final, and "COL won
# 20 min ago" needs that.  So note the first poll at which each event showed up
# completed.  Lost on restart, which only costs us a fall back to the
# start+typical_minutes estimate for games already over at that moment.
_final_seen: dict[str, datetime] = {}


def _record_final(event_id: str, now: datetime) -> datetime:
    return _final_seen.setdefault(event_id, now)


# ── Cache ──────────────────────────────────────────────────────────────────

_cache: tuple[datetime, list[GameSlide]] | None = None
_cache_lock = asyncio.Lock()

# Last good result per league, so a single flaky ESPN response doesn't blink a
# game off the map.  Held only briefly — stale scores are worse than no scores.
_league_cache: dict[str, tuple[datetime, list[GameSlide]]] = {}
_LEAGUE_FALLBACK_MAX_AGE = timedelta(minutes=15)


def reset_caches() -> None:
    """Drop every cached value.  Used by tests."""
    global _cache
    _cache = None
    _league_cache.clear()
    _final_seen.clear()


# ── Parsing ────────────────────────────────────────────────────────────────


def _as_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _parse_iso(value: object) -> datetime | None:
    """ESPN stamps events like ``2026-09-10T01:10Z`` — not quite ISO to Python."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _matches(team: TeamConfig, competitor: dict) -> bool:
    info = competitor.get("team") or {}
    haystack = " ".join(
        str(info.get(field, ""))
        for field in ("displayName", "shortDisplayName", "name", "location", "nickname")
    ).lower()
    return any(word in haystack for word in team.keywords)


def _competitor_label(info: dict, fallback_abbr: str, fallback_name: str) -> tuple[str, str]:
    abbr = str(info.get("abbreviation") or "").strip() or fallback_abbr
    name = (
        str(info.get("shortDisplayName") or "").strip()
        or str(info.get("displayName") or "").strip()
        or fallback_name
    )
    return abbr, name


def _parse_event(event: dict, team: TeamConfig, now: datetime) -> GameSlide | None:
    """Turn one ESPN event into a slide, or ``None`` if it isn't a home game."""
    competitions = event.get("competitions") or []
    if not competitions:
        return None
    comp = competitions[0]

    competitors = comp.get("competitors") or []
    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    if home is None or away is None:
        return None

    # Home games only.  This is the whole point of the feature: an away game
    # doesn't put anybody on a train in Denver.
    if not _matches(team, home):
        return None

    status = comp.get("status") or event.get("status") or {}
    stype = status.get("type") or {}
    raw_state = str(stype.get("state") or "pre").lower()
    state = raw_state if raw_state in ("pre", "in", "post") else "pre"

    start = _parse_iso(comp.get("date") or event.get("date"))
    if start is None:
        return None

    home_info = home.get("team") or {}
    away_info = away.get("team") or {}
    team_abbr, team_name = _competitor_label(home_info, team.abbr, team.name)
    opp_abbr, opp_name = _competitor_label(away_info, "OPP", "Opponent")

    team_score = _as_int(home.get("score"))
    opp_score = _as_int(away.get("score"))

    end: datetime | None = None
    result = None
    if state == "post":
        end = _record_final(str(event.get("id") or comp.get("id") or start.isoformat()), now)
        if home.get("winner") is True:
            result = "win"
        elif away.get("winner") is True:
            result = "loss"
        elif team_score is not None and opp_score is not None:
            if team_score > opp_score:
                result = "win"
            elif team_score < opp_score:
                result = "loss"
            else:
                result = "draw"

    detail = (
        str(stype.get("shortDetail") or "").strip()
        or str(stype.get("detail") or "").strip()
        or str(stype.get("description") or "").strip()
    )

    venue = None
    venue_info = comp.get("venue") or {}
    if isinstance(venue_info, dict):
        venue = str(venue_info.get("fullName") or "").strip() or None

    return GameSlide(
        id=str(event.get("id") or comp.get("id") or f"{team.key}-{start.isoformat()}"),
        league=team.league,
        sport_emoji=team.emoji,
        team_key=team.key,
        team_abbr=team_abbr,
        team_name=team_name,
        opponent_abbr=opp_abbr,
        opponent_name=opp_name,
        color=_team_color(team, home_info, "color"),
        alt_color=_team_color(team, home_info, "alternateColor"),
        state=state,  # type: ignore[arg-type]
        start=start,
        end=end,
        detail=detail,
        team_score=team_score,
        opponent_score=opp_score,
        result=result,  # type: ignore[arg-type]
        venue=venue,
    )


def _team_color(team: TeamConfig, info: dict, field: str) -> str:
    """The colour to paint this club's badge.

    Our curated hex wins, except for a club we've flagged as unknown — see
    ``TeamConfig.color`` for why ESPN's first-listed colour is the wrong choice
    for the rest of them.
    """
    ours = team.color if field == "color" else team.alt_color
    if not team.defer_to_espn_color:
        return ours
    return _clean_hex(info.get(field)) or ours


def _clean_hex(value: object) -> str | None:
    """ESPN returns bare hex like ``6f263d``; reject anything that isn't."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().lstrip("#").upper()
    if len(candidate) != 6:
        return None
    if any(c not in "0123456789ABCDEF" for c in candidate):
        return None
    return candidate


# ── Fetching ───────────────────────────────────────────────────────────────


def _fetch_dates(today: date) -> tuple[date, date]:
    """Yesterday and today.

    Yesterday is included so a game that started at 9pm and ended after midnight
    is still inside its post-game window rather than vanishing at the date roll.

    Two separate single-day requests rather than one ``YYYYMMDD-YYYYMMDD`` range:
    ESPN's scoreboard endpoint started rejecting the range form outright (HTTP
    400, "Failed to get events endpoint") for every league regardless of the
    dates given, which silently dropped every game from the carousel once the
    per-league fallback cache aged out. Single-day queries are unaffected.
    """
    return today - timedelta(days=1), today


async def _fetch_day(
    session: aiohttp.ClientSession, url: str, day: date
) -> list[dict]:
    params = {"dates": f"{day:%Y%m%d}", "limit": "100"}
    async with session.get(url, params=params) as resp:
        resp.raise_for_status()
        # ESPN serves this as text/javascript often enough to matter.
        payload = await resp.json(content_type=None)
    return payload.get("events") or []


async def _fetch_league(
    session: aiohttp.ClientSession,
    path: str,
    teams: list[TeamConfig],
    now: datetime,
) -> list[GameSlide]:
    url = f"{_ESPN_BASE}/{path}/scoreboard"
    days = _fetch_dates(now.astimezone(_DENVER).date())
    results = await asyncio.gather(*(_fetch_day(session, url, day) for day in days))

    # Dedupe by event id in case the same game were ever returned by both days.
    events_by_id: dict[str, dict] = {}
    for events in results:
        for event in events:
            events_by_id[str(event.get("id"))] = event

    slides: list[GameSlide] = []
    for event in events_by_id.values():
        for team in teams:
            slide = _parse_event(event, team, now)
            if slide is not None:
                slides.append(slide)
                break
    return slides


async def _fetch_all(now: datetime) -> list[GameSlide]:
    grouped = _teams_by_path()
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        results = await asyncio.gather(
            *(_fetch_league(session, path, teams, now) for path, teams in grouped.items()),
            return_exceptions=True,
        )

    slides: list[GameSlide] = []
    for path, result in zip(grouped.keys(), results):
        if isinstance(result, BaseException):
            logger.warning("ESPN scoreboard failed for %s: %s", path, result)
            cached = _league_cache.get(path)
            if cached and now - cached[0] <= _LEAGUE_FALLBACK_MAX_AGE:
                slides.extend(cached[1])
            continue
        _league_cache[path] = (now, result)
        slides.extend(result)
    return slides


# ── Visibility ─────────────────────────────────────────────────────────────


def end_time(slide: GameSlide, teams_by_key: dict[str, TeamConfig] | None = None) -> datetime:
    """Best available guess at when a finished game let out."""
    if slide.end is not None:
        return slide.end
    table = teams_by_key or TEAMS_BY_KEY
    team = table.get(slide.team_key)
    minutes = team.typical_minutes if team else 150
    return slide.start + timedelta(minutes=minutes)


def is_visible(slide: GameSlide, now: datetime, post_window_minutes: int) -> bool:
    """Whether a slide still earns a turn in the carousel.

    Finished games age out; anything else stays.  Nothing here filters on the
    date, because only today's and yesterday's games are ever fetched.
    """
    if slide.state != "post":
        return True
    return now - end_time(slide) <= timedelta(minutes=post_window_minutes)


def sort_key(slide: GameSlide) -> tuple:
    """Live first, then what's coming, then what just finished.

    A game in progress is the most useful thing we can say, and a game that
    ended three hours ago the least.
    """
    rank = {"in": 0, "pre": 1, "post": 2}[slide.state]
    if slide.state == "post":
        # Most recently finished first.
        return (rank, -end_time(slide).timestamp())
    return (rank, slide.start.timestamp())


def _ttl_seconds(slides: list[GameSlide], now: datetime) -> int:
    """How long this answer stays fresh.

    Tightens as the day's games approach so a live score is never more than a
    poll old, and relaxes to ten minutes when Denver has nothing on.
    """
    if any(s.state == "in" for s in slides):
        return _settings.sports_poll_seconds_live
    soon = timedelta(minutes=_settings.sports_countdown_minutes)
    if any(s.state == "pre" and timedelta(0) <= s.start - now <= soon for s in slides):
        return _settings.sports_poll_seconds_soon
    return _settings.sports_poll_seconds_idle


async def get_slides(now: datetime | None = None) -> list[GameSlide]:
    """Today's Denver home games, cached.

    Never raises: a failure to reach ESPN yields an empty list (or the last good
    per-league answer), because a missing carousel slide must not take the map
    down with it.
    """
    global _cache
    now = now or datetime.now(tz=timezone.utc)

    cached = _cache
    if cached is not None and now < cached[0]:
        return cached[1]

    async with _cache_lock:
        # Another request may have refreshed while we waited for the lock.
        cached = _cache
        if cached is not None and now < cached[0]:
            return cached[1]

        try:
            slides = await _fetch_all(now)
        except Exception:
            logger.exception("Sports scoreboard refresh failed")
            slides = []

        visible = [
            s for s in slides if is_visible(s, now, _settings.sports_postgame_window_minutes)
        ]
        visible.sort(key=sort_key)
        _cache = (now + timedelta(seconds=_ttl_seconds(visible, now)), visible)
        return visible
