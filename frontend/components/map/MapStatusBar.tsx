"use client";

import {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  // Aliased: this file also uses the DOM's own `MouseEvent` (the outside-click
  // listener below), so importing React's under the same name would shadow it.
  type MouseEvent as ReactMouseEvent,
} from "react";
import { useGames, useStopsSearch } from "@/lib/hooks";
import type { StopInfo, VehiclePosition } from "@/lib/types";
import { cn } from "@/lib/utils";
import MapFilterControl from "./MapFilterControl";
import GameSlideView from "./GameSlideView";
import { FilterIcon } from "@/components/ui/FilterControls";
import {
  countActiveMapFilters,
  modeOf,
  vehicleKey,
  type MapFilters,
  type TransitMode,
} from "@/lib/mapFilters";
import {
  carouselTurnCount,
  describeGame,
  gameClock,
  gameForTurn,
  virtualNow,
  visibleGames,
  type GameSlideView as GameView,
} from "@/lib/gameSlides";

interface Props {
  /** The whole live feed — what search looks through and what the menu counts. */
  vehicles: VehiclePosition[];
  /** What survives the current filters, i.e. what the map is drawing. */
  filteredVehicles: VehiclePosition[];
  isLoading: boolean;
  isError: boolean;
  dataUpdatedAt: number;
  filters: MapFilters;
  onFiltersChange: (filters: MapFilters) => void;
  onSelect: (vehicle: VehiclePosition) => void;
  onSelectStop: (stop: StopInfo) => void;
}

/** Auto-advance interval for the status carousel. Keep in step with the
 *  `status-ring-sweep` keyframe duration in globals.css. */
const CYCLE_MS = 15000;
/** How often the countdown on a game slide is recomputed while it's on screen.
 *  Off-screen games only need a slow pulse to age out of the rotation. */
const GAME_TICK_MS = 1000;
const GAME_IDLE_TICK_MS = 20000;
const SEARCH_WIDTH = "min(90vw, 23rem)";

