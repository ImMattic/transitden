import { clsx, type ClassValue } from "clsx";
import type { ResolvedTheme } from "@/lib/useTheme";
import type { VehiclePosition } from "@/lib/types";

export function cn(...inputs: ClassValue[]) {
  return clsx(inputs);
}

/**
 * How recently a leg must have reported to be a candidate for "still running".
 * Matches _LIVE_TRIP_WINDOW on the trips-list endpoint, which decides the same
 * thing server-side.
 */
const LIVE_LEG_WINDOW_MS = 90_000;

/**
 * Whether a (vehicle, trip) leg is still being tracked live — i.e. it shows
 * up in the current GTFS-RT realtime feed under the same vehicle label *and*
 * trip id. `liveVehicles` is the `vehicles` array from `useVehicles()`.
 *
 * `legEnd` is the leg's own last-seen time, and it is not optional in spirit:
 * neither a vehicle label nor a trip id is scoped to a date, so the pair alone
 * cannot tell today's run from an earlier one. Rail is where that bites —
 * RTD labels rail vehicles with a run number (1-99, reused every day) rather
 * than a fleet number, and a GTFS trip id is reused for months — so yesterday's
 * 3pm train matches the live feed exactly at 3pm today. A leg that last
 * reported hours ago is finished whatever the feed says.
 */
export function isTripInProgress(
  liveVehicles: VehiclePosition[] | undefined,
  vehicleLabel: string | null | undefined,
  tripId: string | null | undefined,
  legEnd?: string | null,
): boolean {
  if (!liveVehicles || !vehicleLabel || !tripId) return false;
  if (legEnd) {
    const ended = Date.parse(legEnd);
    if (Number.isNaN(ended) || Date.now() - ended > LIVE_LEG_WINDOW_MS) return false;
  }
  return liveVehicles.some((v) => v.vehicle_label === vehicleLabel && v.trip_id === tripId);
}

/**
 * "in_progress" — still showing up in the live feed.
 * "complete" — finished, and reached its terminus.
 * "incomplete" — finished, but never geofenced at its terminus (dropped GPS,
 *   broke down, or otherwise fell off the schedule before the end of the line).
 */
export type TripStatus = "in_progress" | "complete" | "incomplete";

export function computeTripStatus(inProgress: boolean, reachedTerminus: boolean): TripStatus {
  if (inProgress) return "in_progress";
  return reachedTerminus ? "complete" : "incomplete";
}

// ── Occupancy ───────────────────────────────────────────────────────────────
// GTFS-realtime OccupancyStatus, least to most crowded, with "UNKNOWN" standing
// in for a vehicle that reported nothing. Shared by the trips table and both
// filter menus so one vocabulary covers the site.

export const OCCUPANCY_ORDER = [
  "EMPTY",
  "MANY_SEATS_AVAILABLE",
  "FEW_SEATS_AVAILABLE",
  "STANDING_ROOM_ONLY",
  "CRUSHED_STANDING_ROOM_ONLY",
  "FULL",
  "NOT_ACCEPTING_PASSENGERS",
  "UNKNOWN",
] as const;

const OCCUPANCY_LABELS: Record<string, string> = {
  EMPTY: "Empty",
  MANY_SEATS_AVAILABLE: "Many seats",
  FEW_SEATS_AVAILABLE: "Few seats",
  STANDING_ROOM_ONLY: "Standing",
  CRUSHED_STANDING_ROOM_ONLY: "Crushed",
  FULL: "Full",
  NOT_ACCEPTING_PASSENGERS: "Not accepting",
  UNKNOWN: "Not reported",
};

export function occupancyLabel(key: string | null | undefined): string {
  if (!key) return OCCUPANCY_LABELS.UNKNOWN;
  return OCCUPANCY_LABELS[key] ?? formatStatusLabel(key);
}

/** Convert a hex color string (with or without #) to a CSS color. */
export function routeColor(hex: string): string {
  const clean = hex.startsWith("#") ? hex : `#${hex}`;
  return clean === "#" || clean === "#888888" ? "#6b7280" : clean;
}

/**
 * White or near-black — whichever reads better as text on top of `hex`.
 * Used for badges/pills whose background is a data-driven color (headway,
 * route brand color) rather than a theme token, so a fixed text color can't
 * be relied on to stay legible.
 */
