"use client";
import { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  useOverview,
  useOnTime,
  useOnTimeTrend,
  useHeatmap,
  useDistribution,
  useWorstStops,
  useFrequency,
  useScheduleFrequency,
  useOccupancy,
  useAlerts,
  useRoutes,
  useLimits,
} from "@/lib/hooks";
import type { DashboardRange, RouteScope } from "@/lib/api";
import {
  DEFAULT_DASHBOARD_RANGE_LIMITS,
  addDays,
  fromDateStr,
  isPresetActive,
  presetRange,
  spanDays,
  toDateStr,
  type DashboardRangeLimits,
  type DashboardRangePreset,
} from "@/lib/dashboardDateRange";
import {
  EMPTY_DASHBOARD_FILTERS,
  dashboardFilterChips,
  resolveDashboardRouteIds,
  type DashboardFilters,
} from "@/lib/dashboardFilters";
import DashboardFilterMenu from "@/components/dashboard/DashboardFilterMenu";
import DashboardDateRangePicker from "@/components/dashboard/DashboardDateRangePicker";
import { ActiveFilterChip } from "@/components/ui/FilterControls";
import { Card, SectionHeading } from "@/components/ui/Card";
import KpiCard from "@/components/dashboard/KpiCard";
import FrequencyTable from "@/components/dashboard/FrequencyTable";
import DelayIncidents from "@/components/dashboard/DelayIncidents";
import TrendChart from "@/components/charts/TrendChart";
import Heatmap from "@/components/charts/Heatmap";
import type { HeatmapCell } from "@/lib/types";
import DistributionChart from "@/components/charts/DistributionChart";
import ScorecardTable from "@/components/charts/ScorecardTable";
import HeadwayChart from "@/components/charts/HeadwayChart";
import OccupancyChart from "@/components/charts/OccupancyChart";
import WorstStopsTable from "@/components/charts/WorstStopsTable";
import LoadingSpinner from "@/components/ui/LoadingSpinner";
import { formatDelayMin, onTimeColor } from "@/lib/utils";
import { useTheme } from "@/lib/useTheme";

function fmtSpan(hhmm: string | null | undefined): string {
  if (!hhmm) return "—";
  const [hStr, mStr] = hhmm.split(":");
  const h = parseInt(hStr, 10);
  const m = mStr ?? "00";
  const suffix = h < 12 ? "am" : "pm";
  const h12 = h === 0 ? 12 : h > 12 ? h - 12 : h;
  return `${h12}:${m}${suffix}`;
}

function delta(m?: { value: number; previous: number | null }): number | null {
  if (!m || m.previous === null || m.previous === undefined) return null;
  return m.value - m.previous;
}

// Quick day-count buttons, kept alongside the "Custom" calendar picker for
// arbitrary ranges.
const QUICK_DAY_OPTIONS = [1, 7, 30];
const quickDayPreset = (d: number): DashboardRangePreset => ({ label: `${d}d`, days: d });

