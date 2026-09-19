import { describe, it, expect } from "vitest";
import {
  DASHBOARD_RANGE_PRESETS,
  DEFAULT_DASHBOARD_RANGE_LIMITS,
  addDays,
  calendarDays,
  clampDateStr,
  describeDashboardLimits,
  fromDateStr,
  isDaySelectable,
  isPresetActive,
  overallBounds,
  presetRange,
  spanDays,
  subtractMonths,
  toDateStr,
} from "@/lib/dashboardDateRange";

// A fixed "now" so bounds are deterministic. Date-only throughout — no time-of-day.
const NOW = new Date(2026, 8, 12); // 12 Sep 2026
const LIMITS = { maxSpanDays: 366, retentionDays: 365 };

describe("toDateStr / fromDateStr", () => {
  it("round-trips a local date through the YYYY-MM-DD format", () => {
    const d = new Date(2026, 0, 5);
    expect(toDateStr(d)).toBe("2026-01-05");
    expect(fromDateStr("2026-01-05")?.getTime()).toBe(d.getTime());
  });

  it("returns null for values that are not plain dates", () => {
    expect(fromDateStr("")).toBeNull();
    expect(fromDateStr("2026-01-05T00:00")).toBeNull();
    expect(fromDateStr("nonsense")).toBeNull();
  });
});

describe("subtractMonths", () => {
  it("subtracts calendar months, not a fixed number of days", () => {
    expect(toDateStr(subtractMonths(new Date(2026, 8, 12), 6))).toBe("2026-03-12");
  });

  it("handles a year rollover", () => {
    expect(toDateStr(subtractMonths(new Date(2026, 2, 1), 6))).toBe("2025-09-01");
  });
});

describe("spanDays", () => {
  it("counts a single day as a span of 1", () => {
    expect(spanDays(new Date(2026, 0, 1), new Date(2026, 0, 1))).toBe(1);
  });

  it("counts inclusively", () => {
    expect(spanDays(new Date(2026, 0, 1), new Date(2026, 0, 30))).toBe(30);
  });
});

describe("overallBounds", () => {
  it("reaches back a full retention window and up to today", () => {
    const bounds = overallBounds(LIMITS, NOW);
    expect(bounds.max).toBe("2026-09-12");
    expect(bounds.min).toBe(toDateStr(addDays(NOW, -(LIMITS.retentionDays - 1))));
  });
});

describe("isDaySelectable", () => {
  const min = new Date(2026, 8, 1);
  const max = new Date(2026, 8, 12);

  it("accepts days inside the window, inclusive of both ends", () => {
    expect(isDaySelectable(new Date(2026, 8, 1), min, max)).toBe(true);
    expect(isDaySelectable(new Date(2026, 8, 12), min, max)).toBe(true);
    expect(isDaySelectable(new Date(2026, 8, 6), min, max)).toBe(true);
  });

  it("rejects days either side of the window", () => {
    expect(isDaySelectable(new Date(2026, 7, 31), min, max)).toBe(false);
    expect(isDaySelectable(new Date(2026, 8, 13), min, max)).toBe(false);
  });

  it("treats a missing bound as unbounded on that side", () => {
    expect(isDaySelectable(new Date(1990, 0, 1), null, max)).toBe(true);
    expect(isDaySelectable(new Date(2090, 0, 1), min, null)).toBe(true);
  });
});

describe("calendarDays", () => {
  it("always returns six Sunday-aligned weeks", () => {
    const days = calendarDays(new Date(2026, 8, 1));
    expect(days).toHaveLength(42);
    expect(days[0].getDay()).toBe(0);
  });
});

describe("clampDateStr", () => {
  const bounds = { min: "2026-09-01", max: "2026-09-30" };

  it("leaves a value inside the window untouched", () => {
    expect(clampDateStr("2026-09-15", bounds)).toBe("2026-09-15");
  });

  it("pulls a value below the window up to the minimum", () => {
    expect(clampDateStr("2026-08-20", bounds)).toBe(bounds.min);
  });

  it("pulls a value above the window down to the maximum", () => {
    expect(clampDateStr("2026-10-05", bounds)).toBe(bounds.max);
  });

  it("falls back to the minimum for an unparseable value", () => {
    expect(clampDateStr("", bounds)).toBe(bounds.min);
  });
});

describe("presetRange", () => {
  it("Last 90 days reaches back exactly 90 calendar days, inclusive", () => {
    const { start, end } = presetRange(DASHBOARD_RANGE_PRESETS[0], LIMITS, NOW);
    expect(end).toBe("2026-09-12");
    expect(spanDays(fromDateStr(start)!, fromDateStr(end)!)).toBe(90);
  });

  it("Last 6 months uses calendar-month arithmetic, not a fixed day count", () => {
    const preset = DASHBOARD_RANGE_PRESETS.find((p) => p.label === "Last 6 months")!;
    const { start, end } = presetRange(preset, LIMITS, NOW);
    expect(end).toBe("2026-09-12");
    expect(start).toBe("2026-03-13"); // one day past 6 months back
  });

  it("Last year reaches back 12 calendar months", () => {
    const preset = DASHBOARD_RANGE_PRESETS.find((p) => p.label === "Last year")!;
    const { start, end } = presetRange(preset, LIMITS, NOW);
    expect(end).toBe("2026-09-12");
    expect(start).toBe("2025-09-13");
  });

  it("clamps to the retention window when that is the tighter limit", () => {
    const { start } = presetRange(
      DASHBOARD_RANGE_PRESETS.find((p) => p.label === "Last year")!,
      { maxSpanDays: 366, retentionDays: 30 },
      NOW,
    );
    expect(start).toBe(toDateStr(addDays(NOW, -29)));
  });

  it("covers every shipped preset without throwing", () => {
    for (const preset of DASHBOARD_RANGE_PRESETS) {
      const { start, end } = presetRange(preset, LIMITS, NOW);
      expect(fromDateStr(start)!.getTime()).toBeLessThanOrEqual(fromDateStr(end)!.getTime());
    }
  });
});

describe("isPresetActive", () => {
  it("matches the preset that produced the exact same range", () => {
    const preset = DASHBOARD_RANGE_PRESETS[0];
    const { start, end } = presetRange(preset, LIMITS, NOW);
    expect(isPresetActive(start, end, preset, LIMITS, NOW)).toBe(true);
  });

  it("rejects a range that only partly overlaps a preset", () => {
    const preset = DASHBOARD_RANGE_PRESETS[0];
    const other = DASHBOARD_RANGE_PRESETS[1];
    const { start, end } = presetRange(preset, LIMITS, NOW);
    expect(isPresetActive(start, end, other, LIMITS, NOW)).toBe(false);
  });
});

describe("describeDashboardLimits", () => {
  it("states the retention window in days", () => {
    expect(describeDashboardLimits(DEFAULT_DASHBOARD_RANGE_LIMITS)).toBe(
      "Range can be within the last 365 days.",
    );
  });

  it("reflects a non-default retention window", () => {
    expect(describeDashboardLimits({ maxSpanDays: 90, retentionDays: 30 })).toBe(
      "Range can be within the last 30 days.",
    );
  });
});
