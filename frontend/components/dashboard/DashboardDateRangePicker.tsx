"use client";
import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { cn } from "@/lib/utils";
import { useIsPhone } from "@/lib/useMediaQuery";
import {
  addDays,
  calendarDays,
  clampDateStr,
  describeDashboardLimits,
  fromDateStr,
  isDaySelectable,
  overallBounds,
  startOfDay,
  toDateStr,
  type DashboardRangeLimits,
  type FieldBounds,
} from "@/lib/dashboardDateRange";

interface Props {
  /** `"YYYY-MM-DD"`, date-only (no time-of-day). */
  start: string;
  end: string;
  onChange: (start: string, end: string) => void;
  limits: DashboardRangeLimits;
  now: Date;
  id?: string;
}

const WEEKDAYS = ["S", "M", "T", "W", "T", "F", "S"];
const MONTH_YEAR = new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" });
const FULL_DATE = new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric" });

type RangeRole = "start" | "end" | "single" | "in-range" | "none";

// The desktop popover's fixed width (no presets column, just the calendar) —
// used both for the Tailwind class and for clamping its position on-screen.
const POPOVER_WIDTH = 320;

/**
 * The Dashboard's "Custom" date-range field: a plain trigger (never showing
 * the picked dates — the 1d/7d/30d quick buttons beside it already show what's
 * active) opening one calendar-only panel — a date-only sibling of the Trip
 * Explorer's `DateRangePicker`, with no presets of its own and no anchoring to
 * "now": unlike the trips picker's hour-scale windows, this can express any
 * past `[start, end]`, because the Dashboard's analytics endpoints read
 * continuous aggregates rather than raw hypertable rows.
 *
 * The desktop panel is portaled to `document.body` and positioned from the
 * trigger's own screen rect (like the phone sheet already is) rather than
 * relying on CSS `absolute` positioning inside the page — the page wrapper
 * clips overflow (`overflow-x-hidden`), which was cutting the panel off.
 *
 * Every interaction commits straight through `onChange` — there's no
 * internal draft — same "pick, it's applied" behaviour as `DateRangePicker`.
 */
