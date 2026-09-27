/**
 * How much of a selected date range sits in TimescaleDB's compressed ("packed")
 * storage vs its still-uncompressed ("loose") storage, and the plain-language
 * copy that goes with it — for the Trip Explorer's date-range picker only.
 *
 * The backend (`/api/v1/meta/storage`) exposes exactly one fact: `looseSince`,
 * the earliest instant not yet reached by the compression policy. Everything
 * before it is packed; everything at or after it is loose. That single instant
 * is enough to work out how much of any `[start, end]` selection falls on each
 * side, without this file knowing anything about chunks, hypertables, or the
 * policy interval.
 *
 * Packed data isn't slow because something is wrong — it's ~90% smaller on
 * disk and costs more work to unpack back into individual trips, a deliberate
 * tradeoff (see migration 002). The two-tier language here — "packed"/"loose"
 * rather than "compressed"/"uncompressed" — is deliberate too: most people's
 * only prior exposure to "compressed" is a zip file, where smaller reads as
 * *better*, which is backwards for what it means here.
 */

const DAY_MS = 86_400_000;

export interface RangeTierSummary {
  /** False when the boundary itself is unknown (a non-Timescale DB, e.g. the
   *  test suite, or the endpoint unreachable) — callers should show nothing
   *  about tiers rather than guess. */
  known: boolean;
  totalDays: number;
  packedDays: number;
  looseDays: number;
}

/**
 * `[startIso, endIso]` split against `looseSinceIso`, the boundary reported by
 * `/api/v1/meta/storage` for one hypertable. `looseSinceIso` of `null` means
 * "unknown" (see `StorageResponse.available`), not "everything is packed".
 */
export function rangeTierSummary(
  startIso: string,
  endIso: string,
  looseSinceIso: string | null,
): RangeTierSummary {
  const start = new Date(startIso).getTime();
  const end = new Date(endIso).getTime();
  const totalMs = Math.max(0, end - start);
  const totalDays = totalMs / DAY_MS;

  if (looseSinceIso === null || Number.isNaN(start) || Number.isNaN(end)) {
    return { known: false, totalDays, packedDays: 0, looseDays: 0 };
  }

  const boundary = new Date(looseSinceIso).getTime();
  // Everything strictly before the boundary is packed; at/after it is loose.
  const packedMs = Math.min(Math.max(0, boundary - start), totalMs);
  const looseMs = totalMs - packedMs;
  return {
    known: true,
    totalDays,
    packedDays: packedMs / DAY_MS,
    looseDays: looseMs / DAY_MS,
  };
}

export type DayTier = "packed" | "loose" | "mixed";

/**
 * Which side of the packed/loose boundary a single calendar day falls on.
 * `day` is read at local midnight (as `calendarDays()` produces it) — the
 * boundary is an instant, not a day, so it can land inside a day rather than
 * cleanly between two of them, hence "mixed". `null` means unknown, the same
 * as everywhere else in this file.
 */
export function dayTier(day: Date, looseSinceIso: string | null): DayTier | null {
  if (looseSinceIso === null) return null;
  const boundary = new Date(looseSinceIso).getTime();
  const dayStart = new Date(day.getFullYear(), day.getMonth(), day.getDate()).getTime();
  const dayEnd = dayStart + DAY_MS;
  if (boundary <= dayStart) return "loose";
  if (boundary >= dayEnd) return "packed";
  return "mixed";
}

/**
 * For a "mixed" day only: how far across it (0–1) the packed→loose split
 * lands, so the calendar can render a two-tone band rather than picking one
 * color and being wrong for part of the day.
 */
export function dayPackedFraction(day: Date, looseSinceIso: string): number {
  const boundary = new Date(looseSinceIso).getTime();
  const dayStart = new Date(day.getFullYear(), day.getMonth(), day.getDate()).getTime();
  const packedMs = Math.min(Math.max(boundary - dayStart, 0), DAY_MS);
  return packedMs / DAY_MS;
}

/** One decimal place, e.g. "2.4 days" / "1 day" — day counts read oddly at
 *  full float precision and don't need it. */
export function formatDays(days: number): string {
  if (days <= 0) return "0 days";
  // A day-ish value close to 1 reads better as "1 day" than "1.0 days".
  const rounded = Math.round(days * 10) / 10;
  return rounded === 1 ? "1 day" : `${rounded % 1 === 0 ? rounded.toFixed(0) : rounded.toFixed(1)} days`;
}

/**
 * The plain, always-visible line — states the effect ("this takes a while"),
 * never the mechanism. `null` when the range doesn't touch packed storage at
 * all, or the boundary is unknown: nothing to say, so the picker shows nothing.
 */
export function rangeSpeedHeadline(summary: RangeTierSummary): string | null {
  if (!summary.known || summary.packedDays <= 0) return null;
  return summary.looseDays > 0
    ? "This range takes a few seconds to load. Today and yesterday are quicker."
    : "This range takes a few seconds to load.";
}
