"use client";
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { cn } from "@/lib/utils";
import { useIsPhone } from "@/lib/useMediaQuery";
import {
  RANGE_PRESETS,
  calendarDays,
  clampLocal,
  describeLimits,
  endOfDay,
  fromLocalInput,
  isDaySelectable,
  isPresetActive,
  isWithin,
  presetRange,
  rangeBounds,
  startOfDay,
  toLocalInput,
  type FieldBounds,
  type RangeLimits,
} from "@/lib/dateRange";

interface Props {
  /** `"YYYY-MM-DDTHH:mm"` in local time. */
  start: string;
  end: string;
  onChange: (start: string, end: string) => void;
  limits: RangeLimits;
  now: Date;
  id?: string;
}

const WEEKDAYS = ["S", "M", "T", "W", "T", "F", "S"];

const MONTH_YEAR = new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" });
const FULL_DATE = new Intl.DateTimeFormat(undefined, {
  weekday: "short",
  month: "short",
  day: "numeric",
});
const TRIGGER_DATE = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
});
// Military time throughout — the trigger label and the hour/minute selects.
const TRIGGER_TIME = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

function hourLabel(h: number): string {
  return String(h).padStart(2, "0");
}

type RangeRole = "start" | "end" | "single" | "in-range" | "none";

/**
 * The Trip Explorer's date-range field: one trigger, one panel holding quick
 * presets, a single range-select calendar, and start/end time — replacing the
 * old pair of `DateTimePicker` fields (and, with it, the phone fallback to the
 * platform's own `datetime-local` control: this calendar is fully in-house on
 * every device, so range selection reads the same way everywhere).
 *
 * Every interaction commits straight through `onChange` — there's no internal
 * draft — mirroring the old field's "pick, it's applied" behaviour. The page's
 * own "Load trips" button is still what turns a committed range into a fetch.
 */
