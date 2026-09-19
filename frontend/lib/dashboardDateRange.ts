/**
 * Calendar-day helpers for the Dashboard's date-range picker.
 *
 * Unlike the Trip Explorer's `dateRange.ts` — built entirely around
 * `datetime-local` strings and "last N hours anchored to now" presets — the
 * Dashboard's analytics endpoints read continuous aggregates keyed by
 * calendar day (America/Denver), take an arbitrary `[start, end]` window (not
 * necessarily ending "now"), and offer month-scale presets ("Last 6 months")
 * that hours-based math can't express cleanly across DST/varying month
 * lengths. Dates here are plain `"YYYY-MM-DD"` strings — there's no
 * time-of-day component.
 */

/** Server-side caps the Dashboard picker has to respect. */
export interface DashboardRangeLimits {
  /** Widest `end - start` (inclusive, in days) the analytics endpoints accept. */
  maxSpanDays: number;
  /** How far back raw rows still exist (the hypertable retention policy). */
  retentionDays: number;
}

/**
 * Used until /api/v1/meta/limits answers, and if it never does. Kept in step
 * with `dashboard_max_span_days` / `data_retention_days` in backend/app/config.py.
 */
export const DEFAULT_DASHBOARD_RANGE_LIMITS: DashboardRangeLimits = {
  maxSpanDays: 366,
  retentionDays: 365,
};

export interface DateRange {
  start: string; // "YYYY-MM-DD"
  end: string;   // "YYYY-MM-DD"
}

const pad = (n: number) => String(n).padStart(2, "0");

/** Local midnight `Date` → `"YYYY-MM-DD"`. */
export function toDateStr(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** `"YYYY-MM-DD"` → local-midnight `Date`, or null if unparseable. */
export function fromDateStr(value: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!m) return null;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return Number.isNaN(d.getTime()) ? null : d;
}

/** Local midnight of `d`'s calendar day. */
export function startOfDay(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

export function addDays(d: Date, days: number): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() + days);
}

/** Calendar-accurate month subtraction (handles month-length/DST correctly). */
export function subtractMonths(d: Date, months: number): Date {
  return new Date(d.getFullYear(), d.getMonth() - months, d.getDate());
}

/** Number of calendar days spanned by `[start, end]`, inclusive. */
export function spanDays(start: Date, end: Date): number {
  const MS_PER_DAY = 24 * 60 * 60 * 1000;
  return Math.round((startOfDay(end).getTime() - startOfDay(start).getTime()) / MS_PER_DAY) + 1;
}

export interface FieldBounds {
  min: string;
  max: string;
}

/**
 * The overall selectable window — `[today - retentionDays + 1, today]` — bounds
 * month paging and which days can begin a fresh pick. Mirrors `dateRange.ts`'s
 * `rangeBounds` but has no per-anchor "end" bounds: the max-span cap here only
 * changes what a *second click* does (see the picker component), never which
 * days are disabled.
 */
export function overallBounds(limits: DashboardRangeLimits, now: Date = new Date()): FieldBounds {
  const today = startOfDay(now);
  const earliest = addDays(today, -(limits.retentionDays - 1));
  return { min: toDateStr(earliest), max: toDateStr(today) };
}

/** True when `day` falls inside `[min, max]` (both inclusive, date-only). */
export function isDaySelectable(day: Date, min: Date | null, max: Date | null): boolean {
  const t = startOfDay(day).getTime();
  if (min && t < startOfDay(min).getTime()) return false;
  if (max && t > startOfDay(max).getTime()) return false;
  return true;
}

/**
 * The 42 cells (6 weeks, Sunday-first) covering `month`, so the grid never
 * changes height as you page through months. Same shape as `dateRange.ts`'s
 * `calendarDays`.
 */
export function calendarDays(month: Date): Date[] {
  const first = new Date(month.getFullYear(), month.getMonth(), 1);
  const gridStart = new Date(first.getFullYear(), first.getMonth(), 1 - first.getDay());
  return Array.from(
    { length: 42 },
    (_, i) => new Date(gridStart.getFullYear(), gridStart.getMonth(), gridStart.getDate() + i),
  );
}

/** One "Last …" quick-range button on the Dashboard's date-range picker. */
export interface DashboardRangePreset {
  label: string;
  /** Exactly one of `days`/`months` is set. */
  days?: number;
  months?: number;
}

/**
 * The Dashboard reads continuous aggregates, so wide windows are cheap — its
 * presets favor long, month-scale horizons rather than the Trip Explorer's
 * hour-scale ones.
 */
export const DASHBOARD_RANGE_PRESETS: DashboardRangePreset[] = [
  { label: "Last 90 days", days: 90 },
  { label: "Last 6 months", months: 6 },
  { label: "Last year", months: 12 },
];

/**
 * The concrete `[start, end]` for a preset, anchored to `now` and clamped to
 * the retention window — the same limit `overallBounds` enforces, so a preset
 * button can never produce a range the API would reject.
 */
export function presetRange(
  preset: DashboardRangePreset,
  limits: DashboardRangeLimits,
  now: Date = new Date(),
): DateRange {
  const end = startOfDay(now);
  const earliest = addDays(end, -(limits.retentionDays - 1));
  const rawStart = preset.days != null
    ? addDays(end, -(preset.days - 1))
    : addDays(subtractMonths(end, preset.months ?? 0), 1);
  const start = rawStart.getTime() < earliest.getTime() ? earliest : rawStart;
  return { start: toDateStr(start), end: toDateStr(end) };
}

/**
 * Whether `[start, end]` is exactly what the given preset currently evaluates
 * to — so the picker can highlight which quick range, if any, is active.
 */
export function isPresetActive(
  start: string,
  end: string,
  preset: DashboardRangePreset,
  limits: DashboardRangeLimits,
  now: Date = new Date(),
): boolean {
  const r = presetRange(preset, limits, now);
  return start === r.start && end === r.end;
}

/** Pull `value` inside `[min, max]` (date-only), returning it unchanged when already inside. */
export function clampDateStr(value: string, bounds: FieldBounds): string {
  const v = fromDateStr(value);
  const min = fromDateStr(bounds.min);
  const max = fromDateStr(bounds.max);
  if (!v) return bounds.min;
  if (min && v < min) return bounds.min;
  if (max && v > max) return bounds.max;
  return value;
}

/** Human-readable summary of the retention window, for the hint under the calendar. */
export function describeDashboardLimits(limits: DashboardRangeLimits): string {
  return `Range can be within the last ${limits.retentionDays} days.`;
}
