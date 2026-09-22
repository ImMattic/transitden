import { describe, it, expect } from "vitest";
import {
  EMPTY_MAP_FILTERS,
  applyMapFilters,
  buildMapFacets,
  countActiveMapFilters,
  headwayBand,
  mapFiltersEqual,
  modeOf,
  movementBand,
  punctualityBand,
  vehicleKey,
  type MapFilters,
} from "@/lib/mapFilters";
import type { VehiclePosition } from "@/lib/types";

function vehicle(overrides: Partial<VehiclePosition> = {}): VehiclePosition {
  return {
    vehicle_id: "v1",
    vehicle_label: "1001",
    trip_id: "t1",
    route_id: "15",
    route_short_name: "15",
    route_long_name: "East Colfax",
    route_color: "0055B8",
    route_type: "3",
    latitude: 39.7,
    longitude: -105,
    bearing: 90,
    current_stop_sequence: 4,
    current_status: 2,
    current_status_label: "IN_TRANSIT_TO",
    stop_id: "s1",
    stop_name: "Colfax & Broadway",
    occupancy_status: "MANY_SEATS_AVAILABLE",
    timestamp: "2026-09-10T12:00:00Z",
    delay_seconds: 60,
    is_late: false,
    headway_minutes: 12,
    ...overrides,
  };
}

const filters = (overrides: Partial<MapFilters> = {}): MapFilters => ({
  ...EMPTY_MAP_FILTERS,
  ...overrides,
});

describe("band helpers", () => {
  it("splits rail from bus by GTFS route_type", () => {
    expect(modeOf("0")).toBe("rail");
    expect(modeOf("2")).toBe("rail");
    expect(modeOf("3")).toBe("bus");
    expect(modeOf("11")).toBe("other");
    expect(modeOf(null)).toBe("other");
  });

  it("buckets headway on the same four steps the markers are painted with", () => {
    expect(headwayBand(10)).toBe("under15");
    expect(headwayBand(15)).toBe("to30");
    expect(headwayBand(20)).toBe("to30");
    expect(headwayBand(30)).toBe("to60");
    expect(headwayBand(30.5)).toBe("to60");
    expect(headwayBand(60)).toBe("over60");
    expect(headwayBand(80)).toBe("over60");
    // A route with too few vehicles reports 0, which is "we don't know".
    expect(headwayBand(0)).toBe("unknown");
    expect(headwayBand(null)).toBe("unknown");
  });

  it("treats RTD's ±5 minutes as on time", () => {
    expect(punctualityBand(-400)).toBe("early");
    expect(punctualityBand(-300)).toBe("on_time");
    expect(punctualityBand(300)).toBe("on_time");
    expect(punctualityBand(600)).toBe("late");
    expect(punctualityBand(1200)).toBe("very_late");
    expect(punctualityBand(null)).toBe("unknown");
  });

  it("names the GTFS stop statuses", () => {
    expect(movementBand(0)).toBe("incoming");
    expect(movementBand(1)).toBe("stopped");
    expect(movementBand(2)).toBe("in_transit");
    expect(movementBand(null)).toBe("unknown");
  });

  it("identifies a vehicle by fleet number when it reports one", () => {
    expect(vehicleKey(vehicle())).toBe("1001");
    expect(vehicleKey(vehicle({ vehicle_label: null }))).toBe("v1");
    expect(vehicleKey(vehicle({ vehicle_label: null, vehicle_id: null }))).toBe("15:t1");
  });
});