export default function MapStatusBar({
  vehicles,
  filteredVehicles,
  isLoading,
  isError,
  dataUpdatedAt,
  filters,
  onFiltersChange,
  onSelect,
  onSelectStop,
}: Props) {
  const [mode, setMode] = useState<"status" | "search">("status");
  const [cycle, setCycle] = useState(0);

  const filtersActive = countActiveMapFilters(filters) > 0;
  const vehicleCount = filteredVehicles.length;
  const routeCount = useMemo(
    () => new Set(filteredVehicles.map((v) => v.route_id)).size,
    [filteredVehicles],
  );

  const [query, setQuery] = useState("");
  const [dropdownOpen, setDropdownOpen] = useState(false);

  const anchorRef = useRef<HTMLDivElement>(null);
  const statusRowRef = useRef<HTMLButtonElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const { data: stopResults } = useStopsSearch(query);

  // ── Home-game slides ──────────────────────────────────────────────────
  // Nothing here renders on a day with no Denver home game: the endpoint
  // returns an empty list and the carousel keeps its original two turns.
  const { data: gamesData, dataUpdatedAt: gamesUpdatedAt } = useGames();
  const [tick, setTick] = useState(() => Date.now());

  const clock = useMemo(
    () => (gamesData ? gameClock(gamesData, gamesUpdatedAt) : null),
    [gamesData, gamesUpdatedAt],
  );

  const gameViews: GameView[] = useMemo(() => {
    if (!gamesData || !clock) return [];
    const at = virtualNow(clock, tick);
    return visibleGames(gamesData.games, at).map((g) => describeGame(g, at));
  }, [gamesData, clock, tick]);

  const cycleCount = carouselTurnCount(gameViews.length);
  // Games come and go under the carousel, so the index is always read modulo
  // the current length rather than trusted to stay in range.
  const activeCycle = cycle % cycleCount;
  const activeGame = gameForTurn(activeCycle, gameViews);
  const gameSignature = activeGame
    ? `${activeGame.id}|${activeGame.lead}|${activeGame.score ?? ""}|${activeGame.status}`
    : "";

  // Pill width follows the status content so it hugs whatever the current
  // cycle state needs, then animates to the wide search field on takeover.
  const [statusWidth, setStatusWidth] = useState<number>();

  useEffect(() => {
    if (mode !== "status" || !statusRowRef.current) return;
    const measure = () => {
      if (statusRowRef.current) setStatusWidth(statusRowRef.current.scrollWidth);
    };
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [
    mode,
    activeCycle,
    vehicleCount,
    routeCount,
    vehicles.length,
    filtersActive,
    dataUpdatedAt,
    isLoading,
    isError,
    // A game slide changes width as its own text does — "in 45 min" is
    // narrower than "7:00 PM MT", and a score appears mid-game.
    gameSignature,
  ]);

  const openSearch = useCallback(() => {
    setMode("search");
    setDropdownOpen(true);
    setTimeout(() => inputRef.current?.focus(), 60);
  }, []);

  const closeSearch = useCallback(() => {
    setMode("status");
    setQuery("");
    setDropdownOpen(false);
  }, []);

  const advance = useCallback(
    () => setCycle((c) => (c + 1) % cycleCount),
    [cycleCount],
  );

  // Keep the countdown moving between polls. Fast while a game slide is the one
  // on screen; a slow pulse otherwise, which is only there so a game that has
  // passed its two-hour mark leaves the rotation without waiting for a fetch.
  // `describeGame` hands back a fresh object every tick, so the timer keys off
  // whether a game is showing rather than off the view itself — depending on
  // the object would tear the interval down and rebuild it once a second.
  const gameOnScreen = activeGame !== undefined;
  useEffect(() => {
    if (gameViews.length === 0) return;
    const interval = gameOnScreen ? GAME_TICK_MS : GAME_IDLE_TICK_MS;
    const id = setInterval(() => setTick(Date.now()), interval);
    return () => clearInterval(id);
  }, [gameViews.length, gameOnScreen]);

  // Auto-advance the carousel. Re-armed whenever `cycle` changes, so a manual
  // tap resets the countdown (and the sweep ring, which is keyed on `cycle`).
  // Paused while the search field has taken over the pill, and off entirely for
  // visitors who asked for reduced motion — they can still tap to cycle.
  useEffect(() => {
    if (mode !== "status") return;
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    const id = setTimeout(advance, CYCLE_MS);
    return () => clearTimeout(id);
  }, [cycle, mode, advance]);

  useEffect(() => {
    if (mode !== "search") return;
    function handleOutsideClick(e: MouseEvent) {
      if (anchorRef.current && !anchorRef.current.contains(e.target as Node)) {
        closeSearch();
      }
    }
    function handleKey(e: KeyboardEvent) {
      if (e.key === "Escape") closeSearch();
    }
    document.addEventListener("mousedown", handleOutsideClick);
    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("mousedown", handleOutsideClick);
      document.removeEventListener("keydown", handleKey);
    };
  }, [mode, closeSearch]);

  // ── Route/vehicle browser ────────────────────────────────────────────────
  // Grouped straight from the live feed rather than the static route list, so
  // what's browsable here — and what "all active vehicles underneath" means —
  // is exactly the set of routes and vehicles actually running right now.
  interface RouteGroup {
    routeId: string;
    shortName: string;
    longName: string;
    color: string;
    mode: TransitMode;
    vehicles: { key: string; label: string }[];
  }

  const collator = useMemo(() => new Intl.Collator(undefined, { numeric: true }), []);

  const routeGroups = useMemo<RouteGroup[]>(() => {
    const map = new Map<string, RouteGroup>();
    for (const v of vehicles) {
      let group = map.get(v.route_id);
      if (!group) {
        group = {
          routeId: v.route_id,
          shortName: v.route_short_name || v.route_id,
          longName: v.route_long_name ?? "",
          color: v.route_color || "888888",
          mode: modeOf(v.route_type),
          vehicles: [],
        };
        map.set(v.route_id, group);
      }
      const key = vehicleKey(v);
      if (!group.vehicles.some((x) => x.key === key)) {
        group.vehicles.push({ key, label: v.vehicle_label ?? key });
      }
    }
    for (const group of map.values()) {
      group.vehicles.sort((a, b) => collator.compare(a.label, b.label));
    }
    return [...map.values()].sort((a, b) => collator.compare(a.shortName, b.shortName));
  }, [vehicles, collator]);

  // A route matches on its own name; a vehicle number matches through the route
  // it's running on, so searching a fleet number pulls up the route it belongs to.
  const queryLower = query.toLowerCase().trim();
  const matchedRouteGroups = useMemo(() => {
    if (!queryLower) return routeGroups;
    return routeGroups.filter(
      (g) =>
        g.shortName.toLowerCase().includes(queryLower) ||
        g.longName.toLowerCase().includes(queryLower) ||
        g.vehicles.some((v) => v.label.toLowerCase().includes(queryLower)),
    );
  }, [routeGroups, queryLower]);

  const groupedRoutes = useMemo(() => {
    const rail = matchedRouteGroups.filter((g) => g.mode === "rail");
    const bus = matchedRouteGroups.filter((g) => g.mode === "bus");
    const other = matchedRouteGroups.filter((g) => g.mode === "other");
    return { rail, bus, other };
  }, [matchedRouteGroups]);

  // A single matching route — searching "15", or a fleet number that only runs
  // on one route — drops its vehicles open automatically; anything broader
  // stays collapsed until its caret is tapped. Manual taps are tracked
  // separately so they win over the auto-expand, and reset on every new query
  // so a stale expansion doesn't carry into an unrelated search.
  const autoExpandRouteId =
    queryLower && matchedRouteGroups.length === 1 ? matchedRouteGroups[0].routeId : null;
  const [expandOverride, setExpandOverride] = useState<Record<string, boolean>>({});
  useEffect(() => {
    setExpandOverride({});
  }, [query]);

  function isRouteOpen(routeId: string): boolean {
    return expandOverride[routeId] ?? routeId === autoExpandRouteId;
  }

  function toggleRouteOpen(routeId: string) {
    setExpandOverride((o) => ({ ...o, [routeId]: !isRouteOpen(routeId) }));
  }

  const stops = query.trim().length >= 2 ? stopResults?.stops ?? [] : [];
  const hasRoutes =
    groupedRoutes.rail.length +
      groupedRoutes.bus.length +
      groupedRoutes.other.length >
    0;
  const totalGroups =
    groupedRoutes.rail.length +
    groupedRoutes.bus.length +
    groupedRoutes.other.length +
    stops.length;

  function handleSelectVehicle(key: string) {
    // Prefer one the filters are already showing, so the map jumps to a marker
    // that's on screen rather than to a vehicle the current filters hide.
    const vehicle =
      filteredVehicles.find((v) => vehicleKey(v) === key) ??
      vehicles.find((v) => vehicleKey(v) === key);
    if (vehicle) onSelect(vehicle);
    closeSearch();
  }

  function handleSelectStop(stop: StopInfo) {
    onSelectStop(stop);
    closeSearch();
  }

  // Quick add/remove straight from the browser — the funnel icon on a route or
  // vehicle row toggles it in the live filter set immediately, no draft/Apply
  // step, since this is meant as the fast path past opening the full menu.
  function toggleRouteFilter(e: ReactMouseEvent, routeId: string) {
    e.stopPropagation();
    const active = filters.routeIds.includes(routeId);
    onFiltersChange({
      ...filters,
      routeIds: active
        ? filters.routeIds.filter((id) => id !== routeId)
        : [...filters.routeIds, routeId],
    });
  }

  function toggleVehicleFilter(e: ReactMouseEvent, key: string) {
    e.stopPropagation();
    const active = filters.vehicleKeys.includes(key);
    onFiltersChange({
      ...filters,
      vehicleKeys: active
        ? filters.vehicleKeys.filter((k) => k !== key)
        : [...filters.vehicleKeys, key],
    });
  }

  // ── Cycle content ─────────────────────────────────────────────────────
  // In turn order: vehicle count, then a turn per home game, then freshness.
  const cycleBody = () => {
    if (activeGame) return <GameSlideView view={activeGame} />;
    if (activeCycle === 0) {
      return (
        <span className="flex items-center gap-2 font-medium text-fg-muted">
          <span
            className={cn(
              "inline-block h-2 w-2 shrink-0 rounded-full",
              isError
                ? "bg-danger"
                : isLoading
                  ? "bg-warn"
                  : "bg-ok",
            )}
          />
          {isLoading
            ? "Connecting…"
            : isError
              ? "Feed unavailable"
              : filtersActive
                ? `${vehicleCount} of ${vehicles.length} vehicles · ${routeCount} routes`
                : `${vehicleCount} vehicles · ${routeCount} routes`}
        </span>
      );
    }
    return (
      <span className="text-xs text-fg-muted">
        {dataUpdatedAt
          ? `Updated ${new Date(dataUpdatedAt).toLocaleTimeString()}`
          : "Awaiting data…"}
      </span>
    );
  };

  return (
    <div className="pointer-events-none absolute inset-x-0 top-16 z-[1000] flex justify-center px-3">
      <div ref={anchorRef} className="pointer-events-auto relative flex items-center">
        {/* Search — separate button on the left; expands to take over the info box */}
        <div
          className="flex h-9 shrink-0 items-center overflow-hidden rounded-full border border-line bg-card/90 shadow-lg shadow-black/30 backdrop-blur-md transition-[width,margin] duration-300 ease-out"
          style={{
            width: mode === "search" ? SEARCH_WIDTH : "2.25rem",
            marginRight: mode === "search" ? 0 : "0.5rem",
            // Leaves room for the filter button, which stays put while search
            // is open rather than collapsing with the info pill.
            maxWidth: "calc(100vw - 6rem)",
          }}
        >
          {mode === "status" ? (
            <button
              type="button"
              onClick={openSearch}
              aria-label="Search routes and stations"
              className="flex h-9 w-9 shrink-0 items-center justify-center text-fg-subtle transition-colors hover:text-fg-muted"
            >
              <SearchIcon />
            </button>
          ) : (
            <div className="animate-search-in flex w-full items-center gap-2 px-3">
              <SearchIcon className="shrink-0 text-fg-subtle" />
              <input
                ref={inputRef}
                type="search"
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value);
                  setDropdownOpen(true);
                }}
                onFocus={() => setDropdownOpen(true)}
                placeholder="Search route or station…"
                className="min-w-0 flex-1 bg-transparent text-base text-fg-muted placeholder-fg-subtle outline-none sm:text-sm"
              />
              <button
                type="button"
                onClick={closeSearch}
                aria-label="Close search"
                className="shrink-0 text-fg-subtle transition-colors hover:text-fg-muted"
              >
                <ClearIcon />
              </button>
            </div>
          )}
        </div>

        {/* Info box — tap to cycle; collapses when search takes over */}
        <div className="relative shrink-0">
          <div
            className={cn(
              "group flex h-9 items-center overflow-hidden rounded-full border bg-card/90 text-sm text-fg-muted shadow-lg shadow-black/30 backdrop-blur-md transition-[width,opacity,background-color,border-color,box-shadow] duration-300 ease-out",
              activeGame
                ? "border-transparent"
                : filtersActive
                  ? "border-accent/60"
                  : "border-line",
              mode === "search"
                ? "pointer-events-none"
                : "cursor-pointer hover:border-line-strong hover:bg-card hover:shadow-black/40",
            )}
            style={{
              width:
                mode === "search"
                  ? 0
                  : statusWidth
                    ? statusWidth + 2
                    : undefined,
              opacity: mode === "search" ? 0 : 1,
              maxWidth: "calc(100vw - 8rem)",
              // The team's colour carries onto the pill's rim while its slide is
              // up. Decorative only — the readable pairing is the badge, which
              // picks its own text colour by contrast.
              ...(activeGame ? { borderColor: `${activeGame.color}99` } : {}),
            }}
            aria-hidden={mode === "search"}
          >
            <button
              ref={statusRowRef}
              type="button"
              onClick={advance}
              aria-label={activeGame ? activeGame.label : "Cycle map info"}
              tabIndex={mode === "search" ? -1 : 0}
              className="flex items-center whitespace-nowrap px-3 transition-transform duration-150 ease-out active:scale-[0.97]"
            >
              <span key={activeCycle} className="animate-cycle-in inline-flex items-center">
                {cycleBody()}
              </span>
            </button>
          </div>

          {/* Time-to-advance outline — starts as a full thin ring and retreats
              to nothing as the cycle runs out, then restarts (keyed on `cycle`,
              so a manual tap resets it too). Purely decorative. */}
          {mode === "status" && (
            <svg
              className={cn(
                "pointer-events-none absolute inset-0 h-full w-full overflow-visible",
                activeGame
                  ? undefined
                  : filtersActive
                    ? "text-accent/70"
                    : "text-fg-subtle/60",
              )}
              style={activeGame ? { color: activeGame.color } : undefined}
              aria-hidden="true"
            >
              <rect
                key={activeCycle}
                x="0"
                y="0"
                width="100%"
                height="100%"
                rx="18"
                ry="18"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                pathLength={1}
                strokeDasharray="1"
                style={{ strokeDashoffset: 1 }}
                className="animate-status-ring"
              />
            </svg>
          )}
        </div>

        {/* Filter menu — stays reachable while search is open, so the pill is the
            only thing that gives up its space. */}
        <div className="ml-2 shrink-0">
          <MapFilterControl
            vehicles={vehicles}
            filters={filters}
            onChange={onFiltersChange}
          />
        </div>

        {/* Search results hang off the search field's own left edge — the row is
            no longer symmetric now that the filter button sits at its right end. */}
        {mode === "search" && dropdownOpen && (
          <ul
            className="absolute left-0 top-full z-[1001] mt-2 max-h-80 overflow-y-auto rounded-lg border border-line-strong bg-overlay/95 shadow-lg backdrop-blur-sm"
            style={{ width: SEARCH_WIDTH, maxWidth: "calc(100vw - 6rem)" }}
          >
            {(
              [
                ["Rail", groupedRoutes.rail, false],
                ["Bus", groupedRoutes.bus, groupedRoutes.rail.length > 0],
                [
                  "Other",
                  groupedRoutes.other,
                  groupedRoutes.rail.length + groupedRoutes.bus.length > 0,
                ],
              ] as const
            ).map(([label, list, divider]) =>
              list.length > 0 ? (
                <Fragment key={label}>
                  <li
                    className={cn(
                      "px-3 py-1 text-xs font-semibold uppercase tracking-wide text-fg-subtle",
                      divider && "border-t border-line-strong",
                    )}
                  >
                    {label}
                  </li>
                  {list.map((g) => {
                    const open = isRouteOpen(g.routeId);
                    const routeActive = filters.routeIds.includes(g.routeId);
                    return (
                      <Fragment key={g.routeId}>
                        <li className="flex items-stretch border-t border-line">
                          {/* Caret + name — the dropdown trigger */}
                          <button
                            type="button"
                            onMouseDown={(e) => e.preventDefault()}
                            onClick={() => toggleRouteOpen(g.routeId)}
                            aria-expanded={open}
                            aria-label={`${open ? "Hide" : "Show"} active vehicles on route ${g.shortName}`}
                            className="flex min-w-0 flex-1 items-center gap-2.5 px-3 py-2.5 text-left transition-colors hover:bg-raised"
                          >
                            <CaretIcon
                              className={cn(
                                "shrink-0 text-fg-subtle transition-transform duration-150",
                                open && "rotate-90",
                              )}
                            />
                            <span
                              className="h-3 w-3 shrink-0 rounded-full"
                              style={{ backgroundColor: `#${g.color}` }}
                            />
                            <div className="min-w-0 flex-1">
                              <p className="text-sm font-semibold text-fg">
                                {g.shortName}
                              </p>
                              <p className="truncate text-xs text-fg-subtle">
                                {g.longName || `${g.vehicles.length} active`}
                              </p>
                            </div>
                          </button>
                          {/* Quick-filter — adds/removes this route from the map
                              filters without opening the full filter menu. */}
                          <button
                            type="button"
                            onMouseDown={(e) => e.preventDefault()}
                            onClick={(e) => toggleRouteFilter(e, g.routeId)}
                            aria-pressed={routeActive}
                            aria-label={
                              routeActive
                                ? `Remove route ${g.shortName} from filters`
                                : `Add route ${g.shortName} to filters`
                            }
                            className={cn(
                              "flex shrink-0 items-center justify-center px-3 transition-colors",
                              routeActive
                                ? "text-accent"
                                : "text-fg-subtle hover:text-fg-muted",
                            )}
                          >
                            <FilterIcon className="h-4 w-4" />
                          </button>
                        </li>
                        {open &&
                          g.vehicles.map((v) => {
                            const vehicleActive = filters.vehicleKeys.includes(v.key);
                            return (
                              <li key={v.key} className="flex items-stretch border-t border-line/60">
                                <button
                                  type="button"
                                  onMouseDown={(e) => e.preventDefault()}
                                  onClick={() => handleSelectVehicle(v.key)}
                                  className="flex min-w-0 flex-1 items-center gap-2.5 py-2 pl-10 pr-3 text-left transition-colors hover:bg-raised"
                                >
                                  <span className="truncate text-sm text-fg-muted">
                                    #{v.label}
                                  </span>
                                </button>
                                <button
                                  type="button"
                                  onMouseDown={(e) => e.preventDefault()}
                                  onClick={(e) => toggleVehicleFilter(e, v.key)}
                                  aria-pressed={vehicleActive}
                                  aria-label={
                                    vehicleActive
                                      ? `Remove vehicle #${v.label} from filters`
                                      : `Add vehicle #${v.label} to filters`
                                  }
                                  className={cn(
                                    "flex shrink-0 items-center justify-center px-3 transition-colors",
                                    vehicleActive
                                      ? "text-accent"
                                      : "text-fg-subtle hover:text-fg-muted",
                                  )}
                                >
                                  <FilterIcon className="h-3.5 w-3.5" />
                                </button>
                              </li>
                            );
                          })}
                      </Fragment>
                    );
                  })}
                </Fragment>
              ) : null,
            )}

            {stops.length > 0 && (
              <>
                <li
                  className={cn(
                    "px-3 py-1 text-xs font-semibold uppercase tracking-wide text-fg-subtle",
                    hasRoutes && "border-t border-line-strong",
                  )}
                >
                  Stations
                </li>
                {stops.map((stop) => (
                  <li key={stop.stop_id}>
                    <button
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => handleSelectStop(stop)}
                      className="flex w-full items-center gap-3 border-t border-line px-3 py-2.5 text-left transition-colors hover:bg-raised"
                    >
                      <span className="shrink-0 text-fg-subtle">
                        <StationIcon />
                      </span>
                      <div className="min-w-0 flex-1">
                        <p className="text-sm font-semibold text-fg">
                          {stop.stop_name}
                        </p>
                        <div className="mt-0.5 flex flex-wrap items-center gap-1">
                          {stop.stop_desc && (
                            <span className="mr-1 text-xs text-fg-subtle">
                              {stop.stop_desc}
                            </span>
                          )}
                          {stop.routes.slice(0, 6).map((r) => (
                            <span
                              key={r.route_id}
                              className="inline-block rounded px-1 py-px text-[10px] font-bold leading-tight text-white"
                              style={{ backgroundColor: `#${r.color || "888888"}` }}
                            >
                              {r.short_name}
                            </span>
                          ))}
                          {stop.routes.length > 6 && (
                            <span className="text-[10px] text-fg-subtle">
                              +{stop.routes.length - 6}
                            </span>
                          )}
                        </div>
                      </div>
                    </button>
                  </li>
                ))}
              </>
            )}

            {totalGroups === 0 && (
              <li className="px-3 py-3 text-sm text-fg-subtle">
                No routes or stations found
              </li>
            )}
          </ul>
        )}
      </div>
    </div>
  );
}

