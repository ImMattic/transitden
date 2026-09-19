import type { ComponentProps } from "react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import DateRangePicker from "@/components/ui/DateRangePicker";
import { presetRange, type RangeLimits } from "@/lib/dateRange";

// The component labels days and months with Intl, so build the expected
// strings the same way rather than hardcoding one locale's output.
const DAY_LABEL = new Intl.DateTimeFormat(undefined, {
  weekday: "short",
  month: "short",
  day: "numeric",
});
const MONTH_LABEL = new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" });

const dayLabel = (y: number, m: number, d: number) => DAY_LABEL.format(new Date(y, m, d));

// Wed 9 Sep 2026, 14:30 — fixed so every bound in these tests is deterministic.
const NOW = new Date(2026, 8, 9, 14, 30);
// Five days of retention keeps the window (4–9 Sep) inside one month, so the
// month-paging tests don't have to reason about a window that straddles two.
const LIMITS: RangeLimits = { maxSpanHours: 72, retentionDays: 5 };

function setup(props: Partial<ComponentProps<typeof DateRangePicker>> = {}) {
  const onChange = vi.fn();
  render(
    <DateRangePicker
      start="2026-09-08T10:00"
      end="2026-09-09T10:00"
      onChange={onChange}
      limits={LIMITS}
      now={NOW}
      {...props}
    />,
  );
  return { onChange };
}

