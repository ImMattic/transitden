"use client";

import { useEffect, useMemo, useState } from "react";
import FilterPanel from "@/components/ui/FilterPanel";
import {
  FilterChips,
  FilterIcon,
  FilterOptionList,
  FilterSection,
  withSelectedOptions,
  type ChipOption,
  type ListOption,
} from "@/components/ui/FilterControls";
import {
  DASHBOARD_MODES,
  DASHBOARD_MODE_LABELS,
  EMPTY_DASHBOARD_FILTERS,
  countActiveDashboardFilters,
  dashboardFiltersEqual,
  resolveDashboardRouteIds,
  type DashboardFilters,
  type DashboardMode,
} from "@/lib/dashboardFilters";
import type { RouteInfo } from "@/lib/types";
import { cn, routeColor } from "@/lib/utils";

interface Props {
  /** Every RTD route, from the static GTFS bundle. */
  routes: RouteInfo[];
  filters: DashboardFilters;
  onChange: (filters: DashboardFilters) => void;
}

const MODE_GROUP_LABEL: Record<string, string> = {
  bus: "Bus",
  light_rail: "Light rail",
  heavy_rail: "Heavy rail",
  commuter_rail: "Commuter rail",
  other: "Other",
};

const GROUP_ORDER = ["light_rail", "commuter_rail", "heavy_rail", "bus", "other"];

/**
 * The Dashboard's filter menu — an icon button that replaces the old route
 * search box. Unlike the Trip Explorer it filters by a *set* of routes (plus
 * bus / light-rail / commuter-rail quick-selects), so several routes' numbers
 * can be read together. Edits sit in a draft until "Apply filter", which then
 * closes the menu; a mode pick narrows the route list to that mode.
 */
export default function DashboardFilterMenu({ routes, filters, onChange }: Props) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<DashboardFilters>(filters);

  // Re-seed on open so a discarded edit doesn't linger; follow the applied set
  // while closed.
  useEffect(() => {
    if (!open) setDraft(filters);
  }, [open, filters]);

  const routeCountByType = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const r of routes) counts[r.type_name] = (counts[r.type_name] ?? 0) + 1;
    return counts;
  }, [routes]);

  const modeOptions: ChipOption[] = DASHBOARD_MODES.map((m) => ({
    value: m,
    label: DASHBOARD_MODE_LABELS[m],
    count: routeCountByType[m] ?? 0,
  }));

  // A mode pick hides routes of the other modes; a route already ticked stays so
  // it can still be unticked.
  const modeFilter = new Set(draft.modes);
  const routeOptions: ListOption[] = withSelectedOptions(
    routes
      .filter((r) => modeFilter.size === 0 || (modeFilter as Set<string>).has(r.type_name))
      .map((r) => ({
        value: r.route_id,
        label: r.short_name || r.route_id,
        sublabel: r.long_name,
        dotColor: routeColor(r.color),
        group: MODE_GROUP_LABEL[r.type_name] ?? "Other",
      })),
    draft.routeIds,
    (routeId) => ({ value: routeId, label: routeId, sublabel: "Not in the current GTFS bundle" }),
  );

  const groupRank = (label: string) =>
    GROUP_ORDER.findIndex((g) => MODE_GROUP_LABEL[g] === label);
  const byGroup = (a: ListOption, b: ListOption) =>
    groupRank(a.group ?? "") - groupRank(b.group ?? "");

  function toggleMode(value: string) {
    setDraft((d) => {
      const m = value as DashboardMode;
      const modes = d.modes.includes(m) ? d.modes.filter((v) => v !== m) : [...d.modes, m];
      return { ...d, modes };
    });
  }

  function toggleRoute(value: string) {
    setDraft((d) => ({
      ...d,
      routeIds: d.routeIds.includes(value)
        ? d.routeIds.filter((v) => v !== value)
        : [...d.routeIds, value],
    }));
  }

  function setManyRoutes(values: string[], on: boolean) {
    setDraft((d) => ({
      ...d,
      routeIds: on
        ? [...new Set([...d.routeIds, ...values])]
        : d.routeIds.filter((v) => !values.includes(v)),
    }));
  }

  const appliedCount = countActiveDashboardFilters(filters);
  const draftCount = countActiveDashboardFilters(draft);
  const previewRoutes = resolveDashboardRouteIds(draft, routes).length;

  return (
    <div className="relative flex items-center">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-label="Filter dashboard"
        aria-expanded={open}
        className={cn(
          "press flex h-8 shrink-0 items-center justify-center gap-1 rounded border transition-[background-color,border-color,color] duration-200",
          appliedCount > 0 ? "px-2.5" : "w-8",
          appliedCount > 0 || open
            ? "border-accent bg-accent/10 text-accent"
            : "border-line bg-card text-fg-subtle hover:border-line-strong hover:text-fg-muted",
        )}
      >
        <FilterIcon className="h-4 w-4" />
        {appliedCount > 0 && (
          <span
            key={appliedCount}
            className="animate-badge-pop rounded-full bg-accent px-1.5 py-0.5 text-[10px] font-bold leading-none text-accent-ink"
          >
            {appliedCount}
          </span>
        )}
      </button>

      <FilterPanel
        open={open}
        onClose={() => setOpen(false)}
        title="Filter dashboard"
        subtitle="Every card updates for the routes you pick"
        activeCount={draftCount}
        align="right"
        onReset={() => {
          setDraft(EMPTY_DASHBOARD_FILTERS);
          onChange(EMPTY_DASHBOARD_FILTERS);
        }}
        onApply={() => {
          onChange(draft);
          setOpen(false);
        }}
        applyLabel="Apply filter"
        applyDisabled={dashboardFiltersEqual(draft, filters)}
        applyHint={
          previewRoutes === 0
            ? "all routes"
            : `${previewRoutes} route${previewRoutes === 1 ? "" : "s"}`
        }
      >
        <FilterSection stagger={0} title="Transit type" activeCount={draft.modes.length} defaultOpen>
          <FilterChips
            options={modeOptions}
            selected={draft.modes}
            onToggle={toggleMode}
            hideEmpty
          />
        </FilterSection>

        <FilterSection stagger={40} title="Routes" activeCount={draft.routeIds.length} defaultOpen>
          <FilterOptionList
            options={[...routeOptions].sort(byGroup)}
            selected={draft.routeIds}
            onToggle={toggleRoute}
            onSetMany={setManyRoutes}
            searchPlaceholder="Search routes…"
            emptyLabel="No routes match that search."
          />
        </FilterSection>
      </FilterPanel>
    </div>
  );
}