/** Right-pointing at rest, rotated 90° open — the route row's dropdown trigger. */
function CaretIcon({ className = "" }: { className?: string }) {
  return (
    <svg className={`h-3.5 w-3.5 ${className}`} viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path
        fillRule="evenodd"
        d="M7.293 14.707a1 1 0 010-1.414L10.586 10 7.293 6.707a1 1 0 011.414-1.414l4 4a1 1 0 010 1.414l-4 4a1 1 0 01-1.414 0z"
        clipRule="evenodd"
      />
    </svg>
  );
}

function SearchIcon({ className = "" }: { className?: string }) {
  return (
    <svg
      className={`h-4 w-4 ${className}`}
      viewBox="0 0 20 20"
      fill="currentColor"
      aria-hidden="true"
    >
      <path
        fillRule="evenodd"
        d="M9 3.5a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11ZM2 9a7 7 0 1 1 12.452 4.391l3.328 3.329a.75.75 0 1 1-1.06 1.06l-3.329-3.328A7 7 0 0 1 2 9Z"
        clipRule="evenodd"
      />
    </svg>
  );
}

function ClearIcon() {
  return (
    <svg className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path d="M6.28 5.22a.75.75 0 0 0-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 1 0 1.06 1.06L10 11.06l3.72 3.72a.75.75 0 1 0 1.06-1.06L11.06 10l3.72-3.72a.75.75 0 0 0-1.06-1.06L10 8.94 6.28 5.22Z" />
    </svg>
  );
}

function StationIcon() {
  return (
    <svg className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path
        fillRule="evenodd"
        d="M9.69 18.933l.003.001C9.89 19.02 10 19 10 19s.11.02.308-.066l.002-.001.006-.003.018-.008a5.741 5.741 0 00.281-.14c.186-.096.446-.24.757-.433.62-.384 1.445-.966 2.274-1.765C15.302 15.01 17 12.42 17 9A7 7 0 1 0 3 9c0 3.42 1.698 6.01 3.354 7.585.829.799 1.654 1.381 2.274 1.765.311.193.57.337.757.433a5.741 5.741 0 00.281.14l.018.008.006.003zM10 11.5a2.5 2.5 0 100-5 2.5 2.5 0 000 5z"
        clipRule="evenodd"
      />
    </svg>
  );
}