export default function DashboardPage() {
  const router = useRouter();
  const { resolvedTheme } = useTheme();
  // Calendar range (date-only) — the Dashboard reads continuous aggregates,
  // so a wide window is cheap; defaults to the last 7 days.
  const [now] = useState(() => new Date());
  const [range, setRange] = useState<DashboardRange>(() =>
    presetRange(quickDayPreset(7), DEFAULT_DASHBOARD_RANGE_LIMITS, now),
  );
  const [filters, setFilters] = useState<DashboardFilters>(EMPTY_DASHBOARD_FILTERS);
  const [occDirection, setOccDirection] = useState<number | undefined>(undefined);

  const limitsQuery = useLimits();
  const rangeLimits: DashboardRangeLimits = useMemo(
    () =>
      limitsQuery.data
        ? {
            maxSpanDays: limitsQuery.data.dashboard_max_span_days,
            retentionDays: limitsQuery.data.data_retention_days,
          }
        : DEFAULT_DASHBOARD_RANGE_LIMITS,
    [limitsQuery.data],
  );

  const rangeSpanDays = useMemo(() => {
    const s = fromDateStr(range.start);
    const e = fromDateStr(range.end);
    return s && e ? spanDays(s, e) : 1;
  }, [range]);
  const granularity = rangeSpanDays <= 2 ? "hour" : "day";

  // The hour×day-of-week heatmap reads sparse under a short window, so it
  // always requests at least 14 days even when the picked range is shorter —
  // the other cards use the picked range exactly.
  const heatmapRange = useMemo<DashboardRange>(() => {
    if (rangeSpanDays >= 14) return range;
    const e = fromDateStr(range.end);
    if (!e) return range;
    return { start: toDateStr(addDays(e, -13)), end: range.end };
  }, [range, rangeSpanDays]);

  function handleRangeChange(nextStart: string, nextEnd: string) {
    setRange({ start: nextStart, end: nextEnd });
  }

  const routes = useRoutes();

  const sortedRoutes = useMemo(() => {
    const list = routes.data?.routes ?? [];
    return [...list].sort((a, b) =>
      a.short_name.localeCompare(b.short_name, undefined, { numeric: true })
    );
  }, [routes.data]);

  // Modes resolve to route_ids on the server; the API takes both, so the scope
  // it sends is just the raw picks. The client-side expansion is only for
  // deciding which single-route cards can render.
  const scope: RouteScope = useMemo(
    () => ({ routeIds: filters.routeIds, modes: filters.modes }),
    [filters],
  );
  const effectiveRouteIds = useMemo(
    () => resolveDashboardRouteIds(filters, sortedRoutes),
    [filters, sortedRoutes],
  );
  const singleRouteId = effectiveRouteIds.length === 1 ? effectiveRouteIds[0] : undefined;

  const overview = useOverview(range, scope);
  const alerts = useAlerts();
  const trend = useOnTimeTrend(range, scope, granularity);
  const heatmap = useHeatmap(heatmapRange, scope);
  const distribution = useDistribution(range, scope);
  const scorecard = useOnTime(range, scope);
  const worstStops = useWorstStops(range, scope, 10);
  const frequency = useFrequency(scope);
  const scheduleFreq = useScheduleFrequency(singleRouteId);
  const occupancy = useOccupancy(range, singleRouteId, occDirection);

  const routeName = (rid: string) =>
    sortedRoutes.find((r) => r.route_id === rid)?.short_name;

  const chips = dashboardFilterChips(filters, routeName);

  /** Add a route to the applied set — used by the scorecard row click. Immediate,
   *  the way removing a chip is. */
  const addRoute = (rid: string) =>
    setFilters((f) => (f.routeIds.includes(rid) ? f : { ...f, routeIds: [...f.routeIds, rid] }));

  /** The route slice of a `/trips` link, so a drill-down keeps the filter. Modes
   *  are expanded to their route_ids here — the Trip Explorer's own `modes`
   *  vocabulary is coarser (rail/bus/other). */
  const tripScopeParams = (): Record<string, string> =>
    effectiveRouteIds.length ? { routes: effectiveRouteIds.join(",") } : {};

  function handleTrendPointClick(point: { t: string }) {
    const start = new Date(point.t);
    const end = new Date(point.t);
    if (granularity === "hour") {
      end.setHours(end.getHours() + 1);
    } else {
      end.setDate(end.getDate() + 1);
    }
    const qs = new URLSearchParams({ start: start.toISOString(), end: end.toISOString(), ...tripScopeParams() });
    router.push(`/trips?${qs}`);
  }

  function handleHeatmapCellClick(cell: HeatmapCell) {
    const now = new Date();
    const startOfHour = new Date(now);
    startOfHour.setMinutes(0, 0, 0);
    const dowMap: Record<string, number> = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };
    for (let hoursBack = 0; hoursBack < 7 * 24; hoursBack++) {
      const candidate = new Date(startOfHour.getTime() - hoursBack * 3600 * 1000);
      const parts = new Intl.DateTimeFormat("en-US", {
        timeZone: "America/Denver",
        weekday: "short",
        hour: "numeric",
        hour12: false,
      }).formatToParts(candidate);
      const weekday = parts.find((p) => p.type === "weekday")?.value ?? "";
      const rawHour = parseInt(parts.find((p) => p.type === "hour")?.value ?? "0", 10);
      const hour = rawHour === 24 ? 0 : rawHour;
      const dow = dowMap[weekday] ?? -1;
      if (dow === cell.dow && hour === cell.hour) {
        const end = new Date(candidate.getTime() + 3600 * 1000);
        const qs = new URLSearchParams({ start: candidate.toISOString(), end: end.toISOString(), ...tripScopeParams() });
        router.push(`/trips?${qs}`);
        return;
      }
    }
  }

  function handleFrequencyRowClick(rowRouteId: string) {
    const end = new Date();
    const start = new Date(Date.now() - 60 * 60 * 1000);
    const qs = new URLSearchParams({
      start: start.toISOString(),
      end: end.toISOString(),
      route_id: rowRouteId,
    });
    router.push(`/trips?${qs}`);
  }

  const ov = overview.data;
  const alertCount = alerts.data?.alerts.length ?? 0;
  const scopeLabel =
    chips.length === 0
      ? "all routes"
      : singleRouteId
        ? `Route ${routeName(singleRouteId) ?? singleRouteId}`
        : `${effectiveRouteIds.length} routes`;

  return (
    <div className="mx-auto w-full max-w-7xl space-y-6 overflow-x-hidden px-4 pb-6 pt-24 text-fg">
      {/* Header + controls */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-fg">Transit Performance Dashboard</h1>
          <p className="text-sm text-fg-subtle">
            Reliability, frequency, service delivery &amp; demand across RTD · {scopeLabel}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <DashboardFilterMenu routes={sortedRoutes} filters={filters} onChange={setFilters} />
          <div className="flex items-center gap-1 text-sm">
            {QUICK_DAY_OPTIONS.map((d) => {
              const preset = quickDayPreset(d);
              const active = isPresetActive(range.start, range.end, preset, rangeLimits, now);
              return (
                <button
                  key={d}
                  onClick={() => setRange(presetRange(preset, rangeLimits, now))}
                  className={`rounded px-3 py-1 font-medium transition-colors ${
                    active
                      ? "bg-accent text-accent-ink"
                      : "bg-card border border-line text-fg-muted hover:border-accent"
                  }`}
                >
                  {d}d
                </button>
              );
            })}
          </div>
          <DashboardDateRangePicker
            start={range.start}
            end={range.end}
            onChange={handleRangeChange}
            limits={rangeLimits}
            now={now}
          />
        </div>
      </div>

      {chips.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="mr-1 text-xs font-medium text-fg-subtle">Filtering by</span>
          {chips.map((chip) => (
            <ActiveFilterChip
              key={chip.id}
              label={chip.label}
              onRemove={() => setFilters(chip.next)}
            />
          ))}
          <button
            type="button"
            onClick={() => setFilters(EMPTY_DASHBOARD_FILTERS)}
            className="ml-1 text-xs font-medium text-fg-subtle underline-offset-2 transition-colors hover:text-fg hover:underline"
          >
            Clear all
          </button>
        </div>
      )}

      {/* KPIs */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <KpiCard title="Routes Tracked" value={ov ? String(ov.routes_tracked) : "—"} />
        <KpiCard
          title="On-Time Rate"
          value={ov ? `${ov.on_time_pct.value.toFixed(1)}%` : "—"}
          subtitle="± 5 mins"
          accentColor={ov ? onTimeColor(ov.on_time_pct.value, resolvedTheme) : undefined}
        />
        <KpiCard
          title="Avg Delay"
          value={ov ? formatDelayMin(ov.avg_delay_seconds.value) : "—"}
          lowerIsBetter
        />
        <KpiCard
          title="Stuck Alerts"
          value={alerts.isLoading ? "…" : String(alertCount)}
          accentColor={alertCount > 0 ? "rgb(var(--fg))" : "rgb(var(--ok))"}
        />
      </div>

      {/* ── Reliability ─────────────────────────────────────────────── */}
      <h2 className="pt-2 text-lg font-bold text-fg-subtle">Reliability</h2>

      <Card>
        <SectionHeading
          title="On-Time Performance Trend"
          subtitle={granularity === "hour" ? "Hourly" : "Daily"}
          hint="Bars show average delay. The dashed line marks RTD's 80% on-time target."
        />
        {trend.isLoading ? <LoadingSpinner /> : <TrendChart points={trend.data?.points ?? []} granularity={granularity} onPointClick={handleTrendPointClick} />}
      </Card>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Card>
          <SectionHeading title="Service Reliability" />
          {heatmap.isLoading ? <LoadingSpinner /> : <Heatmap cells={heatmap.data?.cells ?? []} metric="ontime" onCellClick={handleHeatmapCellClick} />}
        </Card>
        <Card>
          <SectionHeading
            title="Delay Distribution"
            subtitle={
              distribution.data
                ? `Avg ${formatDelayMin(distribution.data.avg_delay_seconds)} · ±${(distribution.data.stddev_seconds / 60).toFixed(1)}m`
                : "How early/late arrivals fall"
            }
          />
          {distribution.isLoading ? <LoadingSpinner /> : <DistributionChart bins={distribution.data?.bins ?? []} />}
        </Card>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Card>
          <SectionHeading
            title="Route Reliability Scorecard"
            hint="Click a route to filter the whole dashboard."
          />
          {scorecard.isLoading ? (
            <LoadingSpinner />
          ) : (
            <ScorecardTable routes={scorecard.data?.routes ?? []} onSelectRoute={addRoute} />
          )}
        </Card>
        <Card>
          <SectionHeading title="Worst Stops by Delay" />
          {worstStops.isLoading ? <LoadingSpinner /> : <WorstStopsTable stops={worstStops.data?.stops ?? []} />}
        </Card>
      </div>

      {/* ── Frequency & Service Delivery ────────────────────────────── */}
      <h2 className="pt-2 text-lg font-bold text-fg-subtle">Frequency &amp; Service Delivery</h2>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Card>
          <SectionHeading
            title="Scheduled Frequency by Hour"
            subtitle={
              singleRouteId && scheduleFreq.data?.routes[0]
                ? `Minutes between vehicles (weekday) · Service ${fmtSpan(scheduleFreq.data.routes[0].span_start)} – ${fmtSpan(scheduleFreq.data.routes[0].span_end)}`
                : "Select a single route to view"
            }
          />
          {!singleRouteId ? (
            <p className="py-8 text-center text-sm text-fg-subtle">
              Filter to exactly one route to see its scheduled headways through the day.
            </p>
          ) : scheduleFreq.isLoading ? (
            <LoadingSpinner />
          ) : (
            <HeadwayChart headways={scheduleFreq.data?.routes[0]?.headways_by_hour ?? []} />
          )}
        </Card>
        <Card>
          <SectionHeading
            title="Current Frequency (Live)"
            hint="Estimated headway from active vehicles."
          />
          {frequency.isLoading ? <LoadingSpinner /> : <FrequencyTable routes={frequency.data?.routes ?? []} onRowClick={handleFrequencyRowClick} />}
        </Card>
      </div>

      {/* ── Live Demand ─────────────────────────────────────────────── */}
      <h2 className="pt-2 text-lg font-bold text-fg-subtle">Live Demand</h2>

      <Card>
        <SectionHeading
          title="Live Crowding"
          subtitle={
            singleRouteId && scheduleFreq.data?.routes[0]
              ? `GTFS-RT occupancy · Service ${fmtSpan(scheduleFreq.data.routes[0].span_start)} – ${fmtSpan(scheduleFreq.data.routes[0].span_end)}`
              : "GTFS-RT occupancy status codes · % of samples by hour"
          }
        />
        {occupancy.isLoading ? (
          <LoadingSpinner />
        ) : occupancy.data ? (
          <OccupancyChart
            data={occupancy.data}
            direction={occDirection}
            onDirectionChange={setOccDirection}
          />
        ) : null}
      </Card>

      {/* ── Live alerts ─────────────────────────────────────────────── */}
      <Card>
        <SectionHeading
          title="Stuck Vehicle Alerts"
          hint="Vehicles stationary beyond the alert threshold."
        />
        {alerts.isLoading ? <LoadingSpinner /> : <DelayIncidents alerts={alerts.data?.alerts ?? []} />}
      </Card>
    </div>
  );
}