export default function DashboardDateRangePicker({ start, end, onChange, limits, now, id }: Props) {
  const isPhone = useIsPhone();
  const [open, setOpen] = useState(false);
  const [mounted, setMounted] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const reactId = useId();
  const labelId = `${reactId}-label`;
  const triggerId = `${reactId}-trigger`;

  useEffect(() => setMounted(true), []);

  const startDate = fromDateStr(start);
  const endDate = fromDateStr(end);

  // Two-click range selection, mirroring DateRangePicker: `pendingStart` holds
  // the first-clicked day while a second click is awaited, and resets
  // whenever the panel opens or closes.
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

  useEffect(() => {
    if (!open || !isPhone) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, [open, isPhone]);

  // Desktop panel is portaled to <body> and positioned from the trigger's own
  // screen rect, clamped to stay on-screen — CSS `absolute` positioning inside
  // the page gets clipped by the page wrapper's `overflow-x-hidden`.
  const [popoverPos, setPopoverPos] = useState<{ top: number; left: number } | null>(null);
  const recomputePopoverPos = useCallback(() => {
    const rect = containerRef.current?.getBoundingClientRect();
    if (!rect) return;
    const left = Math.min(Math.max(8, rect.left), window.innerWidth - POPOVER_WIDTH - 8);
    setPopoverPos({ top: rect.bottom + 4, left });
  }, []);

  useLayoutEffect(() => {
    if (!open || isPhone) return;
    recomputePopoverPos();
    window.addEventListener("resize", recomputePopoverPos);
    window.addEventListener("scroll", recomputePopoverPos, true);
    return () => {
      window.removeEventListener("resize", recomputePopoverPos);
      window.removeEventListener("scroll", recomputePopoverPos, true);
    };
  }, [open, isPhone, recomputePopoverPos]);

  // The full retention window — bounds month paging and which days can begin
  // a fresh pick. Independent of the max-span cap (see maxEndDayFor).
  const bounds: FieldBounds = useMemo(() => overallBounds(limits, now), [limits, now]);
  const overallMin = fromDateStr(bounds.min);
  const overallMax = fromDateStr(bounds.max);

  // The latest day the max span reaches from `anchor` — real, clickable data;
  // the picker never disables a day for being too far forward, it just stops
  // treating it as a valid *end* (see handleDayClick).
  const maxEndDayFor = useCallback(
    (anchor: Date): Date => {
      const capped = addDays(anchor, limits.maxSpanDays - 1);
      return overallMax && capped.getTime() > overallMax.getTime() ? overallMax : capped;
    },
    [limits.maxSpanDays, overallMax],
  );

  function daySelectable(day: Date): boolean {
    return isDaySelectable(day, overallMin, overallMax);
  }

  const hoverEndDay = useMemo(() => {
    if (!pendingStart || !hoverDay) return null;
    const s = startOfDay(pendingStart).getTime();
    if (hoverDay.getTime() < s) return null;
    const maxEnd = maxEndDayFor(pendingStart);
    if (hoverDay.getTime() > maxEnd.getTime()) return maxEnd;
    return startOfDay(hoverDay);
  }, [pendingStart, hoverDay, maxEndDayFor]);

  function rangeRole(day: Date): RangeRole {
    const t = day.getTime();
    if (pendingStart) {
      const s = startOfDay(pendingStart).getTime();
      const e = hoverEndDay?.getTime();
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
      setPendingStart(startOfDay(day));
      return;
    }
    const s = startOfDay(pendingStart);
    const maxEnd = maxEndDayFor(pendingStart);
    const beyondMaxSpan = day.getTime() > maxEnd.getTime();
    if (day.getTime() < s.getTime() || beyondMaxSpan) {
      // Earlier than the pending start, or further out than the max span
      // reaches — either way not a valid *end*, so restart the pick.
      setPendingStart(startOfDay(day));
      return;
    }
    const newStart = clampDateStr(toDateStr(pendingStart), bounds);
    const newEnd = clampDateStr(toDateStr(day), bounds);
    onChange(newStart, newEnd);
    setPendingStart(null);
    setHoverDay(null);
  }

  const days = useMemo(() => calendarDays(viewMonth), [viewMonth]);
  const todayStart = startOfDay(now).getTime();

  const prevMonth = new Date(viewMonth.getFullYear(), viewMonth.getMonth() - 1, 1);
  const nextMonth = new Date(viewMonth.getFullYear(), viewMonth.getMonth() + 1, 1);
  const prevDisabled = Boolean(
    overallMin && new Date(viewMonth.getFullYear(), viewMonth.getMonth(), 0) < overallMin,
  );
  const nextDisabled = Boolean(overallMax && nextMonth > overallMax);

  const calendarBlock = (
    <div className="p-3">
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

      <div className="grid grid-cols-7 gap-y-0.5" onMouseLeave={() => setHoverDay(null)}>
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
          const maxEnd = pendingStart ? maxEndDayFor(pendingStart) : null;
          return days.map((day) => {
            const inMonth = day.getMonth() === viewMonth.getMonth();
            const selectable = daySelectable(day);
            const role = rangeRole(day);
            const isToday = day.getTime() === todayStart;
            const isEndpoint = role === "start" || role === "end" || role === "single";
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
                  role === "none" && "rounded",
                  !selectable && "cursor-not-allowed text-fg-subtle/35 line-through",
                  selectable && role === "none" && tooFarForward && "text-fg-muted hover:bg-raised",
                  selectable && role === "none" && !tooFarForward && inMonth && "text-fg hover:bg-raised",
                  selectable && role === "none" && !tooFarForward && !inMonth && "text-fg-subtle hover:bg-raised",
                  role === "in-range" && "bg-accent/15 text-fg",
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

      <p className="mt-3 text-center text-xs font-medium text-accent">
        {pendingStart ? "Now pick an end date" : " "}
      </p>
    </div>
  );

  const header = (
    <div className="border-b border-line px-4 py-2.5">
      <div className="flex items-center justify-between">
        <span className="text-sm font-bold text-fg">Custom date range</span>
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
      <p className="mt-1 text-xs text-fg-subtle">{describeDashboardLimits(limits)}</p>
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
      <span id={labelId} className="sr-only">
        Custom date range
      </span>
      <div ref={containerRef} className="relative">
        <button
          id={id ?? triggerId}
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-haspopup="dialog"
          aria-expanded={open}
          aria-labelledby={`${labelId} ${id ?? triggerId}`}
          title="Pick a custom date range"
          className={cn(
            fieldClass,
            "flex items-center gap-1.5 font-medium",
            open && "ring-2 ring-accent",
          )}
        >
          {/* Cog/settings icon — a custom range is a mode you switch into,
              not a value being displayed (the quick-day buttons beside this
              already show the active range). */}
          <svg className="h-3.5 w-3.5 shrink-0 text-fg-subtle" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
            <path
              fillRule="evenodd"
              d="M8.34 1.804A1 1 0 0 1 9.32 1h1.36a1 1 0 0 1 .98.804l.295 1.473c.497.144.971.342 1.416.587l1.25-.834a1 1 0 0 1 1.262.125l.962.962a1 1 0 0 1 .125 1.262l-.834 1.25c.245.445.443.919.587 1.416l1.473.295a1 1 0 0 1 .804.98v1.36a1 1 0 0 1-.804.98l-1.473.295a5.973 5.973 0 0 1-.587 1.416l.834 1.25a1 1 0 0 1-.125 1.262l-.962.962a1 1 0 0 1-1.262.125l-1.25-.834a5.98 5.98 0 0 1-1.416.587l-.295 1.473a1 1 0 0 1-.98.804H9.32a1 1 0 0 1-.98-.804l-.295-1.473a5.98 5.98 0 0 1-1.416-.587l-1.25.834a1 1 0 0 1-1.262-.125l-.962-.962a1 1 0 0 1-.125-1.262l.834-1.25a5.98 5.98 0 0 1-.587-1.416l-1.473-.295a1 1 0 0 1-.804-.98V9.32a1 1 0 0 1 .804-.98l1.473-.295c.144-.497.342-.971.587-1.416l-.834-1.25a1 1 0 0 1 .125-1.262l.962-.962a1 1 0 0 1 1.262-.125l1.25.834c.445-.245.919-.443 1.416-.587l.295-1.473ZM10 13a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z"
              clipRule="evenodd"
            />
          </svg>
          <span>Custom</span>
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
                aria-label="Custom date range"
                className="animate-sheet-in relative flex max-h-[88vh] flex-col rounded-t-2xl border-t border-line bg-card shadow-[0_-8px_40px_-12px_rgba(0,0,0,0.6)]"
              >
                <div className="flex justify-center pb-1 pt-2">
                  <span className="h-1 w-10 rounded-full bg-line-strong" />
                </div>
                {header}
                <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
                  {calendarBlock}
                </div>
                <div className="pb-[env(safe-area-inset-bottom)]">{footer}</div>
              </div>
            </div>,
            document.body,
          )}

        {open && !isPhone && mounted &&
          createPortal(
            <div
              ref={panelRef}
              role="dialog"
              aria-label="Custom date range"
              style={{
                position: "fixed",
                top: popoverPos?.top ?? -9999,
                left: popoverPos?.left ?? -9999,
                width: POPOVER_WIDTH,
                visibility: popoverPos ? "visible" : "hidden",
              }}
              className="animate-popover-in z-[1200] flex flex-col overflow-hidden rounded-xl border border-line-strong bg-card shadow-card"
            >
              {header}
              {calendarBlock}
              {footer}
            </div>,
            document.body,
          )}
      </div>
    </div>
  );
}
