import { describe, it, expect } from "vitest";
import {
  dayPackedFraction,
  dayTier,
  formatDays,
  rangeSpeedHeadline,
  rangeTierSummary,
} from "@/lib/storageTier";

describe("rangeTierSummary", () => {
  it("reports the whole range as loose when the boundary is before it", () => {
    const s = rangeTierSummary(
      "2026-09-20T00:00:00Z",
      "2026-09-21T00:00:00Z",
      "2026-09-19T00:00:00Z",
    );
    expect(s.known).toBe(true);
    expect(s.totalDays).toBeCloseTo(1);
    expect(s.packedDays).toBe(0);
    expect(s.looseDays).toBeCloseTo(1);
  });

  it("reports the whole range as packed when the boundary is after it", () => {
    const s = rangeTierSummary(
      "2026-09-10T00:00:00Z",
      "2026-09-13T00:00:00Z",
      "2026-09-20T00:00:00Z",
    );
    expect(s.packedDays).toBeCloseTo(3);
    expect(s.looseDays).toBe(0);
  });

  it("splits a range the boundary falls inside", () => {
    // 3-day range, boundary sits 0.6 days before the end.
    const s = rangeTierSummary(
      "2026-09-18T18:00:00Z",
      "2026-09-21T18:00:00Z",
      "2026-09-21T00:00:00Z",
    );
    expect(s.totalDays).toBeCloseTo(3);
    expect(s.packedDays).toBeCloseTo(2.25);
    expect(s.looseDays).toBeCloseTo(0.75);
  });

  it("is unknown when the boundary is null, without guessing a split", () => {
    const s = rangeTierSummary("2026-09-20T00:00:00Z", "2026-09-21T00:00:00Z", null);
    expect(s.known).toBe(false);
    expect(s.packedDays).toBe(0);
    expect(s.looseDays).toBe(0);
    // Total is still reported — it doesn't depend on the boundary.
    expect(s.totalDays).toBeCloseTo(1);
  });

  it("never reports a negative split for an inverted range", () => {
    const s = rangeTierSummary(
      "2026-09-21T00:00:00Z",
      "2026-09-20T00:00:00Z",
      "2026-09-19T00:00:00Z",
    );
    expect(s.totalDays).toBe(0);
    expect(s.packedDays).toBe(0);
    expect(s.looseDays).toBe(0);
  });
});

describe("rangeSpeedHeadline", () => {
  it("says nothing when the boundary is unknown", () => {
    expect(rangeSpeedHeadline({ known: false, totalDays: 3, packedDays: 0, looseDays: 0 })).toBeNull();
  });

  it("says nothing when no part of the range is packed", () => {
    expect(rangeSpeedHeadline({ known: true, totalDays: 1, packedDays: 0, looseDays: 1 })).toBeNull();
  });

  it("mentions today/yesterday only when some of the range is still loose", () => {
    const mixed = rangeSpeedHeadline({ known: true, totalDays: 3, packedDays: 2.4, looseDays: 0.6 });
    expect(mixed).toContain("Today and yesterday");

    const allPacked = rangeSpeedHeadline({ known: true, totalDays: 3, packedDays: 3, looseDays: 0 });
    expect(allPacked).not.toContain("Today and yesterday");
    expect(allPacked).toContain("takes a few seconds");
  });
});

describe("dayTier", () => {
  const day = new Date(2026, 8, 20); // local midnight, Sep 20 2026

  it("is unknown when the boundary is null", () => {
    expect(dayTier(day, null)).toBeNull();
  });

  it("is loose when the boundary is at or before the day's start", () => {
    expect(dayTier(day, day.toISOString())).toBe("loose");
    expect(dayTier(day, new Date(2026, 8, 19).toISOString())).toBe("loose");
  });

  it("is packed when the boundary is at or after the day's end", () => {
    expect(dayTier(day, new Date(2026, 8, 21).toISOString())).toBe("packed");
  });

  it("is mixed when the boundary falls inside the day", () => {
    const noon = new Date(2026, 8, 20, 12, 0);
    expect(dayTier(day, noon.toISOString())).toBe("mixed");
  });
});

describe("dayPackedFraction", () => {
  it("is 0 at the very start of the day and 1 at the very end", () => {
    const day = new Date(2026, 8, 20);
    expect(dayPackedFraction(day, day.toISOString())).toBe(0);
    expect(dayPackedFraction(day, new Date(2026, 8, 21).toISOString())).toBe(1);
  });

  it("is 0.5 when the boundary lands at noon", () => {
    const day = new Date(2026, 8, 20);
    const noon = new Date(2026, 8, 20, 12, 0);
    expect(dayPackedFraction(day, noon.toISOString())).toBeCloseTo(0.5);
  });
});

describe("formatDays", () => {
  it("formats zero and negative as 0 days", () => {
    expect(formatDays(0)).toBe("0 days");
    expect(formatDays(-1)).toBe("0 days");
  });

  it("singularizes exactly one day", () => {
    expect(formatDays(1)).toBe("1 day");
  });

  it("keeps one decimal place otherwise", () => {
    expect(formatDays(2.4)).toBe("2.4 days");
    expect(formatDays(0.6)).toBe("0.6 days");
  });

  it("drops a trailing .0", () => {
    expect(formatDays(3)).toBe("3 days");
  });
});