export function bestTextOn(hex: string): string {
  const clean = hex.startsWith("#") ? hex.slice(1) : hex;
  if (clean.length !== 6) return "#FFFFFF";
  const r = parseInt(clean.slice(0, 2), 16) / 255;
  const g = parseInt(clean.slice(2, 4), 16) / 255;
  const b = parseInt(clean.slice(4, 6), 16) / 255;
  const lin = (c: number) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  const luminance = 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
  // Contrast against white vs. against near-black — pick whichever is higher.
  const contrastWhite = 1.05 / (luminance + 0.05);
  const contrastDark = (luminance + 0.05) / 0.05;
  return contrastWhite >= contrastDark ? "#FFFFFF" : "#0D0E11";
}

/**
 * Frequency headway → marker/badge color. A four-step good→bad ramp: Signal
 * Teal, then gold, then Signal Orange, then Signal Red (see DESIGN_TOKENS.md).
 * Re-stepped per theme so each clears contrast against both the dark and light
 * map/chart surfaces. Pair with `bestTextOn` for any fixed-text badge. Keep the
 * thresholds in lock-step with `headwayBand` in `lib/mapFilters.ts` and the map
 * legend.
 */
export function headwayColor(headwayMinutes: number | null, mode: ResolvedTheme = "dark"): string {
  if (mode === "light") {
    if (headwayMinutes === null || headwayMinutes === 0) return "#6b7280"; // unknown – gray
    if (headwayMinutes < 15) return "#007A6B"; // <15 min – Signal Teal, deep
    if (headwayMinutes < 30) return "#A16207"; // 15–30 min – gold, deep
    if (headwayMinutes < 60) return "#B44D08"; // 30–60 min – Signal Orange, deep
    return "#C50C2B";                          // 60+ min – Signal Red
  }
  if (headwayMinutes === null || headwayMinutes === 0) return "#7C838E"; // unknown – gray
  if (headwayMinutes < 15) return "#009483"; // <15 min – RTD Teal
  if (headwayMinutes < 30) return "#F2C12E"; // 15–30 min – gold
  if (headwayMinutes < 60) return "#F6871F"; // 30–60 min – RTD Orange
  return "#F03E48";                          // 60+ min – RTD Red
}

/** Format seconds as "+Xm Ys" / "" / "-Xm Ys". Returns "" for exactly 0 (badge already says "On time"). */
export function formatDelay(seconds: number | null): string {
  if (seconds === null) return "—";
  if (seconds === 0) return "";
  const sign = seconds > 0 ? "+" : "-";
  const abs = Math.abs(seconds);
  const mins = Math.floor(abs / 60);
  const secs = abs % 60;
  return mins > 0 ? `${sign}${mins}m ${secs}s` : `${sign}${secs}s`;
}

/** ISO timestamp → human-friendly local string. */
export function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Color for an on-time percentage across five tiers, re-stepped per theme. */
export function onTimeColor(pct: number, mode: ResolvedTheme = "dark"): string {
  if (mode === "light") {
    if (pct >= 80) return "#16a34a";
    if (pct >= 65) return "#c8d614";
    if (pct >= 50) return "#eab308";
    if (pct >= 35) return "#f97316";
    return "#dc2626";
  }
  if (pct >= 80) return "#16a34a"; // green — already bright enough for dark
  if (pct >= 65) return "#c8d614"; // chartreuse
  if (pct >= 50) return "#eab308"; // yellow
  if (pct >= 35) return "#f97316"; // orange
  return "#EC3A35";                // red, lightened to clear the dark card
}

/** Color for an average delay in seconds — early/on-time (cool) → very late (warm/red). */
export function delayColor(seconds: number, mode: ResolvedTheme = "dark"): string {
  if (mode === "light") {
    if (seconds <= 0) return "#16a34a";
    if (seconds <= 300) return "#65a30d";
    if (seconds <= 600) return "#eab308";
    if (seconds <= 900) return "#ea580c";
    return "#dc2626";
  }
  if (seconds <= 0) return "#16a34a";
  if (seconds <= 300) return "#65a30d";
  if (seconds <= 600) return "#eab308";
  if (seconds <= 900) return "#ea580c";
  return "#EC3A35";
}