describe("applyMapFilters", () => {
  const bus = vehicle();
  const train = vehicle({
    vehicle_id: "v2",
    vehicle_label: "2002",
    trip_id: "t2",
    route_id: "E",
    route_short_name: "E",
    route_type: "0",
    occupancy_status: "STANDING_ROOM_ONLY",
    headway_minutes: 45,
    delay_seconds: 1200,
    current_status: 1,
  });
  const all = [bus, train];

  it("returns everything when no group is set", () => {
    expect(applyMapFilters(all, EMPTY_MAP_FILTERS)).toEqual(all);
    expect(countActiveMapFilters(EMPTY_MAP_FILTERS)).toBe(0);
  });

  it("ORs within a group", () => {
    expect(applyMapFilters(all, filters({ modes: ["rail", "bus"] }))).toEqual(all);
    expect(applyMapFilters(all, filters({ modes: ["rail"] }))).toEqual([train]);
  });

  it("ANDs across groups", () => {
    expect(
      applyMapFilters(all, filters({ modes: ["rail"], occupancy: ["MANY_SEATS_AVAILABLE"] })),
    ).toEqual([]);
    expect(
      applyMapFilters(all, filters({ modes: ["rail"], occupancy: ["STANDING_ROOM_ONLY"] })),
    ).toEqual([train]);
  });

  it("filters by route, fleet number, frequency and punctuality", () => {
    expect(applyMapFilters(all, filters({ routeIds: ["15"] }))).toEqual([bus]);
    expect(applyMapFilters(all, filters({ vehicleKeys: ["2002"] }))).toEqual([train]);
    expect(applyMapFilters(all, filters({ headway: ["under15"] }))).toEqual([bus]);
    expect(applyMapFilters(all, filters({ punctuality: ["very_late"] }))).toEqual([train]);
    expect(applyMapFilters(all, filters({ movement: ["stopped"] }))).toEqual([train]);
  });

  it("treats a vehicle that never reported occupancy as UNKNOWN", () => {
    const quiet = vehicle({ vehicle_label: "3003", occupancy_status: null });
    expect(applyMapFilters([quiet], filters({ occupancy: ["UNKNOWN"] }))).toEqual([quiet]);
  });

  it("counts one active filter per group that is set", () => {
    expect(
      countActiveMapFilters(filters({ modes: ["rail"], routeIds: ["E", "15"] })),
    ).toBe(2);
  });
});

describe("buildMapFacets", () => {
  it("counts options over the whole feed, not the filtered view", () => {
    const feed = [
      vehicle(),
      vehicle({ vehicle_id: "v2", vehicle_label: "1002", trip_id: "t2" }),
      vehicle({
        vehicle_id: "v3",
        vehicle_label: "3003",
        trip_id: "t3",
        route_id: "E",
        route_short_name: "E",
        route_type: "0",
      }),
    ];
    const facets = buildMapFacets(feed);

    expect(facets.modes).toEqual({ rail: 1, bus: 2, other: 0 });
    expect(facets.routes.map((r) => [r.shortName, r.count])).toEqual([
      ["15", 2],
      ["E", 1],
    ]);
    expect(facets.vehicles.map((v) => v.label)).toEqual(["1001", "1002", "3003"]);
    expect(facets.vehicles[2].routeShortName).toBe("E");
    expect(facets.occupancy.MANY_SEATS_AVAILABLE).toBe(3);
  });

  it("sorts route numbers numerically rather than as text", () => {
    const feed = [
      vehicle({ route_id: "120", route_short_name: "120" }),
      vehicle({ route_id: "15", route_short_name: "15", vehicle_label: "1002" }),
    ];
    expect(buildMapFacets(feed).routes.map((r) => r.shortName)).toEqual(["15", "120"]);
  });
});

describe("mapFiltersEqual", () => {
  it("ignores order within a group", () => {
    const a: MapFilters = { ...EMPTY_MAP_FILTERS, routeIds: ["15", "20"], modes: ["bus"] };
    const b: MapFilters = { ...EMPTY_MAP_FILTERS, routeIds: ["20", "15"], modes: ["bus"] };
    expect(mapFiltersEqual(a, b)).toBe(true);
  });

  it("catches a real edit", () => {
    const base: MapFilters = { ...EMPTY_MAP_FILTERS, routeIds: ["15"] };
    expect(mapFiltersEqual(base, { ...base, routeIds: ["15", "20"] })).toBe(false);
    expect(mapFiltersEqual(base, { ...base, movement: ["stopped"] })).toBe(false);
    expect(mapFiltersEqual(EMPTY_MAP_FILTERS, EMPTY_MAP_FILTERS)).toBe(true);
  });
});
