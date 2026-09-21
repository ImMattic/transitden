// The Trip Explorer's sort order — pure model plus URL encode/decode, alongside
// tripFilters.ts. Sorting runs server-side for the same reason filtering does:
// the table is paginated by the API, so ordering only the page that came back
// would put the "longest trip" on whichever page it happened to land.
//
// Occupancy is a category rather than a number, so the API orders it by how
// crowded the level is (Empty → … → Full; see OCCUPANCY_ORDER in lib/utils.ts),
// with trips that never reported one at the end.

export type TripSortKey =
  | "route"
  | "vehicle"
  | "start"
  | "end"
  | "duration"
  | "occupancy"
  | "avg_delay"
  | "on_time";

export type SortDir = "asc" | "desc";

export interface TripSort {
  key: TripSortKey;
  dir: SortDir;
}

export const TRIP_SORT_KEYS: readonly TripSortKey[] = [
  "route",
  "vehicle",
  "start",
  "end",
  "duration",
  "occupancy",
  "avg_delay",
  "on_time",
];

/** What the API returns when no sort is asked for: earliest start first. */
export const DEFAULT_TRIP_SORT: TripSort = { key: "start", dir: "asc" };

/** The sort the table is actually in — the default when nothing is chosen, so
 *  the Start header can show it. */
export function effectiveTripSort(sort: TripSort | null): TripSort {
  return sort ?? DEFAULT_TRIP_SORT;
}

/**
 * What clicking a column's header does. A new column starts ascending; clicking
 * the active one flips it, and a second flip returns to the default order. Start
 * ascending *is* the default, so it is never stored: `null` covers it.
 */
export function nextTripSort(current: TripSort | null, key: TripSortKey): TripSort | null {
  const active = effectiveTripSort(current);
  if (active.key !== key) return key === DEFAULT_TRIP_SORT.key ? null : { key, dir: "asc" };
  return active.dir === "asc" ? { key, dir: "desc" } : null;
}

/** Reads `?sort=…&dir=…`; anything unrecognised means "no sort". */
export function tripSortFromParams(params: URLSearchParams): TripSort | null {
  const key = params.get("sort");
  if (!key || !(TRIP_SORT_KEYS as readonly string[]).includes(key)) return null;
  const dir: SortDir = params.get("dir") === "desc" ? "desc" : "asc";
  if (key === DEFAULT_TRIP_SORT.key && dir === DEFAULT_TRIP_SORT.dir) return null;
  return { key: key as TripSortKey, dir };
}

/** The sort half of the page URL — merge into a `URLSearchParams`. */
export function tripSortToParams(sort: TripSort | null): Record<string, string> {
  return sort ? { sort: sort.key, dir: sort.dir } : {};
}

/** The same sort shaped for `fetchActiveVehicles`. */
export function tripSortToQuery(sort: TripSort | null) {
  return { sort_by: sort?.key, sort_dir: sort?.dir };
}