export default function DateRangePicker({ start, end, onChange, limits, now, id }: Props) {
  const isPhone = useIsPhone();
  const [open, setOpen] = useState(false);
  const [mounted, setMounted] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const reactId = useId();
  const labelId = `${reactId}-label`;
  const triggerId = `${reactId}-trigger`;

  useEffect(() => setMounted(true), []);

  const startDate = fromLocalInput(start);
  const endDate = fromLocalInput(end);

  // Two-click range selection. `pendingStart` is null once idle (showing the
  // committed range from props) and holds the first-clicked day, time carried
  // over from the current start, while a second click is awaited. It resets
  // whenever the panel opens or closes, so reopening never resumes an
  // abandoned pick and a click anywhere always starts from what's committed.
  const [pendingStart, setPendingStart] = useState<Date | null>(null);
  const [hoverDay, setHoverDay] = useState<Date | null>(null);

  const [viewMonth, setViewMonth] = useState<Date>(() => startOfDay(startDate ?? now));

  useEffect(() => {
    if (open) {
      setPendingStart(null);
      setHoverDay(null);
      const anchor = startDate ?? now;
      setViewMonth(new Date(anchor.getFullYear(), anchor.getMonth(), 1));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function onPointerDown(e: MouseEvent) {
      const target = e.target as Node;
      if (containerRef.current?.contains(target)) return;
      if (panelRef.current?.contains(target)) return;
      setOpen(false);
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  // Stop the page behind the sheet from scrolling with it.
  useEffect(() => {
    if (!open || !isPhone) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, [open, isPhone]);

  // The full retention window — [earliest retained instant, a minute before
  // now] — independent of any particular start, so it bounds month paging and
  // which days can begin a fresh pick.
  const overallBounds: FieldBounds = useMemo(
    () => rangeBounds(toLocalInput(now), limits, now).start,
    [limits, now],
  );
  const overallMin = fromLocalInput(overallBounds.min);
  const overallMax = fromLocalInput(overallBounds.max);

  // Where the *committed* end field sits right now — used both to validate an
  // edit to its own time-of-day and, below, to size the end hour/minute lists.
  const endBounds = useMemo(() => rangeBounds(start, limits, now).end, [start, limits, now]);
  const endBoundsMin = fromLocalInput(endBounds.min);
  const endBoundsMax = fromLocalInput(endBounds.max);

  const endBoundsFor = useCallback(
    (anchor: Date): FieldBounds => rangeBounds(toLocalInput(anchor), limits, now).end,
    [limits, now],
  );

  // The latest day the max span reaches from `anchor` — the picker never
  // disables days past this, it just stops treating them as a valid *end*
  // (see `handleDayClick`/`hoverEndDay`), so nothing here ever goes
  // greyed-out or strikethrough for being "too far".
  const maxEndDayFor = useCallback(
    (anchor: Date): Date | null => {
      const max = fromLocalInput(endBoundsFor(anchor).max);
      return max ? startOfDay(max) : null;
    },
    [endBoundsFor],
  );

  // Selectability is just the retention window (real data, not in the
  // future) — the max-span cap never disables a day, it only changes what
  // clicking one *does* (see `handleDayClick`).
  function daySelectable(day: Date): boolean {
    return isDaySelectable(day, overallMin, overallMax);
  }

  // Only offered as a preview once it would actually extend the pick
  // forward — hovering a day before the pending start previews a restart,
  // not a range — and capped at the max span so the bar maxes out at 3 days
  // rather than growing indefinitely; hovering past that shows no preview at
  // all, since a click out there restarts instead of completing.
  const hoverEndDay = useMemo(() => {
    if (!pendingStart || !hoverDay) return null;
    const s = startOfDay(pendingStart).getTime();
    if (hoverDay.getTime() < s) return null;
    const maxEnd = maxEndDayFor(pendingStart);
    if (maxEnd && hoverDay.getTime() > maxEnd.getTime()) return maxEnd;
    return startOfDay(hoverDay);
  }, [pendingStart, hoverDay, maxEndDayFor]);

  function rangeRole(day: Date): RangeRole {
    const t = day.getTime();
    if (pendingStart) {
      const s = startOfDay(pendingStart).getTime();
      const e = hoverEndDay?.getTime();
      // No forward hover yet (or hovering back onto the start itself): the
      // pending start is a lone selection, not one end of a bar — full circle,
      // not a half-rounded cap with nothing to connect to.
      if (e === undefined || e === s) return t === s ? "single" : "none";
      if (t === s) return "start";
      if (t === e) return "end";
      if (t > s && t < e) return "in-range";
      return "none";
    }
    if (!startDate || !endDate) return "none";
    const s = startOfDay(startDate).getTime();
    const e = startOfDay(endDate).getTime();
    if (s === e) return t === s ? "single" : "none";
    if (t === s) return "start";
    if (t === e) return "end";
    if (t > s && t < e) return "in-range";
    return "none";
  }

  function handleDayClick(day: Date) {
    if (!pendingStart) {
      const t = startDate ?? now;
      setPendingStart(
        new Date(day.getFullYear(), day.getMonth(), day.getDate(), t.getHours(), t.getMinutes()),
      );
      return;
    }
    const s = startOfDay(pendingStart);
    const maxEnd = maxEndDayFor(pendingStart);
    const beyondMaxSpan = Boolean(maxEnd && day.getTime() > maxEnd.getTime());
    if (day.getTime() < s.getTime() || beyondMaxSpan) {
      // Either earlier than the pending start, or further out than the max
      // span reaches — either way this isn't a valid *end* for that start,
      // so it starts a fresh pick of its own rather than clamping down to
      // whatever the cap allows.
      const t = startDate ?? now;
      setPendingStart(
        new Date(day.getFullYear(), day.getMonth(), day.getDate(), t.getHours(), t.getMinutes()),
      );
      return;
    }
    // Second click completes the range.
    const et = endDate ?? now;
    const rawEnd = new Date(day.getFullYear(), day.getMonth(), day.getDate(), et.getHours(), et.getMinutes());
    const newStartLocal = clampLocal(toLocalInput(pendingStart), overallBounds);
    const newEndLocal = clampLocal(toLocalInput(rawEnd), rangeBounds(newStartLocal, limits, now).end);
    onChange(newStartLocal, newEndLocal);
    setPendingStart(null);
    setHoverDay(null);
  }

  function pickPreset(hours: number) {
    const preset = presetRange(hours, limits, now);
    onChange(preset.start, preset.end);
    setPendingStart(null);
    setOpen(false);
  }

  function setStartTime(hours: number, minutes: number) {
    if (!startDate) return;
    const next = new Date(startDate.getFullYear(), startDate.getMonth(), startDate.getDate(), hours, minutes);
    const newStartLocal = clampLocal(toLocalInput(next), overallBounds);
    const newEndLocal = clampLocal(end, rangeBounds(newStartLocal, limits, now).end);
    onChange(newStartLocal, newEndLocal);
  }

  function setEndTime(hours: number, minutes: number) {
    if (!endDate) return;
    const next = new Date(endDate.getFullYear(), endDate.getMonth(), endDate.getDate(), hours, minutes);
    onChange(start, clampLocal(toLocalInput(next), endBounds));
  }

  // On the boundary days of the overall window only part of the clock is
  // reachable, so the start hour/minute lists disable what falls outside it.
  const startHourEnabled = (h: number): boolean => {
    if (!startDate) return true;
    const from = new Date(startDate.getFullYear(), startDate.getMonth(), startDate.getDate(), h, 0);
    const to = new Date(startDate.getFullYear(), startDate.getMonth(), startDate.getDate(), h, 59);
    return (!overallMin || to >= overallMin) && (!overallMax || from <= overallMax);
  };
  const startMinuteEnabled = (m: number): boolean => {
    if (!startDate) return true;
    const at = new Date(
      startDate.getFullYear(),
      startDate.getMonth(),
      startDate.getDate(),
      startDate.getHours(),
      m,
    );
    return isWithin(at, overallMin, overallMax);
  };

  const endHourEnabled = (h: number): boolean => {
    if (!endDate) return true;
    const from = new Date(endDate.getFullYear(), endDate.getMonth(), endDate.getDate(), h, 0);
    const to = new Date(endDate.getFullYear(), endDate.getMonth(), endDate.getDate(), h, 59);
    return (!endBoundsMin || to >= endBoundsMin) && (!endBoundsMax || from <= endBoundsMax);
  };
  const endMinuteEnabled = (m: number): boolean => {
    if (!endDate) return true;
    const at = new Date(
      endDate.getFullYear(),
      endDate.getMonth(),
      endDate.getDate(),
      endDate.getHours(),
      m,
    );
    return isWithin(at, endBoundsMin, endBoundsMax);
  };

  const days = useMemo(() => calendarDays(viewMonth), [viewMonth]);
  const todayStart = startOfDay(now).getTime();

  const prevMonth = new Date(viewMonth.getFullYear(), viewMonth.getMonth() - 1, 1);
  const nextMonth = new Date(viewMonth.getFullYear(), viewMonth.getMonth() + 1, 1);
  const prevDisabled = Boolean(
    overallMin && endOfDay(new Date(viewMonth.getFullYear(), viewMonth.getMonth(), 0)) < overallMin,
  );
  const nextDisabled = Boolean(overallMax && nextMonth > overallMax);

  const triggerLabel =
    startDate && endDate
      ? `${TRIGGER_DATE.format(startDate)}, ${TRIGGER_TIME.format(startDate)}  –  ${TRIGGER_DATE.format(endDate)}, ${TRIGGER_TIME.format(endDate)}`
      : "Select…";

  const presetsList = (
    <div className="flex shrink-0 gap-1.5 overflow-x-auto border-b border-line p-2.5 sm:w-32 sm:flex-col sm:overflow-visible sm:border-b-0 sm:border-r sm:p-3">
      {RANGE_PRESETS.map((preset) => {
        const active = !pendingStart && isPresetActive(start, end, preset.hours, limits, now);
        return (
          <button
            key={preset.hours}
            type="button"
            onClick={() => pickPreset(preset.hours)}
            aria-pressed={active}
            className={cn(
              "press shrink-0 rounded-md border px-2.5 py-1.5 text-left text-xs font-medium transition-[transform,background-color,border-color,color] duration-150 sm:w-full",
              active
                ? "border-accent bg-accent/15 text-accent"
                : "border-line bg-raised text-fg-muted hover:border-line-strong hover:text-fg",
            )}
          >
            {preset.label}
          </button>
        );
      })}
    </div>
  );

  const calendarBlock = (
    <div className="flex-1 p-3">
      <div className="mb-2 flex items-center justify-between">
        <button
          type="button"
          onClick={() => setViewMonth(prevMonth)}
          disabled={prevDisabled}
          aria-label="Previous month"
          className="grid h-7 w-7 place-items-center rounded text-fg-muted transition-colors hover:bg-raised hover:text-fg disabled:cursor-not-allowed disabled:opacity-30 disabled:hover:bg-transparent"
        >
          <svg className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
            <path fillRule="evenodd" d="M12.79 5.23a.75.75 0 0 1 0 1.06L9.06 10l3.73 3.71a.75.75 0 1 1-1.06 1.06l-4.25-4.24a.75.75 0 0 1 0-1.06l4.25-4.24a.75.75 0 0 1 1.06 0Z" clipRule="evenodd" />
          </svg>
        </button>
        <span className="text-sm font-semibold text-fg">{MONTH_YEAR.format(viewMonth)}</span>
        <button
          type="button"
          onClick={() => setViewMonth(nextMonth)}
          disabled={nextDisabled}
          aria-label="Next month"
          className="grid h-7 w-7 place-items-center rounded text-fg-muted transition-colors hover:bg-raised hover:text-fg disabled:cursor-not-allowed disabled:opacity-30 disabled:hover:bg-transparent"
        >
          <svg className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
            <path fillRule="evenodd" d="M7.21 14.77a.75.75 0 0 1 0-1.06L10.94 10 7.21 6.29a.75.75 0 1 1 1.06-1.06l4.25 4.24a.75.75 0 0 1 0 1.06l-4.25 4.24a.75.75 0 0 1-1.06 0Z" clipRule="evenodd" />
          </svg>
        </button>
      </div>

      {/* Plain buttons rather than an ARIA grid: each day already carries its
          full date as a label, and a half-built grid role reads worse than
          none. No horizontal gap: adjacent in-range cells need to touch so
          the highlight reads as one continuous bar, not separate tiles. */}
      <div
        className="grid grid-cols-7 gap-y-0.5"
        onMouseLeave={() => setHoverDay(null)}
      >
        {WEEKDAYS.map((d, i) => (
          <div
            key={`${d}-${i}`}
            className="grid h-7 place-items-center text-[11px] font-semibold uppercase text-fg-subtle"
            aria-hidden="true"
          >
            {d}
          </div>
        ))}
        {(() => {
          // Computed once per render rather than per day: same pending start
          // for every cell in this pass.
          const maxEnd = pendingStart ? maxEndDayFor(pendingStart) : null;
          return days.map((day) => {
            const inMonth = day.getMonth() === viewMonth.getMonth();
            const selectable = daySelectable(day);
            const role = rangeRole(day);
            const isToday = day.getTime() === todayStart;
            const isEndpoint = role === "start" || role === "end" || role === "single";
            // Real, clickable data — clicking it just restarts the pick from
            // there — but past what this start could reach as an *end*, so
            // it reads as dimmed rather than either full-strength or struck
            // through.
            const tooFarForward = Boolean(maxEnd && day.getTime() > maxEnd.getTime());
            return (
              <button
                key={day.toISOString()}
                type="button"
                disabled={!selectable}
                aria-current={isEndpoint ? "date" : undefined}
                aria-label={FULL_DATE.format(day)}
                onClick={() => handleDayClick(day)}
                onMouseEnter={() => setHoverDay(day)}
                className={cn(
                  "grid h-8 place-items-center text-sm tabular-nums transition-colors",
                  // Each role owns its own corner radius rather than one
                  // class overriding another — with plain clsx (no
                  // tailwind-merge) two conflicting radius utilities on one
                  // element resolve by Tailwind's internal generation order,
                  // not by which comes last here, so they must never coexist
                  // on the same button.
                  role === "none" && "rounded",
                  !selectable && "cursor-not-allowed text-fg-subtle/35 line-through",
                  // Two shades for role "none": full-strength for a plain
                  // in-month day, and the same "muted" grey for both a
                  // neighbouring-month padding day and a day that's real and
                  // clickable but too far forward to complete this pick — one
                  // grey standing for "not a normal pick" rather than two.
                  selectable && role === "none" && (tooFarForward || !inMonth) && "text-fg-muted hover:bg-raised",
                  selectable && role === "none" && !tooFarForward && inMonth && "text-fg hover:bg-raised",
                  // The bar: a flat, edge-to-edge tint between the two caps —
                  // continuous because the grid above has no column gap to
                  // break it up.
                  role === "in-range" && "bg-accent/15 text-fg",
                  // The caps: solid, and rounded only on the outward-facing
                  // side, so each one reads as the rounded end of the bar
                  // rather than a circle sitting apart from it. "single" (no
                  // second date picked yet, or a one-day range) has no bar
                  // on either side, so it rounds all the way round instead.
                  role === "start" && "rounded-l-full bg-accent font-semibold text-accent-ink hover:opacity-90",
                  role === "end" && "rounded-r-full bg-accent font-semibold text-accent-ink hover:opacity-90",
                  role === "single" && "rounded-full bg-accent font-semibold text-accent-ink hover:opacity-90",
                  role === "none" && isToday && selectable && "ring-1 ring-inset ring-line-strong",
                )}
              >
                {day.getDate()}
              </button>
            );
          });
        })()}
      </div>

      {pendingStart ? (
        <p className="mt-3 text-center text-xs font-medium text-accent">Now pick an end date</p>
      ) : (
        <div className="mt-3 flex flex-col gap-2 border-t border-line pt-3 sm:flex-row sm:items-center sm:gap-4">
          <div className="flex items-center gap-2">
            <span className="w-10 text-xs font-medium text-fg-subtle">Start</span>
            <select
              aria-label="Start hour"
              value={startDate ? startDate.getHours() : 0}
              onChange={(e) => setStartTime(Number(e.target.value), startDate?.getMinutes() ?? 0)}
              className="rounded border border-line bg-card px-1.5 py-1 text-sm text-fg focus:outline-none focus:ring-2 focus:ring-accent"
            >
              {Array.from({ length: 24 }, (_, h) => (
                <option key={h} value={h} disabled={!startHourEnabled(h)}>
                  {hourLabel(h)}
                </option>
              ))}
            </select>
            <select
              aria-label="Start minute"
              value={startDate ? startDate.getMinutes() : 0}
              onChange={(e) => setStartTime(startDate?.getHours() ?? 0, Number(e.target.value))}
              className="rounded border border-line bg-card px-1.5 py-1 text-sm text-fg focus:outline-none focus:ring-2 focus:ring-accent"
            >
              {Array.from({ length: 60 }, (_, m) => (
                <option key={m} value={m} disabled={!startMinuteEnabled(m)}>
                  {String(m).padStart(2, "0")}
                </option>
              ))}
            </select>
          </div>
          <div className="flex items-center gap-2">
            <span className="w-10 text-xs font-medium text-fg-subtle">End</span>
            <select
              aria-label="End hour"
              value={endDate ? endDate.getHours() : 0}
              onChange={(e) => setEndTime(Number(e.target.value), endDate?.getMinutes() ?? 0)}
              className="rounded border border-line bg-card px-1.5 py-1 text-sm text-fg focus:outline-none focus:ring-2 focus:ring-accent"
            >
              {Array.from({ length: 24 }, (_, h) => (
                <option key={h} value={h} disabled={!endHourEnabled(h)}>
                  {hourLabel(h)}
                </option>
              ))}
            </select>
            <select
              aria-label="End minute"
              value={endDate ? endDate.getMinutes() : 0}
              onChange={(e) => setEndTime(endDate?.getHours() ?? 0, Number(e.target.value))}
              className="rounded border border-line bg-card px-1.5 py-1 text-sm text-fg focus:outline-none focus:ring-2 focus:ring-accent"
            >
              {Array.from({ length: 60 }, (_, m) => (
                <option key={m} value={m} disabled={!endMinuteEnabled(m)}>
                  {String(m).padStart(2, "0")}
                </option>
              ))}
            </select>
          </div>
        </div>
      )}
    </div>
  );

  const header = (
    <div className="border-b border-line px-4 py-2.5">
      <div className="flex items-center justify-between">
        <span className="text-sm font-bold text-fg">Date &amp; time range</span>
        <button
          type="button"
          onClick={() => setOpen(false)}
          aria-label="Close date range picker"
          className="press -mr-1 rounded-full p-1 text-fg-subtle transition-colors hover:bg-raised hover:text-fg"
        >
          <svg className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
            <path d="M6.28 5.22a.75.75 0 0 0-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 1 0 1.06 1.06L10 11.06l3.72 3.72a.75.75 0 1 0 1.06-1.06L11.06 10l3.72-3.72a.75.75 0 0 0-1.06-1.06L10 8.94 6.28 5.22Z" />
          </svg>
        </button>
      </div>
      <p className="mt-1 text-xs text-fg-subtle">{describeLimits(limits)}</p>
    </div>
  );

  const footer = (
    <div className="flex items-center justify-end border-t border-line px-4 py-2.5">
      <button
        type="button"
        onClick={() => setOpen(false)}
        className="press rounded-md bg-accent px-4 py-1.5 text-sm font-bold text-accent-ink transition-opacity hover:opacity-90"
      >
        Done
      </button>
    </div>
  );

  const fieldClass =
    "rounded border border-line bg-card px-2 py-1.5 text-sm text-fg focus:outline-none focus:ring-2 focus:ring-accent";

  return (
    <div className="flex flex-col gap-1">
      <span id={labelId} className="text-xs font-medium text-fg-subtle">
        Date &amp; time range
      </span>
      <div ref={containerRef} className="relative">
        <button
          id={id ?? triggerId}
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-haspopup="dialog"
          aria-expanded={open}
          aria-labelledby={`${labelId} ${id ?? triggerId}`}
          title={triggerLabel}
          className={cn(
            fieldClass,
            // Fit the whole "start – end" label rather than a fixed width that
            // truncates it prematurely — grows with content, capped at the
            // container's edge (where `truncate` on the label below still
            // takes over as a last resort on a genuinely narrow screen).
            "flex w-fit max-w-full items-center gap-2 text-left tabular-nums",
            open && "ring-2 ring-accent",
          )}
        >
          <svg className="h-3.5 w-3.5 shrink-0 text-fg-subtle" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
            <path
              fillRule="evenodd"
              d="M6 2a.75.75 0 0 1 .75.75V4h6.5V2.75a.75.75 0 0 1 1.5 0V4h.25A2.25 2.25 0 0 1 17.25 6.25v9A2.25 2.25 0 0 1 15 17.5H5a2.25 2.25 0 0 1-2.25-2.25v-9A2.25 2.25 0 0 1 5 4h.25V2.75A.75.75 0 0 1 6 2ZM4.25 8v7.25c0 .414.336.75.75.75h10a.75.75 0 0 0 .75-.75V8H4.25Z"
              clipRule="evenodd"
            />
          </svg>
          <span className="truncate">{triggerLabel}</span>
        </button>

        {open && isPhone && mounted &&
          createPortal(
            <div className="fixed inset-0 z-[2000] flex flex-col justify-end">
              <button
                type="button"
                aria-label="Close date range picker"
                onClick={() => setOpen(false)}
                className="animate-scrim-in absolute inset-0 bg-black/55 backdrop-blur-[2px]"
              />
              <div
                ref={panelRef}
                role="dialog"
                aria-label="Date and time range"
                className="animate-sheet-in relative flex max-h-[88vh] flex-col rounded-t-2xl border-t border-line bg-card shadow-[0_-8px_40px_-12px_rgba(0,0,0,0.6)]"
              >
                <div className="flex justify-center pb-1 pt-2">
                  <span className="h-1 w-10 rounded-full bg-line-strong" />
                </div>
                {header}
                <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
                  <div className="flex flex-col">
                    {presetsList}
                    {calendarBlock}
                  </div>
                </div>
                <div className="pb-[env(safe-area-inset-bottom)]">{footer}</div>
              </div>
            </div>,
            document.body,
          )}

        {open && !isPhone && (
          <div
            ref={panelRef}
            role="dialog"
            aria-label="Date and time range"
            className="animate-popover-in absolute left-0 top-full z-[1200] mt-1 flex w-[34rem] max-w-[calc(100vw-1.5rem)] flex-col overflow-hidden rounded-xl border border-line-strong bg-card shadow-card"
          >
            {header}
            <div className="flex">
              {presetsList}
              {calendarBlock}
            </div>
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}