/** Crowding percentage (share of samples standing/crushed/full+) → color, higher is worse. */
export function crowdingColor(pct: number, mode: ResolvedTheme = "dark"): string {
  if (mode === "light") {
    if (pct >= 70) return "#dc2626";
    if (pct >= 45) return "#ea580c";
    if (pct >= 20) return "#eab308";
    return "#16a34a";
  }
  if (pct >= 70) return "#EC3A35";
  if (pct >= 45) return "#f97316";
  if (pct >= 20) return "#eab308";
  return "#16a34a";
}

/** Compact integer with thousands separators (e.g. 12,345). */
export function formatNumber(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return n.toLocaleString("en-US");
}

/** Large numbers as short form: 1.2M, 34.5K. */
export function formatCompact(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(n);
}

/** Delay seconds → short signed minutes string, e.g. "+5.2m", "-1.0m", "on time". */
export function formatDelayMin(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  const mins = seconds / 60;
  if (Math.abs(mins) < 0.1) return "on time";
  return `${mins > 0 ? "+" : ""}${mins.toFixed(1)}m`;
}

/** ISO date (YYYY-MM-DD) → "May 2026". */
export function formatMonth(iso: string): string {
  const d = new Date(`${iso}T00:00:00`);
  return d.toLocaleDateString("en-US", { month: "short", year: "numeric" });
}

/** Format hour 0–23 as "1 PM", "12 AM". */
export function formatHour(hour: number): string {
  const h = ((hour % 24) + 24) % 24;
  if (h === 0) return "12a";
  if (h === 12) return "12p";
  return h < 12 ? `${h}a` : `${h - 12}p`;
}

export const DOW_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/** GTFS-realtime status/occupancy key (e.g. "IN_TRANSIT_TO") → "In transit to". */
export function formatStatusLabel(key: string | null | undefined): string {
  if (!key) return "";
  const words = key.toLowerCase().split("_");
  return words[0].charAt(0).toUpperCase() + words[0].slice(1) + (words.length > 1 ? ` ${words.slice(1).join(" ")}` : "");
}

/** Compass bearing (deg, 0=N) from point A→B. Returns null if the points coincide. */
function bearingBetween(lat1: number, lon1: number, lat2: number, lon2: number): number | null {
  if (lat1 === lat2 && lon1 === lon2) return null;
  const toRad = (d: number) => (d * Math.PI) / 180;
  const phi1 = toRad(lat1);
  const phi2 = toRad(lat2);
  const dLon = toRad(lon2 - lon1);
  const y = Math.sin(dLon) * Math.cos(phi2);
  const x = Math.cos(phi1) * Math.sin(phi2) - Math.sin(phi1) * Math.cos(phi2) * Math.cos(dLon);
  return ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
}

interface TrackSample {
  latitude: number;
  longitude: number;
  bearing: number | null;
  timestamp: string;
}

/**
 * Find where the vehicle was at `targetIso` by linearly interpolating between the
 * two bracketing position samples. Bearing comes from the direction of travel
 * across that segment (falling back to a reported bearing when the vehicle is
 * stationary). Clamps to the first/last sample when the target falls outside the
 * recorded track. Returns null when no usable samples exist.
 */
export function interpolateTrackPosition(
  positions: TrackSample[],
  targetIso: string,
): { lat: number; lon: number; bearing: number | null } | null {
  const samples = positions
    .filter((p) => p.latitude != null && p.longitude != null)
    .map((p) => ({ ...p, t: new Date(p.timestamp).getTime() }))
    .sort((a, b) => a.t - b.t);
  if (samples.length === 0) return null;

  const target = new Date(targetIso).getTime();
  const first = samples[0];
  const last = samples[samples.length - 1];

  if (target <= first.t) return { lat: first.latitude, lon: first.longitude, bearing: first.bearing };
  if (target >= last.t) return { lat: last.latitude, lon: last.longitude, bearing: last.bearing };

  for (let i = 0; i < samples.length - 1; i++) {
    const a = samples[i];
    const b = samples[i + 1];
    if (target >= a.t && target <= b.t) {
      const frac = b.t === a.t ? 0 : (target - a.t) / (b.t - a.t);
      const lat = a.latitude + (b.latitude - a.latitude) * frac;
      const lon = a.longitude + (b.longitude - a.longitude) * frac;
      const bearing =
        bearingBetween(a.latitude, a.longitude, b.latitude, b.longitude) ?? a.bearing ?? b.bearing;
      return { lat, lon, bearing };
    }
  }
  return null;
}
