// ── Live-map filter model ───────────────────────────────────────────────────
// Every predicate here is pure and takes only what it needs, so the filter menu
// and the map agree by construction and the whole thing is unit-testable without
// a DOM. The realtime feed is already in memory, so all of this runs client-side.

import type { VehiclePosition } from "@/lib/types";

/** GTFS route_type values that mean rail: 0 tram/LRT, 1 subway, 2 commuter. */
const RAIL_ROUTE_TYPES = new Set(["0", "1", "2"]);

export type TransitMode = "rail" | "bus" | "other";

/** Headway buckets — the same steps `headwayColor` paints on the markers. */
export type HeadwayBand = "under15" | "to30" | "to60" | "over60" | "unknown";

/** Schedule adherence, stepped around RTD's ±5-minute on-time definition. */
export type PunctualityBand = "early" | "on_time" | "late" | "very_late" | "unknown";

/** GTFS-realtime VehicleStopStatus, named for humans. */
export type MovementBand = "stopped" | "in_transit" | "incoming" | "unknown";

export interface MapFilters {
  modes: TransitMode[];
  routeIds: string[];
  /** Vehicle identity as produced by `vehicleKey`. */
  vehicleKeys: string[];
  headway: HeadwayBand[];
  occupancy: string[];
  punctuality: PunctualityBand[];
  movement: MovementBand[];
}

export const EMPTY_MAP_FILTERS: MapFilters = {
  modes: [],
  routeIds: [],
  vehicleKeys: [],
  headway: [],
  occupancy: [],
  punctuality: [],
  movement: [],
};

/**
 * Stable identity for one vehicle in the feed. The fleet number (`vehicle_label`)
 * is what riders and the filter menu actually see, so it wins; the other keys are
 * only there so a vehicle reporting no label is still selectable.
 */
export function vehicleKey(v: {
  vehicle_label: string | null;
  vehicle_id: string | null;
  trip_id: string | null;
  route_id: string;
}): string {
  return v.vehicle_label ?? v.vehicle_id ?? `${v.route_id}:${v.trip_id ?? "?"}`;
}

export function modeOf(routeType: string | null | undefined): TransitMode {
  if (!routeType) return "other";
  if (RAIL_ROUTE_TYPES.has(routeType)) return "rail";
  return routeType === "3" ? "bus" : "other";
}

export function headwayBand(minutes: number | null | undefined): HeadwayBand {
  if (minutes === null || minutes === undefined || minutes === 0) return "unknown";
  if (minutes < 15) return "under15";
  if (minutes < 30) return "to30";
  if (minutes < 60) return "to60";
  return "over60";
}

export function punctualityBand(delaySeconds: number | null | undefined): PunctualityBand {
  if (delaySeconds === null || delaySeconds === undefined) return "unknown";
  if (delaySeconds < -300) return "early";
  if (delaySeconds <= 300) return "on_time";
  if (delaySeconds <= 900) return "late";
  return "very_late";
}

export function movementBand(currentStatus: number | null | undefined): MovementBand {
  switch (currentStatus) {
    case 0:
      return "incoming";
    case 1:
      return "stopped";
    case 2:
      return "in_transit";
    default:
      return "unknown";
  }
}

export function occupancyKey(status: string | null | undefined): string {
  return status ?? "UNKNOWN";
}

/** How many filter groups are narrowing the map — what the button badge shows. */
export function countActiveMapFilters(f: MapFilters): number {
  return (
    (f.modes.length > 0 ? 1 : 0) +
    (f.routeIds.length > 0 ? 1 : 0) +
    (f.vehicleKeys.length > 0 ? 1 : 0) +
    (f.headway.length > 0 ? 1 : 0) +
    (f.occupancy.length > 0 ? 1 : 0) +
    (f.punctuality.length > 0 ? 1 : 0) +
    (f.movement.length > 0 ? 1 : 0)
  );
}

export function mapFiltersActive(f: MapFilters): boolean {
  return countActiveMapFilters(f) > 0;
}

/** Order-insensitive equality, so the Apply button can tell a real edit from a no-op. */
export function mapFiltersEqual(a: MapFilters, b: MapFilters): boolean {
  const sameList = (x: string[], y: string[]) => {
    if (x.length !== y.length) return false;
    const s = new Set(x);
    return y.every((v) => s.has(v));
  };
  return (
    sameList(a.modes, b.modes) &&
    sameList(a.routeIds, b.routeIds) &&
    sameList(a.vehicleKeys, b.vehicleKeys) &&
    sameList(a.headway, b.headway) &&
    sameList(a.occupancy, b.occupancy) &&
    sameList(a.punctuality, b.punctuality) &&
    sameList(a.movement, b.movement)
  );
}

