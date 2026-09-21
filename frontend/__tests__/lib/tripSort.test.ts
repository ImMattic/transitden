import { describe, it, expect } from "vitest";
import {
  DEFAULT_TRIP_SORT,
  effectiveTripSort,
  nextTripSort,
  tripSortFromParams,
  tripSortToParams,
  tripSortToQuery,
} from "@/lib/tripSort";

describe("nextTripSort", () => {
  it("starts a newly clicked column ascending", () => {
    expect(nextTripSort(null, "duration")).toEqual({ key: "duration", dir: "asc" });
    expect(nextTripSort({ key: "route", dir: "desc" }, "occupancy")).toEqual({
      key: "occupancy",
      dir: "asc",
    });
  });

  it("flips the active column, then returns to the default order", () => {
    const asc = nextTripSort(null, "avg_delay");
    const desc = nextTripSort(asc, "avg_delay");
    expect(desc).toEqual({ key: "avg_delay", dir: "desc" });
    expect(nextTripSort(desc, "avg_delay")).toBeNull();
  });

  it("treats Start ascending as the default, so its first click is descending", () => {
    expect(nextTripSort(null, "start")).toEqual({ key: "start", dir: "desc" });
    expect(nextTripSort({ key: "start", dir: "desc" }, "start")).toBeNull();
  });

  it("clicking Start from another column goes back to the default rather than storing it", () => {
    expect(nextTripSort({ key: "duration", dir: "desc" }, "start")).toBeNull();
  });
});

describe("effectiveTripSort", () => {
  it("shows Start ascending when nothing is chosen", () => {
    expect(effectiveTripSort(null)).toEqual(DEFAULT_TRIP_SORT);
    expect(effectiveTripSort({ key: "route", dir: "desc" })).toEqual({ key: "route", dir: "desc" });
  });
});

describe("URL round trip", () => {
  it("reads and writes sort and dir", () => {
    const sort = { key: "occupancy", dir: "desc" } as const;
    const params = new URLSearchParams(tripSortToParams(sort));
    expect(params.toString()).toBe("sort=occupancy&dir=desc");
    expect(tripSortFromParams(params)).toEqual(sort);
  });

  it("writes nothing for the default order", () => {
    expect(tripSortToParams(null)).toEqual({});
  });

  it("treats a missing or bad direction as ascending", () => {
    expect(tripSortFromParams(new URLSearchParams("sort=route"))).toEqual({ key: "route", dir: "asc" });
    expect(tripSortFromParams(new URLSearchParams("sort=route&dir=sideways"))).toEqual({
      key: "route",
      dir: "asc",
    });
  });

  it("ignores an unknown column and normalises the explicit default to none", () => {
    expect(tripSortFromParams(new URLSearchParams("sort=colour&dir=desc"))).toBeNull();
    expect(tripSortFromParams(new URLSearchParams(""))).toBeNull();
    expect(tripSortFromParams(new URLSearchParams("sort=start&dir=asc"))).toBeNull();
    expect(tripSortFromParams(new URLSearchParams("sort=start&dir=desc"))).toEqual({
      key: "start",
      dir: "desc",
    });
  });
});

describe("tripSortToQuery", () => {
  it("maps to the API's parameter names, and to nothing when unsorted", () => {
    expect(tripSortToQuery({ key: "on_time", dir: "desc" })).toEqual({
      sort_by: "on_time",
      sort_dir: "desc",
    });
    expect(tripSortToQuery(null)).toEqual({ sort_by: undefined, sort_dir: undefined });
  });
});
