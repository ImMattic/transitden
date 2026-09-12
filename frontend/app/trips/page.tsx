"use client";
import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import { useActiveVehicles, useLimits, useRoutes, useVehicles } from "@/lib/hooks";
import { ApiError } from "@/lib/api";
import { Card, SectionHeading } from "@/components/ui/Card";
import LoadingSpinner from "@/components/ui/LoadingSpinner";
import ExportButton from "@/components/ui/ExportButton";
import TripStatusBadge from "@/components/ui/TripStatusBadge";
import DateRangePicker from "@/components/ui/DateRangePicker";
import TripFilterMenu from "@/components/trips/TripFilterMenu";
import { ActiveFilterChip, FilterIcon } from "@/components/ui/FilterControls";
import {
  cn,
  computeTripStatus,
  formatDateTime,
  formatDelayMin,
  isTripInProgress,
  occupancyLabel,
  routeColor,
} from "@/lib/utils";
import {
  EMPTY_TRIP_FILTERS,
  countActiveTripFilters,
  tripFilterChips,
  tripFiltersEqual,
  tripFiltersFromParams,
  tripFiltersToParams,
  tripFiltersToQuery,
  type TripFilters,
} from "@/lib/tripFilters";
import {
  DEFAULT_RANGE_LIMITS,
  fromLocalInput,
  isoToLocalInput,
  localInputToIso,
  toLocalInput,
  type RangeLimits,
} from "@/lib/dateRange";
import type { ActiveVehicle } from "@/lib/types";

function ChevronLeftIcon() {
  return (
    <svg className="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path
        fillRule="evenodd"
        d="M12.707 5.293a1 1 0 010 1.414L9.414 10l3.293 3.293a1 1 0 01-1.414 1.414l-4-4a1 1 0 010-1.414l4-4a1 1 0 011.414 0z"
        clipRule="evenodd"
      />
    </svg>
  );
}

function ChevronRightIcon() {
  return (
    <svg className="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path
        fillRule="evenodd"
        d="M7.293 14.707a1 1 0 010-1.414L10.586 10 7.293 6.707a1 1 0 011.414-1.414l4 4a1 1 0 010 1.414l-4 4a1 1 0 01-1.414 0z"
        clipRule="evenodd"
      />
    </svg>
  );
}

// Same four-way bucketing used for the delay dots on the trip map / stop
// timeline (see VehicleTripMap.tsx) — an average delay reads on the same scale
// as a single stop's.
function avgDelayColor(seconds: number | null): string {
  if (seconds === null) return "text-fg-muted";
  if (seconds > 600) return "text-danger";
  if (seconds > 300) return "text-warn";
  if (seconds < -300) return "text-accent";
  return "text-ok";
}

function formatDuration(minutes: number): string {
  const mins = Math.round(minutes);
  if (mins < 60) return `${mins}m`;
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return m === 0 ? `${h}h` : `${h}h ${m}m`;
}

function RouteBadge({ shortName, color }: { shortName: string | null; color: string | null }) {
  const bg = routeColor(color ?? "888888");
  return (
    <span
      className="inline-block rounded px-2 py-0.5 text-xs font-bold text-white"
      style={{ backgroundColor: bg }}
    >
      {shortName ?? "?"}
    </span>
  );
}