const openPopover = async () => {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: /date.*time range/i }));
  return { user, dialog: screen.getByRole("dialog") };
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("DateRangePicker (custom calendar)", () => {
  it("opens on click and shows the range's month", async () => {
    setup();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    const { dialog } = await openPopover();
    expect(within(dialog).getByText(MONTH_LABEL.format(new Date(2026, 8, 1)))).toBeInTheDocument();
  });

  it("disables days outside the retention window", async () => {
    setup();
    const { dialog } = await openPopover();

    // Retention is 5 days back from 9 Sep, i.e. 4–9 Sep; 3 Sep is fully outside it.
    expect(within(dialog).getByLabelText(dayLabel(2026, 8, 3))).toBeDisabled();
    expect(within(dialog).getByLabelText(dayLabel(2026, 8, 5))).toBeEnabled();
  });

  it("stops month paging at the edges of the retention window", async () => {
    setup();
    const { dialog } = await openPopover();

    expect(within(dialog).getByRole("button", { name: /previous month/i })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: /next month/i })).toBeDisabled();
  });

  it("prompts for an end date after the first click, then commits both on the second", async () => {
    const { onChange } = setup();
    const { user, dialog } = await openPopover();

    await user.click(within(dialog).getByLabelText(dayLabel(2026, 8, 5)));
    expect(within(dialog).getByText(/pick an end date/i)).toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();

    await user.click(within(dialog).getByLabelText(dayLabel(2026, 8, 7)));
    // Time of day carries over from the previous start/end (10:00 on both).
    expect(onChange).toHaveBeenCalledWith("2026-09-05T10:00", "2026-09-07T10:00");
  });

  it("restarts the pick when the second click lands before the first", async () => {
    const { onChange } = setup();
    const { user, dialog } = await openPopover();

    await user.click(within(dialog).getByLabelText(dayLabel(2026, 8, 7)));
    // Earlier than 7 Sep: restarts rather than completing a (invalid) range.
    await user.click(within(dialog).getByLabelText(dayLabel(2026, 8, 5)));
    expect(onChange).not.toHaveBeenCalled();
    expect(within(dialog).getByText(/pick an end date/i)).toBeInTheDocument();

    await user.click(within(dialog).getByLabelText(dayLabel(2026, 8, 8)));
    expect(onChange).toHaveBeenCalledWith("2026-09-05T10:00", "2026-09-08T10:00");
  });

  it("dims (but never disables or strikes through) days past the max span", async () => {
    setup({ limits: { maxSpanHours: 24, retentionDays: 5 } });
    const { user, dialog } = await openPopover();

    await user.click(within(dialog).getByLabelText(dayLabel(2026, 8, 5)));
    // 8 Sep is well past the 24h cap from a 5 Sep 10:00 start, but it's still
    // real, in-retention data, so it stays enabled and merely reads dimmer.
    const tooFar = within(dialog).getByLabelText(dayLabel(2026, 8, 8));
    expect(tooFar).toBeEnabled();
    expect(tooFar.className).toContain("text-fg-muted");
    expect(tooFar.className).not.toContain("line-through");
    expect(tooFar.className).not.toContain("cursor-not-allowed");

    // A genuinely out-of-retention day is a different, stronger treatment.
    const outOfRetention = within(dialog).getByLabelText(dayLabel(2026, 8, 3));
    expect(outOfRetention).toBeDisabled();
    expect(outOfRetention.className).toContain("line-through");
  });

  it("restarts the pick when the second click lands beyond the max span, rather than clamping to it", async () => {
    const { onChange } = setup({ limits: { maxSpanHours: 24, retentionDays: 5 } });
    const { user, dialog } = await openPopover();

    await user.click(within(dialog).getByLabelText(dayLabel(2026, 8, 5)));
    // 8 Sep is past the 24h cap from a 5 Sep 10:00 start: restarts the pick
    // from 8 Sep instead of completing a range clamped down to fit.
    await user.click(within(dialog).getByLabelText(dayLabel(2026, 8, 8)));
    expect(onChange).not.toHaveBeenCalled();
    expect(within(dialog).getByText(/pick an end date/i)).toBeInTheDocument();

    await user.click(within(dialog).getByLabelText(dayLabel(2026, 8, 9)));
    expect(onChange).toHaveBeenCalledWith("2026-09-08T10:00", "2026-09-09T10:00");
  });

  it("commits a preset range and closes", async () => {
    const { onChange } = setup();
    const { user, dialog } = await openPopover();

    await user.click(within(dialog).getByRole("button", { name: "Last 3 hours" }));
    const expected = presetRange(3, LIMITS, NOW);
    expect(onChange).toHaveBeenCalledWith(expected.start, expected.end);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("marks the preset matching the current range as active", async () => {
    const preset = presetRange(6, LIMITS, NOW);
    setup({ start: preset.start, end: preset.end });
    const { dialog } = await openPopover();

    expect(within(dialog).getByRole("button", { name: "Last 6 hours" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(within(dialog).getByRole("button", { name: "Last hour" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("disables the start hours that fall outside the retention window on a boundary day", async () => {
    // 4 Sep is only reachable from 14:30 onward (5-day retention from 9 Sep 14:30).
    setup({ start: "2026-09-04T20:00", end: "2026-09-09T10:00" });
    const { dialog } = await openPopover();

    const hours = within(dialog).getByLabelText(/start hour/i);
    // 13:00 (13:00–13:59) is entirely before the 14:30 cutoff; 20:00 is well
    // after it. (14:00 is only *partly* before it, so it stays enabled — same
    // "any overlap counts" rule the day grid uses.)
    expect(within(hours).getByRole("option", { name: "13" })).toBeDisabled();
    expect(within(hours).getByRole("option", { name: "20" })).toBeEnabled();
  });

  it("closes on Escape", async () => {
    setup();
    const { user } = await openPopover();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("closes on an outside click", async () => {
    setup();
    const { user } = await openPopover();
    await user.click(document.body);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("DateRangePicker (phone)", () => {
  function stubPhoneViewport() {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query.includes("max-width: 639px"),
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }));
  }

  it("opens the calendar as a bottom sheet rather than any native control", async () => {
    stubPhoneViewport();
    setup();
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: /date.*time range/i }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toBeInTheDocument();
    // No hand-off to a platform datetime-local control anywhere on the page.
    expect(document.querySelector('input[type="datetime-local"]')).not.toBeInTheDocument();
    expect(within(dialog).getByText(MONTH_LABEL.format(new Date(2026, 8, 1)))).toBeInTheDocument();
  });
});