/**
 * Apply the filter set. Groups are ANDed with each other and ORed within
 * themselves — an empty group means "don't narrow on this", which is what makes
 * a freshly reset menu show the whole system.
 */
export function applyMapFilters(
  vehicles: VehiclePosition[],
  f: MapFilters,
): VehiclePosition[] {
  if (!mapFiltersActive(f)) return vehicles;

  const modes = new Set(f.modes);
  const routeIds = new Set(f.routeIds);
  const keys = new Set(f.vehicleKeys);
  const headway = new Set(f.headway);
  const occupancy = new Set(f.occupancy);
  const punctuality = new Set(f.punctuality);
  const movement = new Set(f.movement);

  return vehicles.filter((v) => {
    if (modes.size > 0 && !modes.has(modeOf(v.route_type))) return false;
    if (routeIds.size > 0 && !routeIds.has(v.route_id)) return false;
    if (keys.size > 0 && !keys.has(vehicleKey(v))) return false;
    if (headway.size > 0 && !headway.has(headwayBand(v.headway_minutes))) return false;
    if (occupancy.size > 0 && !occupancy.has(occupancyKey(v.occupancy_status))) return false;
    if (punctuality.size > 0 && !punctuality.has(punctualityBand(v.delay_seconds))) return false;
    if (movement.size > 0 && !movement.has(movementBand(v.current_status))) return false;
    return true;
  });
}

// ── Facets ──────────────────────────────────────────────────────────────────
// Counts come from the *unfiltered* feed so the numbers beside each option stay
// still as you tick boxes, instead of collapsing toward the current selection.

export interface RouteFacet {
  routeId: string;
  shortName: string;
  longName: string;
  color: string;
  mode: TransitMode;
  count: number;
}

export interface VehicleFacet {
  key: string;
  label: string;
  routeShortName: string;
  routeColor: string;
  mode: TransitMode;
}

export interface MapFacets {
  routes: RouteFacet[];
  vehicles: VehicleFacet[];
  modes: Record<TransitMode, number>;
  headway: Record<HeadwayBand, number>;
  occupancy: Record<string, number>;
  punctuality: Record<PunctualityBand, number>;
  movement: Record<MovementBand, number>;
}

function bump<K extends string>(counts: Record<K, number>, key: K): void {
  counts[key] = (counts[key] ?? 0) + 1;
}

export function buildMapFacets(vehicles: VehiclePosition[]): MapFacets {
  const routes = new Map<string, RouteFacet>();
  const vehicleFacets: VehicleFacet[] = [];
  const seenVehicles = new Set<string>();
  const modes = { rail: 0, bus: 0, other: 0 } as Record<TransitMode, number>;
  const headway = {
    under15: 0,
    to30: 0,
    to60: 0,
    over60: 0,
    unknown: 0,
  } as Record<HeadwayBand, number>;
  const occupancy: Record<string, number> = {};
  const punctuality = {
    early: 0,
    on_time: 0,
    late: 0,
    very_late: 0,
    unknown: 0,
  } as Record<PunctualityBand, number>;
  const movement = {
    stopped: 0,
    in_transit: 0,
    incoming: 0,
    unknown: 0,
  } as Record<MovementBand, number>;

  for (const v of vehicles) {
    const mode = modeOf(v.route_type);
    bump(modes, mode);
    bump(headway, headwayBand(v.headway_minutes));
    bump(occupancy, occupancyKey(v.occupancy_status));
    bump(punctuality, punctualityBand(v.delay_seconds));
    bump(movement, movementBand(v.current_status));

    const existing = routes.get(v.route_id);
    if (existing) {
      existing.count += 1;
    } else {
      routes.set(v.route_id, {
        routeId: v.route_id,
        shortName: v.route_short_name || v.route_id,
        longName: v.route_long_name ?? "",
        color: v.route_color || "888888",
        mode,
        count: 1,
      });
    }

    const key = vehicleKey(v);
    if (!seenVehicles.has(key)) {
      seenVehicles.add(key);
      vehicleFacets.push({
        key,
        label: v.vehicle_label ?? key,
        routeShortName: v.route_short_name || v.route_id,
        routeColor: v.route_color || "888888",
        mode,
      });
    }
  }

  const collator = new Intl.Collator(undefined, { numeric: true });
  return {
    routes: [...routes.values()].sort((a, b) => collator.compare(a.shortName, b.shortName)),
    vehicles: vehicleFacets.sort((a, b) => collator.compare(a.label, b.label)),
    modes,
    headway,
    occupancy,
    punctuality,
    movement,
  };
}
