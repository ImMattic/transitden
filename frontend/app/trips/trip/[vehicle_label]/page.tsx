"use client";
import dynamic from "next/dynamic";
import { Suspense, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import { useVehicleTrip, useVehicles } from "@/lib/hooks";
import { tripExportUrl } from "@/lib/api";
import { usePlayback } from "@/lib/usePlayback";
import { Card, SectionHeading } from "@/components/ui/Card";
import LoadingSpinner from "@/components/ui/LoadingSpinner";
import TripPlaybackControls from "@/components/map/TripPlaybackControls";
import TripStatusBadge from "@/components/ui/TripStatusBadge";
import { computeTripStatus, formatDelay, formatDelayMin, isTripInProgress, routeColor } from "@/lib/utils";
import type { SignalGap, VehicleStopEvent } from "@/lib/types";

const VehicleTripMap = dynamic(() => import("@/components/map/VehicleTripMap"), {
  ssr: false,
  loading: () => <div className="h-full animate-pulse rounded bg-raised" />,
});

const OCCUPANCY_LABELS: Record<string, string> = {
  EMPTY: "Empty",
  MANY_SEATS_AVAILABLE: "Many seats",
  FEW_SEATS_AVAILABLE: "Few seats",
  STANDING_ROOM_ONLY: "Standing room",
  CRUSHED_STANDING_ROOM_ONLY: "Crushed",
  FULL: "Full",
  NOT_ACCEPTING_PASSENGERS: "Not accepting",
  UNKNOWN: "—",
};

function delayBadge(seconds: number): string {
  if (seconds > 600) return "status-danger";
  if (seconds > 300) return "status-warn";
  if (seconds < -300) return "status-info";
  return "status-ok";
}

function StatBox({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-lg border border-line bg-card p-4 shadow-card">
      <p className="text-xs text-fg-subtle">{label}</p>
      <p className="mt-1 text-2xl font-bold text-fg">{value}</p>
      {sub && <p className="mt-0.5 text-xs text-fg-subtle">{sub}</p>}
    </div>
  );
}

function hhmm(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function formatGapDuration(seconds: number): string {
  const minutes = Math.round(seconds / 60);
  return minutes < 1 ? `${Math.round(seconds)}s` : `${minutes} min`;
}

/**
 * Banner for a run whose feed went silent long enough that on-time detection
 * itself stops trusting a straight-line guess between the two fixes on
 * either side (trip_signal_gap_seconds, mirroring arrival_segment_max_gap_seconds
 * on the backend) — most often a cell dead zone on a remote leg (e.g. NB2).
 * Position during the gap is unknown, not merely uncoloured.
 */
function SignalGapBanner({ gaps }: { gaps: SignalGap[] }) {
  if (gaps.length === 0) return null;
  return (
    <div className="status-warn rounded px-4 py-3 text-sm">
      <p className="font-medium">
        {gaps.length === 1 ? "Signal gap detected" : `${gaps.length} signal gaps detected`}
      </p>
      <p className="mt-0.5 text-xs">
        No position reported for {gaps.map((g, i) => (
          <span key={`${g.start}-${g.end}`}>
            {i > 0 && ", "}
            {formatGapDuration(g.duration_seconds)} between {hhmm(g.start)} and {hhmm(g.end)}
          </span>
        ))}
        {" — likely lost cell signal. The path shown between those times is a straight-line estimate, not a measurement."}
      </p>
    </div>
  );
}

/** Fill colour for a solid (observed) timeline node. */
function nodeFill(seconds: number | null): string {
  if (seconds === null) return "bg-fg-subtle";
  if (seconds > 600) return "bg-danger";
  if (seconds > 300) return "bg-warn";
  if (seconds < -300) return "bg-accent";
  return "bg-ok";
}

/** Routes with this many stops or fewer show every stop; no condensing. */
const MAX_FULL_STOPS = 20;
/** Above that, condense down to this many equidistant "anchor" stops. */
const CONDENSED_STOP_COUNT = 10;

function anchorKeyFor(stop: VehicleStopEvent): string {
  return `${stop.stop_id}-${stop.stop_sequence}`;
}

function haversineMeters(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const R = 6371000;
  const toRad = (d: number) => (d * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

/**
 * Picks up to `count` indices from `pool` (a sorted subset of stop indices
 * that always includes the first and last stop) whose cumulative
 * straight-line distance along the full stop sequence is as close to evenly
 * spaced as the pool allows. Falls back to even spacing through the pool if
 * coordinates are missing (or the route has no measurable length).
 */
function selectEquidistantFromPool(
  pool: number[],
  cumulative: number[],
  count: number,
): number[] {
  const m = pool.length;
  if (m <= count) return pool;

  const total = cumulative[pool[m - 1]];
  const pickedPool: number[] = [];
  for (let i = 0; i < count; i++) {
    let bestJ = 0;
    if (total > 0) {
      const target = (total * i) / (count - 1);
      let bestDiff = Infinity;
      for (let j = 0; j < m; j++) {
        const diff = Math.abs(cumulative[pool[j]] - target);
        if (diff < bestDiff) {
          bestDiff = diff;
          bestJ = j;
        }
      }
    } else {
      bestJ = Math.round((i * (m - 1)) / (count - 1));
    }
    // Keep picks strictly increasing, and leave enough room for the picks still to come.
    const minJ = pickedPool.length ? pickedPool[pickedPool.length - 1] + 1 : 0;
    const maxJ = m - 1 - (count - 1 - i);
    pickedPool.push(Math.min(Math.max(bestJ, minJ), maxJ));
  }
  pickedPool[0] = 0;
  pickedPool[pickedPool.length - 1] = m - 1;
  return pickedPool.map((j) => pool[j]);
}

/**
 * Chooses which stops become the condensed view's "anchor" stops. Only RTD
 * timepoints ever get a geofenced arrival (see `stop_arrival_events` on the
 * backend), so a non-timepoint anchor could never show a status colour — the
 * pool is restricted to timepoints (plus the origin/terminus, forced in
 * regardless of their timepoint flag) so every anchor is capable of showing
 * one once the vehicle reaches it.
 */
function selectEquidistantIndices(stops: VehicleStopEvent[], count: number): number[] {
  const n = stops.length;
  if (n <= count) return stops.map((_, i) => i);

  const cumulative = new Array<number>(n).fill(0);
  for (let i = 1; i < n; i++) {
    const a = stops[i - 1];
    const b = stops[i];
    const d =
      a.stop_lat != null && a.stop_lon != null && b.stop_lat != null && b.stop_lon != null
        ? haversineMeters(a.stop_lat, a.stop_lon, b.stop_lat, b.stop_lon)
        : 0;
    cumulative[i] = cumulative[i - 1] + d;
  }

  const pool = stops
    .map((s, i) => i)
    .filter((i) => i === 0 || i === n - 1 || stops[i].is_timepoint);

  return selectEquidistantFromPool(pool, cumulative, count);
}

interface TimelineRow {
  stop: VehicleStopEvent;
  anchorKey: string;
  /** Stops collapsed under this row's caret; 0 for rows that aren't anchors. */
  childCount: number;
  expanded: boolean;
}

/**
 * Builds the rows the Stop Timeline renders. Routes with more than
 * MAX_FULL_STOPS stops are condensed to up to CONDENSED_STOP_COUNT equidistant
 * timepoint "anchor" stops (always including origin + terminus); the stops in
 * between two anchors collapse under the earlier anchor's drop-down caret
 * until the user expands it (or the "Show all stops" toggle expands every
 * anchor).
 */
function buildTimelineRows(
  stops: VehicleStopEvent[],
  expandedAnchors: Set<string>,
): TimelineRow[] {
  if (stops.length <= MAX_FULL_STOPS) {
    return stops.map((stop) => ({
      stop,
      anchorKey: anchorKeyFor(stop),
      childCount: 0,
      expanded: false,
    }));
  }

  const anchorIndices = selectEquidistantIndices(stops, CONDENSED_STOP_COUNT);
  const rows: TimelineRow[] = [];
  for (let i = 0; i < anchorIndices.length; i++) {
    const anchorIdx = anchorIndices[i];
    const nextAnchorIdx = anchorIndices[i + 1] ?? stops.length;
    const anchor = stops[anchorIdx];
    const key = anchorKeyFor(anchor);
    const children = stops.slice(anchorIdx + 1, nextAnchorIdx);
    const expanded = children.length > 0 && expandedAnchors.has(key);
    rows.push({ stop: anchor, anchorKey: key, childCount: children.length, expanded });
    if (expanded) {
      for (const child of children) {
        rows.push({ stop: child, anchorKey: anchorKeyFor(child), childCount: 0, expanded: false });
      }
    }
  }
  return rows;
}

function ChevronIcon({ expanded }: { expanded: boolean }) {
  return (
    <svg
      className={`h-3 w-3 shrink-0 transition-transform ${expanded ? "rotate-180" : ""}`}
      viewBox="0 0 20 20"
      fill="currentColor"
      aria-hidden="true"
    >
      <path
        fillRule="evenodd"
        d="M5.23 7.21a.75.75 0 011.06.02L10 11.168l3.71-3.938a.75.75 0 111.08 1.04l-4.25 4.5a.75.75 0 01-1.08 0l-4.25-4.5a.75.75 0 01.02-1.06z"
        clipRule="evenodd"
      />
    </svg>
  );
}

/**
 * Stop-by-stop schedule for the trip's direction. Short routes (≤20 stops)
 * show every stop; longer routes condense to equidistant anchor stops with a
 * drop-down caret to reveal the stops collapsed between two anchors — see
 * `buildTimelineRows`. Stops the vehicle was geofenced at ("tracked") carry
 * an actual time + delay; the rest show the schedule only. The origin's
 * actual time is when the vehicle *left*, not when it first showed up there
 * — see `event_type` on VehicleStopEvent.
 */
function StopTimeline({
  rows,
  onHover,
  onToggleAnchor,
}: {
  rows: TimelineRow[];
  onHover: (s: VehicleStopEvent | null) => void;
  onToggleAnchor: (anchorKey: string) => void;
}) {
  return (
    <ol className="max-h-[560px] overflow-y-auto overflow-x-hidden pr-1">
      {rows.map((row, i) => {
        const { stop } = row;
        const isFirst = i === 0;
        const isLast = i === rows.length - 1;
        const terminus = isFirst ? "Origin" : isLast ? "Terminus" : null;
        return (
          <li
            key={row.anchorKey}
            className="group flex gap-1.5 rounded px-1.5 hover:bg-raised"
            onMouseEnter={() => onHover(stop)}
            onMouseLeave={() => onHover(null)}
          >
            {/* Rail */}
            <div className="flex w-3 shrink-0 flex-col items-center">
              <span className={`w-px ${isFirst ? "h-3" : "h-3 bg-line-strong"}`} />
              <span
                className={
                  stop.observed
                    ? `${stop.is_timepoint ? "h-3.5 w-3.5" : "h-2.5 w-2.5"} rounded-full ${nodeFill(
                        stop.delay_seconds,
                      )} ring-2 ring-card`
                    : `${
                        stop.is_timepoint ? "h-3 w-3 border-fg-subtle" : "h-2.5 w-2.5 border-line-strong"
                      } rounded-full border-2 bg-card`
                }
              />
              <span className={`w-px flex-1 ${isLast ? "" : "bg-line-strong"}`} />
            </div>

            {/* Drop-down caret — fixed-width slot so stop names still line up
                on rows that don't have one. */}
            <div className="flex w-4 shrink-0 items-start pt-[7px]">
              {row.childCount > 0 && (
                <button
                  type="button"
                  onClick={() => onToggleAnchor(row.anchorKey)}
                  aria-expanded={row.expanded}
                  aria-label={row.expanded ? "Hide stops between" : "Show stops between"}
                  className="rounded text-fg-subtle hover:text-fg-muted"
                >
                  <ChevronIcon expanded={row.expanded} />
                </button>
              )}
            </div>

            {/* Content */}
            <div className="flex min-w-0 flex-1 items-start justify-between gap-2 py-2 sm:gap-3">
              <div className="min-w-0">
                <p
                  className={`truncate text-[13px] sm:text-sm ${
                    stop.is_timepoint ? "font-semibold text-fg" : "font-medium text-fg-muted"
                  }`}
                >
                  {stop.stop_name ?? stop.stop_id}
                </p>
                <p className="mt-0.5 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[10px] text-fg-subtle sm:text-[11px]">
                  <span>#{stop.stop_sequence}</span>
                  {terminus && (
                    <span className="rounded bg-raised px-1 py-px font-medium uppercase tracking-wide text-fg-muted">
                      {terminus}
                    </span>
                  )}
                  {stop.observed && stop.occupancy_status && stop.occupancy_status !== "UNKNOWN" && (
                    <span>· {OCCUPANCY_LABELS[stop.occupancy_status] ?? stop.occupancy_status}</span>
                  )}
                </p>
              </div>

              {/* Arrival time reads as plain (high-contrast) text; the delay
                  is what gets the status-tinted badge beside it. */}
              <div className="min-w-0 shrink-0 text-right">
                {stop.observed ? (
                  <>
                    <div className="flex flex-wrap items-center justify-end gap-x-1 gap-y-0.5">
                      <span
                        className="shrink-0 text-sm font-semibold tabular-nums text-fg"
                        title={
                          stop.detection_method === "terminus_fallback"
                            ? "Estimated: the feed stopped reporting on approach, so this is the last sighting near the stop — the vehicle arrived at or after this time."
                            : undefined
                        }
                      >
                        {stop.detection_method === "terminus_fallback" ? "≥ " : ""}
                        {hhmm(stop.actual_time)}
                      </span>
                      <span
                        className={`inline-block shrink-0 rounded-full px-1 py-0.5 text-[9px] font-medium tabular-nums sm:px-1.5 sm:text-[11px] ${delayBadge(
                          stop.delay_seconds ?? 0,
                        )}`}
                      >
                        {formatDelay(stop.delay_seconds) || "on time"}
                      </span>
                    </div>
                    <div className="mt-0.5 text-[10px] tabular-nums text-fg-subtle sm:text-[11px]">
                      {stop.event_type === "departure" ? "departed · sched " : "sched "}
                      {hhmm(stop.scheduled_time)}
                    </div>
                  </>
                ) : (
                  <>
                    <div className="text-[13px] tabular-nums text-fg-muted sm:text-sm">
                      {hhmm(stop.scheduled_time)}
                    </div>
                    {/* Only RTD timepoints are geofenced, which is every rail
                        station but under a fifth of bus stops. Saying so stops
                        an ordinary bus stop from reading as a missed arrival. */}
                    <div
                      className="mt-0.5 text-[10px] text-fg-subtle sm:text-[11px]"
                      title={
                        stop.is_timepoint === false
                          ? "RTD does not time this stop, so no arrival is recorded here."
                          : undefined
                      }
                    >
                      {stop.is_timepoint === false ? "not timed" : "scheduled"}
                    </div>
                  </>
                )}
              </div>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

/** Expands/collapses every condensed anchor's caret at once. */
function ShowAllStopsToggle({
  checked,
  onChange,
}: {
  checked: boolean;
  onChange: () => void;
}) {
  return (
    <label className="flex shrink-0 select-none items-center gap-2 whitespace-nowrap text-xs text-fg-muted">
      <span>Show all stops</span>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={onChange}
        className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${
          checked ? "bg-accent" : "bg-line-strong"
        }`}
      >
        <span
          className={`inline-block h-3.5 w-3.5 transform rounded-full bg-card shadow transition-transform ${
            checked ? "translate-x-[18px]" : "translate-x-1"
          }`}
        />
      </button>
    </label>
  );
}

const RAIL_TYPES = new Set(["0", "1", "2"]);

function TripDetailContent({ vehicleLabel }: { vehicleLabel: string }) {
  const searchParams = useSearchParams();
  const tripId = searchParams.get("trip_id") ?? undefined;
  const [hoveredStop, setHoveredStop] = useState<VehicleStopEvent | null>(null);
  const [expandedAnchors, setExpandedAnchors] = useState<Set<string>>(new Set());
  // Falls back to the trip id the backend resolved, for the (rare) direct
  // link that lands here without one in the URL — see the effect below.
  const [resolvedTripId, setResolvedTripId] = useState<string | undefined>(undefined);
  // start/end bound the full extent of this single trip leg (set by the list).
  const start = searchParams.get("start") ?? undefined;
  const end = searchParams.get("end") ?? undefined;

  // Cross-reference the live realtime feed (same one the map page polls) to
  // tell whether this leg is still being tracked right now.
  const live = useVehicles();
  const effectiveTripId = tripId ?? resolvedTripId;
  // This leg's last-seen time, which is what separates "still running" from
  // "matches something running now". Neither a vehicle label nor a GTFS trip id
  // is scoped to a date, and RTD labels rail vehicles with a run number reused
  // every day, so yesterday's train matches the live feed exactly. Getting that
  // wrong drops the `end` bound below and asks the API for a window wider than
  // it will serve — a 422, and the page renders "Failed to load trip data".
  //
  // Seeded from the bound the list put in the URL, then advanced by each fetch
  // so a genuinely live trip stays live instead of ageing out of the window.
  const [lastSeen, setLastSeen] = useState<string | undefined>(end);
  const isInProgress = isTripInProgress(
    live.data?.vehicles,
    vehicleLabel,
    effectiveTripId,
    lastSeen,
  );

  const { data, isLoading, isError } = useVehicleTrip(
    vehicleLabel,
    // While in progress, drop the fixed `end` bound so every poll re-asks the
    // backend for "up to now" and new stops/positions stream in.
    { trip_id: tripId, start, end: isInProgress ? undefined : end },
    { live: isInProgress },
  );

  useEffect(() => {
    if (data?.trip_id) setResolvedTripId(data.trip_id);
  }, [data?.trip_id]);

  const newestPosition = data?.positions.length
    ? data.positions[data.positions.length - 1].timestamp
    : undefined;
  useEffect(() => {
    if (newestPosition) setLastSeen(newestPosition);
  }, [newestPosition]);

  // The breadcrumb returns to the originating list view — its window *and* its
  // filters — not this leg's bounds. `ret` carries the whole query the list was
  // showing; the older per-field params still work for links already out there.
  const ret = searchParams.get("ret");
  const retStart = searchParams.get("ret_start");
  const retEnd = searchParams.get("ret_end");
  const retRouteId = searchParams.get("ret_route_id");
  const backQs = new URLSearchParams();
  if (retStart) backQs.set("start", retStart);
  if (retEnd) backQs.set("end", retEnd);
  if (retRouteId) backQs.set("route_id", retRouteId);
  const backSearch = ret || backQs.toString();
  const backHref = `/trips${backSearch ? `?${backSearch}` : ""}`;

  const routeHex = routeColor(data?.route_color ?? "888888");

  const lastOccupancy = data?.positions.length
    ? data.positions[data.positions.length - 1].occupancy_status
    : null;

  const observedStopCount =
    data?.observed_stop_count ?? data?.stops.filter((s) => s.observed).length ?? 0;

  const stops = data?.stops ?? [];
  const isCondensed = stops.length > MAX_FULL_STOPS;
  // A trip with no known schedule can't be assessed, so default to "reached"
  // rather than flagging a data gap as an incident — see reached_terminus on
  // the trips-list endpoint for the same convention.
  const reachedTerminus = stops.length > 0 ? stops[stops.length - 1].observed === true : true;
  const tripStatus = computeTripStatus(isInProgress, reachedTerminus);

  // Reset drill-down state whenever a different trip loads.
  useEffect(() => {
    setExpandedAnchors(new Set());
  }, [data?.trip_id]);

  const timelineRows = useMemo(() => buildTimelineRows(stops, expandedAnchors), [stops, expandedAnchors]);
  // What the map should plot: anchor stops, plus any drilled-down stops the
  // table currently has expanded.
  const visibleStops = useMemo(() => timelineRows.map((row) => row.stop), [timelineRows]);
  const expandableAnchors = useMemo(
    () => timelineRows.filter((row) => row.childCount > 0),
    [timelineRows],
  );
  const allStopsShown =
    expandableAnchors.length > 0 && expandableAnchors.every((row) => row.expanded);

  const toggleAnchor = (anchorKey: string) => {
    setExpandedAnchors((prev) => {
      const next = new Set(prev);
      if (next.has(anchorKey)) next.delete(anchorKey);
      else next.add(anchorKey);
      return next;
    });
  };
  const toggleAllStops = () => {
    setExpandedAnchors(
      allStopsShown ? new Set() : new Set(expandableAnchors.map((row) => row.anchorKey)),
    );
  };

  // Prefer the trip's actual extent (first→last snapshot) over the padded
  // query bounds for the header timestamp.
  const tripStart = data?.positions.length ? data.positions[0].timestamp : start;
  const tripEnd = data?.positions.length
    ? data.positions[data.positions.length - 1].timestamp
    : end;

  // Playback clock for the Trip Track map (epoch ms bounds from the position track).
  const playbackStartMs = data?.positions.length ? Date.parse(data.positions[0].timestamp) : 0;
  const playbackEndMs = data?.positions.length
    ? Date.parse(data.positions[data.positions.length - 1].timestamp)
    : 0;
  const playback = usePlayback(playbackStartMs, playbackEndMs);

  return (
    <div className="mx-auto w-full max-w-7xl space-y-6 px-4 pb-6 pt-24 text-fg">
      {/* Breadcrumb */}
      <div className="flex items-center gap-2 text-sm text-fg-subtle">
        <Link href={backHref} className="hover:text-accent">
          Trips
        </Link>
        <span>/</span>
        <span className="text-fg-muted">#{vehicleLabel}</span>
      </div>

      {/* Header */}
      <div>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-bold text-fg">Vehicle #{vehicleLabel}</h1>
          {data?.route_short_name && (
            <span
              className="rounded px-2.5 py-1 text-sm font-bold text-white"
              style={{ backgroundColor: routeHex }}
            >
              Route {data.route_short_name}
            </span>
          )}
          {data && <TripStatusBadge status={tripStatus} />}
          {data && (
            // Same window as the fetch above, so the file matches the screen.
            // Includes the raw rows the page hides (anomalous fixes, trip
            // updates, other vehicles on this trip_id) for offline diagnosis.
            <a
              href={tripExportUrl(vehicleLabel, {
                trip_id: effectiveTripId,
                start,
                end: isInProgress ? undefined : end,
              })}
              download
              className="ml-auto inline-flex items-center gap-1.5 rounded border border-line px-2.5 py-1 text-xs font-medium text-fg-muted transition-colors hover:border-line-strong hover:text-fg"
              title="Download this trip's raw positions, arrivals, trip updates, schedule and detection settings as JSON"
            >
              <svg className="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                <path d="M10.75 2.75a.75.75 0 00-1.5 0v8.614L6.295 8.235a.75.75 0 10-1.09 1.03l4.25 4.5a.75.75 0 001.09 0l4.25-4.5a.75.75 0 00-1.09-1.03l-2.955 3.129V2.75z" />
                <path d="M3.5 12.75a.75.75 0 00-1.5 0v2.5A2.75 2.75 0 004.75 18h10.5A2.75 2.75 0 0018 15.25v-2.5a.75.75 0 00-1.5 0v2.5c0 .69-.56 1.25-1.25 1.25H4.75c-.69 0-1.25-.56-1.25-1.25v-2.5z" />
              </svg>
              Export trip data
            </a>
          )}
        </div>
        {data?.route_long_name && (
          <p className="mt-0.5 text-sm text-fg-subtle">{data.route_long_name}</p>
        )}
        {(tripStart || tripEnd) && (
          <p className="mt-0.5 text-xs text-fg-subtle">
            {tripStart
              ? new Date(tripStart).toLocaleString([], {
                  dateStyle: "medium",
                  timeStyle: "short",
                })
              : ""}
            {tripStart && tripEnd ? " – " : ""}
            {tripEnd
              ? new Date(tripEnd).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
              : ""}
          </p>
        )}
      </div>

      {isLoading && <LoadingSpinner />}
      {isError && (
        <p className="status-danger rounded px-4 py-3 text-sm">
          Failed to load trip data.
        </p>
      )}

      {!isLoading && !isError && data && (
        <>
          <SignalGapBanner gaps={data.signal_gaps} />

          {/* Stat strip */}
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <StatBox
              label="Avg Delay"
              value={
                data.avg_delay_seconds !== null ? formatDelayMin(data.avg_delay_seconds) : "—"
              }
              sub={
                observedStopCount
                  ? `across ${observedStopCount} tracked stop${observedStopCount === 1 ? "" : "s"}`
                  : "no tracked stops"
              }
            />
            <StatBox
              label="On-Time Rate"
              value={data.on_time_pct !== null ? `${data.on_time_pct}%` : "—"}
              sub="±5 min window"
            />
            <StatBox
              label="Occupancy"
              value={OCCUPANCY_LABELS[lastOccupancy ?? "UNKNOWN"] ?? "—"}
              sub="last known"
            />
            <StatBox
              label="Samples"
              value={String(data.observation_count)}
              sub="position snapshots"
            />
          </div>

          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            {/* Stop timeline */}
            <Card className="lg:col-span-1">
              <SectionHeading
                title="Stop Timeline"
                subtitle={
                  data.stops.length
                    ? `${observedStopCount}/${data.stops.length} tracked`
                    : undefined
                }
                hint={
                  data.stops.length
                    ? isCondensed
                      ? `Showing ${CONDENSED_STOP_COUNT} key stops, origin → terminus. Expand a stop to see the stops collapsed between it and the next one.`
                      : "Every scheduled stop, origin → terminus."
                    : undefined
                }
                right={
                  isCondensed ? (
                    <ShowAllStopsToggle checked={allStopsShown} onChange={toggleAllStops} />
                  ) : undefined
                }
              />
              {data.stops.length === 0 ? (
                <p className="py-6 text-center text-sm text-fg-subtle">
                  No schedule found for this trip. The stop timeline is built from RTD&rsquo;s
                  static schedule for the trip&rsquo;s direction — it may be unavailable for
                  added/modified trips or older time windows.
                </p>
              ) : (
                <StopTimeline rows={timelineRows} onHover={setHoveredStop} onToggleAnchor={toggleAnchor} />
              )}
            </Card>

            {/* Trip map */}
            <Card className="lg:col-span-1">
              <SectionHeading
                title="Trip Track"
                hint="Press play to replay the trip, or hover a stop for its scheduled position."
              />
              {data.positions.length === 0 && data.stops.length === 0 ? (
                <p className="py-6 text-center text-sm text-fg-subtle">
                  No position data available.
                </p>
              ) : (
                <>
                  <div className="h-[420px] overflow-hidden rounded border border-line">
                    <VehicleTripMap
                      positions={data.positions}
                      stops={visibleStops}
                      routeColor={data.route_color ?? "3b82f6"}
                      isRail={RAIL_TYPES.has(data.route_type ?? "")}
                      highlightStop={hoveredStop}
                      playbackMs={playback.active ? playback.currentMs : null}
                    />
                  </div>
                  {data.positions.length >= 2 && (
                    <TripPlaybackControls
                      playback={playback}
                      startMs={playbackStartMs}
                      endMs={playbackEndMs}
                      routeColor={data.route_color ?? "3b82f6"}
                    />
                  )}
                </>
              )}
            </Card>
          </div>

          {data.trip_id && (
            <p className="text-xs text-fg-subtle">
              Trip ID: <span className="font-mono">{data.trip_id}</span>
            </p>
          )}
        </>
      )}
    </div>
  );
}

export default function TripDetailPage({
  params,
}: {
  params: { vehicle_label: string };
}) {
  const vehicleLabel = decodeURIComponent(params.vehicle_label);
  return (
    <Suspense
      fallback={
        <div className="mx-auto w-full max-w-7xl px-4 pb-6 pt-24">
          <p className="text-sm text-fg-subtle">Loading…</p>
        </div>
      }
    >
      <TripDetailContent vehicleLabel={vehicleLabel} />
    </Suspense>
  );
}