function TripsContent() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const urlStart = searchParams.get("start");
  const urlEnd = searchParams.get("end");
  // The whole applied set lives in the URL, so a filtered view is shareable and
  // the browser's back button walks it the way it walks the date range. Keyed on
  // the query string rather than the params object, so the memo (and the effect
  // that follows it) only re-runs when the URL genuinely changed.
  const paramsKey = searchParams.toString();
  const appliedFilters = useMemo(
    () => tripFiltersFromParams(new URLSearchParams(paramsKey)),
    [paramsKey],
  );

  const [startLocal, setStartLocal] = useState(() =>
    urlStart ? isoToLocalInput(urlStart) : toLocalInput(new Date(Date.now() - 3_600_000)),
  );
  const [endLocal, setEndLocal] = useState(() =>
    urlEnd ? isoToLocalInput(urlEnd) : toLocalInput(new Date()),
  );
  // Two tiers, narrowing toward the table:
  //   draft — live edits inside the menu, not yet queried
  //   URL   — what's actually being asked for; the only thing the query reads.
  //           "Apply filters" commits the filter set here immediately; "Load
  //           trips" commits a new date/time window (keeping whatever filter
  //           set is already applied).
  const [draft, setDraft] = useState<TripFilters>(appliedFilters);
  const [menuOpen, setMenuOpen] = useState(false);

  const PAGE_SIZE_OPTIONS = [15, 30, 50, 100] as const;
  const [pageSize, setPageSize] = useState<number>(15);
  const [page, setPage] = useState(1);

  // Sync picker state when URL params change (e.g. after "Apply filters",
  // "Load trips", or browser back/forward)
  useEffect(() => {
    if (urlStart) setStartLocal(isoToLocalInput(urlStart));
    if (urlEnd) setEndLocal(isoToLocalInput(urlEnd));
    setDraft(appliedFilters);
    setPage(1);
  }, [urlStart, urlEnd, appliedFilters]);

  // ── Selectable date window ────────────────────────────────────────────────
  // The API rejects a range that is inverted, wider than vehicles_max_span_hours,
  // or older than the hypertable's retention. Rather than let someone pick such a
  // range and read the error afterwards, we hand those same limits to the pickers
  // so the impossible days come up greyed out.
  const limitsQuery = useLimits();
  const limits: RangeLimits = useMemo(
    () =>
      limitsQuery.data
        ? {
            maxSpanHours: limitsQuery.data.vehicles_max_span_hours,
            retentionDays: limitsQuery.data.data_retention_days,
          }
        : DEFAULT_RANGE_LIMITS,
    [limitsQuery.data],
  );

  // "Now" is the upper bound of every field, so keep it fresh — but on a minute
  // tick, not per render, or the bounds would churn on every keystroke elsewhere.
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 60_000);
    return () => clearInterval(id);
  }, []);

  // The combined picker below enforces its own bounds (retention window, max
  // span, never past `now`) before it ever calls this, so there's no separate
  // clamp-on-change step here the way the two split fields used to need.
  function handleRangeChange(nextStart: string, nextEnd: string) {
    setStartLocal(nextStart);
    setEndLocal(nextEnd);
  }

  const defaultStart = useMemo(() => new Date(Date.now() - 3_600_000).toISOString(), []);
  const defaultEnd = useMemo(() => new Date().toISOString(), []);

  const fetchStart = urlStart ?? defaultStart;
  const fetchEnd = urlEnd ?? defaultEnd;

  const { data, isLoading, isError, isFetching, error } = useActiveVehicles({
    start: fetchStart,
    end: fetchEnd,
    ...tripFiltersToQuery(appliedFilters),
    limit: pageSize,
    offset: (page - 1) * pageSize,
  });
  const isRateLimited = error instanceof ApiError && error.status === 429;
  // Cross-reference the live realtime feed so each row can show whether its
  // trip is still in progress or already complete.
  const live = useVehicles();
  // Static route list — the menu offers every route, not just the ones the
  // current query happened to count.
  const routes = useRoutes();

  const facets = data?.facets;

  const sortedRoutes = useMemo(() => {
    const list = routes.data?.routes ?? [];
    return [...list].sort((a, b) =>
      a.short_name.localeCompare(b.short_name, undefined, { numeric: true }),
    );
  }, [routes.data]);

  // The picker itself can no longer produce an invalid range, but a stale URL
  // (a bookmark, browser back/forward) still can — so keep the guard.
  const isValidRange = useMemo(() => {
    const s = fromLocalInput(startLocal);
    const e = fromLocalInput(endLocal);
    if (!s || !e || s >= e) return false;
    return e.getTime() - s.getTime() <= limits.maxSpanHours * 3_600_000;
  }, [startLocal, endLocal, limits.maxSpanHours]);

  const pushQuery = useCallback(
    (filters: TripFilters, startIso: string, endIso: string) => {
      const qs = new URLSearchParams({ start: startIso, end: endIso });
      for (const [k, v] of Object.entries(tripFiltersToParams(filters))) qs.set(k, v);
      router.push(`/trips?${qs}`);
    },
    [router],
  );

  /** "Load trips" — commits a new date/time window, keeping whatever filter
   *  set is already applied. */
  const handleLoad = useCallback(() => {
    const startIso = localInputToIso(startLocal);
    const endIso = localInputToIso(endLocal);
    if (!startIso || !endIso) return;
    setPage(1);
    setMenuOpen(false);
    pushQuery(appliedFilters, startIso, endIso);
  }, [appliedFilters, startLocal, endLocal, pushQuery]);

  /** "Apply filters" inside the menu — commits the draft immediately and
   *  tucks the menu away, keeping the current date/time window. */
  const applyFilters = useCallback(() => {
    setPage(1);
    setMenuOpen(false);
    pushQuery(draft, fetchStart, fetchEnd);
  }, [draft, fetchStart, fetchEnd, pushQuery]);

  /** Removing a chip / Reset / Clear is an unambiguous instruction too, so it
   *  applies straight away just like "Apply filters" does. */
  const applyImmediately = useCallback(
    (filters: TripFilters) => {
      setDraft(filters);
      setPage(1);
      pushQuery(filters, fetchStart, fetchEnd);
    },
    [fetchStart, fetchEnd, pushQuery],
  );

  function handleVehicleClick(v: ActiveVehicle) {
    if (!v.vehicle_label) return;
    const qs = new URLSearchParams();
    if (v.trip_id) qs.set("trip_id", v.trip_id);
    // Fetch the *full* leg: pad the trip's own start/end by a couple minutes so
    // the very first/last snapshot is captured, while still bounding to this
    // single occurrence of the trip_id.
    const pad = 2 * 60_000;
    qs.set("start", new Date(new Date(v.start_time).getTime() - pad).toISOString());
    qs.set("end", new Date(new Date(v.end_time).getTime() + pad).toISOString());
    // Preserve the list's whole query — window and filters — so the breadcrumb
    // returns to the view the row was clicked from, not just its date range.
    const ret = new URLSearchParams({ start: fetchStart, end: fetchEnd });
    for (const [k, val] of Object.entries(tripFiltersToParams(appliedFilters))) ret.set(k, val);
    qs.set("ret", ret.toString());
    router.push(`/trips/trip/${encodeURIComponent(v.vehicle_label)}?${qs}`);
  }

  const timeLabel = useMemo(() => {
    try {
      const s = new Date(fetchStart);
      const e = new Date(fetchEnd);
      const diffH = (e.getTime() - s.getTime()) / 3_600_000;
      if (diffH <= 1.5) {
        return `${formatDateTime(fetchStart)} – ${e.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
      }
      return `${formatDateTime(fetchStart)} – ${formatDateTime(fetchEnd)}`;
    } catch {
      return "";
    }
  }, [fetchStart, fetchEnd]);

  const routeNameOf = useCallback(
    (routeId: string) =>
      sortedRoutes.find((r) => r.route_id === routeId)?.short_name ??
      facets?.routes.find((r) => r.route_id === routeId)?.route_short_name ??
      undefined,
    [sortedRoutes, facets],
  );

  // Chips mirror the applied filter set — "Apply filters" commits straight
  // to the URL, so the chip row always matches what the table is showing.
  const chips = useMemo(
    () => tripFilterChips(appliedFilters, routeNameOf),
    [appliedFilters, routeNameOf],
  );

  const appliedCount = countActiveTripFilters(appliedFilters);

  const matched = data?.vehicle_count ?? 0;
  const windowTotal = data?.window_count ?? 0;
  // Export still speaks one route at a time, so only offer to scope it when the
  // filter set narrows to exactly that.
  const exportRouteId =
    appliedFilters.routeIds.length === 1 ? appliedFilters.routeIds[0] : undefined;

  const totalPages = Math.max(1, Math.ceil(matched / pageSize));

  // Type-a-page-number box. Held as its own string (not derived straight from
  // `page`) so a mid-edit value like "" or "1" while backspacing isn't fought
  // over by the effect that follows the current page — that effect only steps
  // in once `page` itself actually changes (e.g. via the arrow buttons).
  const [pageInput, setPageInput] = useState(String(page));
  useEffect(() => setPageInput(String(page)), [page]);

  function commitPageInput() {
    const n = Math.round(Number(pageInput));
    // Reject anything that isn't a real number (empty, "-", "abc", …) by
    // snapping back to the current page rather than guessing what was meant.
    if (!Number.isFinite(n) || pageInput.trim() === "") {
      setPageInput(String(page));
      return;
    }
    const clamped = Math.min(Math.max(n, 1), totalPages);
    setPage(clamped);
    setPageInput(String(clamped));
  }

  return (
    <div className="mx-auto w-full max-w-7xl space-y-6 px-4 pb-6 pt-24 text-fg">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-fg">Trip Explorer</h1>
          <p className="text-sm text-fg-subtle">
            {timeLabel ? `${timeLabel} · ` : ""}
            {/* "N of M" only when something was actually filtered out — a route
                filter runs in SQL, so it narrows both numbers equally. */}
            {isLoading
              ? "Loading…"
              : windowTotal > matched
                ? `${matched} of ${windowTotal} trips`
                : `${matched} trips`}
          </p>
        </div>
        <ExportButton routeId={exportRouteId} start={fetchStart} end={fetchEnd} />
      </div>

      {/* Filter bar */}
      <Card>
        <SectionHeading
          title="Filters"
          hint="Filters apply as soon as you hit Apply filters. Pick a date and time range and hit Load trips to change the window."
        />
        <div className="flex flex-wrap items-end gap-3">
          <DateRangePicker
            id="trips-range"
            start={startLocal}
            end={endLocal}
            onChange={handleRangeChange}
            limits={limits}
            now={now}
          />

          {/* Grouped so the filter button and Load trips move to their own
              line as a unit when the row wraps, rather than splitting up. */}
          <div className="flex items-stretch gap-3">
            <button
              type="button"
              onClick={() => setMenuOpen((o) => !o)}
              aria-expanded={menuOpen}
              aria-label="Filter trips"
              className={cn(
                "press flex h-9 items-center gap-1.5 rounded border px-2.5 text-sm font-medium transition-[transform,background-color,border-color,color] duration-150",
                appliedCount > 0 || menuOpen
                  ? "border-accent bg-accent/10 text-accent"
                  : "border-line bg-card text-fg-muted hover:border-line-strong hover:text-fg",
              )}
            >
              <FilterIcon className="h-4 w-4" />
              {appliedCount > 0 && (
                <span
                  key={appliedCount}
                  className="animate-badge-pop rounded-full bg-accent px-1.5 py-0.5 text-[10px] font-bold leading-none text-accent-ink"
                >
                  {appliedCount}
                </span>
              )}
            </button>

            <button
              onClick={handleLoad}
              disabled={!isValidRange}
              className={cn(
                "press h-9 rounded px-4 text-sm font-medium transition-[transform,opacity,background-color,color] duration-150",
                isValidRange
                  ? "bg-accent text-accent-ink hover:opacity-90"
                  : "cursor-not-allowed bg-raised text-fg-subtle",
              )}
            >
              Load trips
            </button>
          </div>
        </div>

        <TripFilterMenu
          open={menuOpen}
          value={draft}
          onChange={setDraft}
          facets={facets}
          routes={sortedRoutes}
          onReset={() => applyImmediately(EMPTY_TRIP_FILTERS)}
          onApply={applyFilters}
          applyDisabled={tripFiltersEqual(draft, appliedFilters)}
        />

        {chips.length > 0 && (
          <div className="mt-4 flex flex-wrap items-center gap-1.5 border-t border-line pt-3">
            <span className="mr-1 text-xs font-medium text-fg-subtle">Filtering by</span>
            {chips.map((chip) => (
              <ActiveFilterChip
                key={chip.id}
                label={chip.label}
                onRemove={() => applyImmediately(chip.next)}
              />
            ))}
            <button
              type="button"
              onClick={() => applyImmediately(EMPTY_TRIP_FILTERS)}
              className="ml-1 text-xs font-medium text-fg-subtle underline-offset-2 transition-colors hover:text-fg hover:underline"
            >
              Clear all
            </button>
          </div>
        )}
      </Card>

      {/* Vehicles table */}
      <Card>
        <SectionHeading
          title="Vehicles"
          hint="Click a row to see the vehicle's stop-by-stop timeline."
        />

        {isLoading && <LoadingSpinner />}

        {isError && (
          <p className="py-8 text-center text-sm text-danger">
            {isRateLimited
              ? "You've sent too many requests. Please wait a moment and try again."
              : "Failed to load vehicles. The time window may be out of range."}
          </p>
        )}

        {!isLoading && !isError && data?.vehicles.length === 0 && (
          <div className="py-8 text-center">
            <p className="text-sm text-fg-subtle">
              {appliedCount > 0 && windowTotal > 0
                ? `None of the ${windowTotal} trips in this window match these filters.`
                : "No vehicle data found for this time window."}
            </p>
            {appliedCount > 0 && (
              <button
                type="button"
                onClick={() => applyImmediately(EMPTY_TRIP_FILTERS)}
                className="mt-2 text-sm font-medium text-accent underline-offset-2 hover:underline"
              >
                Clear filters
              </button>
            )}
          </div>
        )}

        {!isLoading && !isError && (data?.vehicles.length ?? 0) > 0 && (
          <>
            {/* Dimmed, not replaced, while the next page or filter set loads —
                the rows underneath are still the ones that were asked for. */}
            <div
              className={cn(
                "overflow-x-auto rounded border border-line transition-opacity duration-200",
                isFetching && "opacity-60",
              )}
            >
              {/* table-fixed + an explicit width on every column but From → To: that
                  one column is left unset, so the fixed-layout algorithm hands it
                  whatever space the others don't need — it's the only column that
                  grows or shrinks (and truncates) as the table is resized. Every
                  fixed width carries a few px of slack beyond its own content's
                  measured size: nowrap content can't shrink to fit like From → To
                  can, so a column sized exactly to its content — no margin — will
                  force the whole table a hair wider than 100% the moment a browser
                  renders that text even fractionally wider than expected, which
                  reads as a barely-there permanent scrollbar. border-collapse is
                  explicit rather than trusted to Preflight, for the same reason:
                  belt and suspenders against a few stray px. The table's min-width
                  is the sum of the fixed columns plus a floor for From → To, so
                  squeezing below that hands off to the wrapper's horizontal scroll
                  instead of crushing every column to fit. Columns and padding both
                  shrink a step on mobile (same floor for From → To either way) so
                  the handoff to scroll happens later, matching the compact tables
                  elsewhere in the app. */}
              <table className="w-full min-w-[832px] table-fixed border-collapse text-xs text-fg-muted sm:min-w-[976px] sm:text-sm">
                <thead className="bg-raised text-[10px] uppercase text-fg-subtle sm:text-xs">
                  <tr>
                    <th className="w-8 whitespace-nowrap px-2 py-1.5 text-left sm:w-10 sm:px-3 sm:py-2">
                      <span className="sr-only">Status</span>
                    </th>
                    <th className="w-14 whitespace-nowrap px-2 py-1.5 text-left sm:w-16 sm:px-3 sm:py-2">
                      Route
                    </th>
                    <th className="w-16 whitespace-nowrap px-2 py-1.5 text-left sm:w-20 sm:px-3 sm:py-2">
                      Vehicle
                    </th>
                    <th className="whitespace-nowrap px-2 py-1.5 text-left sm:px-3 sm:py-2">From → To</th>
                    <th className="w-20 whitespace-nowrap px-2 py-1.5 text-left sm:w-24 sm:px-3 sm:py-2">
                      <span className="sm:hidden">Start</span>
                      <span className="hidden sm:inline">Start Time</span>
                    </th>
                    <th className="w-20 whitespace-nowrap px-2 py-1.5 text-left sm:w-24 sm:px-3 sm:py-2">
                      <span className="sm:hidden">End</span>
                      <span className="hidden sm:inline">End Time</span>
                    </th>
                    <th className="w-16 whitespace-nowrap px-2 py-1.5 text-right sm:w-20 sm:px-3 sm:py-2">
                      <span className="sm:hidden">Dur.</span>
                      <span className="hidden sm:inline">Duration</span>
                    </th>
                    <th className="w-[104px] whitespace-nowrap px-2 py-1.5 text-left sm:w-[136px] sm:px-3 sm:py-2">
                      <span className="sm:hidden">Occ.</span>
                      <span className="hidden sm:inline">Occupancy</span>
                    </th>
                    <th className="w-[72px] whitespace-nowrap px-2 py-1.5 text-right sm:w-[88px] sm:px-3 sm:py-2">
                      <span className="sm:hidden">Delay</span>
                      <span className="hidden sm:inline">Avg Delay</span>
                    </th>
                    <th className="w-16 whitespace-nowrap px-2 py-1.5 text-right sm:w-20 sm:px-3 sm:py-2">
                      On-Time
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {data!.vehicles.map((v, i) => (
                    <tr
                      key={`${v.vehicle_label ?? ""}-${v.trip_id ?? i}`}
                      className="cursor-pointer hover:bg-accent/10"
                      onClick={() => handleVehicleClick(v)}
                    >
                      <td className="w-8 px-2 py-1.5 sm:w-10 sm:px-3 sm:py-2">
                        {/* The API decided the status the filter matched on; the
                            live feed is fresher, so a trip that is still running
                            keeps its blinking dot between refetches. */}
                        <TripStatusBadge
                          variant="dot"
                          status={computeTripStatus(
                            v.in_progress ||
                              isTripInProgress(
                                live.data?.vehicles,
                                v.vehicle_label,
                                v.trip_id,
                                v.end_time,
                              ),
                            v.reached_terminus,
                          )}
                        />
                      </td>
                      <td className="w-14 whitespace-nowrap px-2 py-1.5 sm:w-16 sm:px-3 sm:py-2">
                        <RouteBadge shortName={v.route_short_name} color={v.route_color} />
                      </td>
                      <td className="w-16 whitespace-nowrap px-2 py-1.5 font-semibold sm:w-20 sm:px-3 sm:py-2">
                        {v.vehicle_label ? `#${v.vehicle_label}` : "—"}
                      </td>
                      <td className="overflow-hidden px-2 py-1.5 text-fg-muted sm:px-3 sm:py-2">
                        <span className="block truncate" title={`${v.start_stop_name ?? "—"} → ${v.end_stop_name ?? "—"}`}>
                          {v.start_stop_name ?? "—"}
                          <span className="px-1 text-fg-subtle">→</span>
                          {v.end_stop_name ?? "—"}
                        </span>
                      </td>
                      {/* whitespace-nowrap: the locale time string can wrap its AM/PM
                          onto its own line at some column widths, which grows the row
                          to two lines and jitters the table height between pages. */}
                      <td className="w-20 whitespace-nowrap px-2 py-1.5 text-fg-muted sm:w-24 sm:px-3 sm:py-2">
                        {new Date(v.start_time).toLocaleTimeString([], {
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                      </td>
                      <td className="w-20 whitespace-nowrap px-2 py-1.5 text-fg-muted sm:w-24 sm:px-3 sm:py-2">
                        {new Date(v.end_time).toLocaleTimeString([], {
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                      </td>
                      <td className="w-16 whitespace-nowrap px-2 py-1.5 text-right font-mono text-fg-muted sm:w-20 sm:px-3 sm:py-2">
                        {formatDuration(v.duration_minutes)}
                      </td>
                      <td className="w-[104px] whitespace-nowrap px-2 py-1.5 text-fg-muted sm:w-[136px] sm:px-3 sm:py-2">
                        {v.last_occupancy_status
                          ? occupancyLabel(v.last_occupancy_status)
                          : "—"}
                      </td>
                      <td
                        className={cn(
                          "w-[72px] whitespace-nowrap px-2 py-1.5 text-right font-mono sm:w-[88px] sm:px-3 sm:py-2",
                          avgDelayColor(v.avg_delay_seconds),
                        )}
                      >
                        {formatDelayMin(v.avg_delay_seconds)}
                      </td>
                      <td className="w-16 whitespace-nowrap px-2 py-1.5 text-right font-mono text-fg-muted sm:w-20 sm:px-3 sm:py-2">
                        {v.on_time_pct !== null ? `${Math.round(v.on_time_pct)}%` : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Pagination controls */}
            {(() => {
              const totalCount = matched;
              const start = (page - 1) * pageSize + 1;
              const end = Math.min(page * pageSize, totalCount);
              return (
                <div className="mt-3 flex flex-wrap items-center justify-between gap-3 text-sm text-fg-muted">
                  <span>
                    {start}–{end} of {totalCount} trips
                  </span>
                  {/* Full width and right-justified on mobile — with flex-wrap,
                      a lone wrapped item otherwise sits at the row's start
                      rather than lining up under the trip count on the right. */}
                  <div className="flex w-full items-center justify-between gap-3 sm:w-auto sm:justify-start">
                    <div className="flex items-center gap-1.5">
                      {/* Smaller on mobile — "Rows per page" is the thing that was
                          getting squeezed against the page buttons in the same row. */}
                      <label className="text-[10px] text-fg-subtle sm:text-xs">Rows per page</label>
                      <select
                        value={pageSize}
                        onChange={(e) => {
                          setPageSize(Number(e.target.value));
                          setPage(1);
                        }}
                        className="rounded border border-line bg-card px-2 py-1 text-xs text-fg focus:outline-none focus:ring-2 focus:ring-accent"
                      >
                        {PAGE_SIZE_OPTIONS.map((n) => (
                          <option key={n} value={n}>{n}</option>
                        ))}
                      </select>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <button
                        onClick={() => setPage((p) => Math.max(1, p - 1))}
                        disabled={page === 1}
                        aria-label="Previous page"
                        className="press flex h-7 w-7 items-center justify-center rounded border border-line disabled:cursor-not-allowed disabled:opacity-40 hover:bg-raised"
                      >
                        <ChevronLeftIcon />
                      </button>
                      <span className="flex items-center gap-1 px-1 text-xs">
                        {/* Dropped on mobile — the row was already tight with the
                            input and both arrow buttons, and "of Y" carries the
                            total on its own. */}
                        <span className="hidden sm:inline">Page</span>
                        <input
                          type="text"
                          inputMode="numeric"
                          value={pageInput}
                          onChange={(e) => setPageInput(e.target.value)}
                          onBlur={commitPageInput}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") {
                              commitPageInput();
                              e.currentTarget.blur();
                            }
                          }}
                          aria-label={`Go to page, ${totalPages} total pages`}
                          className="w-9 rounded border border-line bg-card px-1 py-0.5 text-center text-xs text-fg focus:outline-none focus:ring-2 focus:ring-accent"
                        />
                        <span>of {totalPages}</span>
                      </span>
                      <button
                        onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                        disabled={page >= totalPages}
                        aria-label="Next page"
                        className="press flex h-7 w-7 items-center justify-center rounded border border-line disabled:cursor-not-allowed disabled:opacity-40 hover:bg-raised"
                      >
                        <ChevronRightIcon />
                      </button>
                    </div>
                  </div>
                </div>
              );
            })()}
          </>
        )}
      </Card>
    </div>
  );
}

export default function TripsPage() {
  return (
    <Suspense
      fallback={
        <div className="mx-auto w-full max-w-7xl px-4 pb-6 pt-24">
          <p className="text-sm text-fg-subtle">Loading…</p>
        </div>
      }
    >
      <TripsContent />
    </Suspense>
  );
}
