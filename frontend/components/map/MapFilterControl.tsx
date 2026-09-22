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
  EMPTY_MAP_FILTERS,
  applyMapFilters,
  buildMapFacets,
  countActiveMapFilters,
  mapFiltersEqual,
  type HeadwayBand,
  type MapFilters,
  type MovementBand,
  type PunctualityBand,
  type TransitMode,
} from "@/lib/mapFilters";
import type { VehiclePosition } from "@/lib/types";
import { cn, headwayColor, occupancyLabel, OCCUPANCY_ORDER, routeColor } from "@/lib/utils";
import { useTheme } from "@/lib/useTheme";

interface Props {
  /** The whole live feed — option counts come from here, not the filtered view. */
  vehicles: VehiclePosition[];
  filters: MapFilters;
  onChange: (filters: MapFilters) => void;
}

/** The multi-select groups — every `MapFilters` key holding a list of values. */
type MapFilterListKey = Exclude<
  {
    [K in keyof MapFilters]: MapFilters[K] extends string[] ? K : never;
  }[keyof MapFilters],
  undefined
>;

const MODE_LABELS: Record<TransitMode, string> = {
  rail: "Rail",
  bus: "Bus",
  other: "Other",
};

const MODE_GROUP_ORDER: TransitMode[] = ["rail", "bus", "other"];

const HEADWAY_BANDS: { value: HeadwayBand; label: string; sample: number | null }[] = [
  { value: "under15", label: "Under 15 min", sample: 10 },
  { value: "to30", label: "15–30 min", sample: 20 },
  { value: "to60", label: "30–60 min", sample: 45 },
  { value: "over60", label: "60 min or more", sample: 99 },
  { value: "unknown", label: "Unknown", sample: null },
];

const PUNCTUALITY_BANDS: { value: PunctualityBand; label: string }[] = [
  { value: "early", label: "Early" },
  { value: "on_time", label: "On time" },
  { value: "late", label: "Late" },
  { value: "very_late", label: "15 min+ late" },
  { value: "unknown", label: "No data" },
];

const MOVEMENT_BANDS: { value: MovementBand; label: string }[] = [
  { value: "in_transit", label: "In transit" },
  { value: "stopped", label: "At a stop" },
  { value: "incoming", label: "Arriving" },
  { value: "unknown", label: "Unknown" },
];

/**
 * The map's filter button and menu.
 *
 * Edits go to a draft that only reaches the map on Apply, so the markers don't
 * churn while someone is still ticking boxes — the button previews how many
 * vehicles the draft would leave. Reset clears and commits at once, because a
 * Reset that needed a second confirming click reads as broken.
 */
export default function MapFilterControl({ vehicles, filters, onChange }: Props) {
  const { resolvedTheme } = useTheme();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<MapFilters>(filters);

  // Re-seed the draft each time the menu opens so a discarded edit doesn't
  // linger, and follow the applied set while it is closed.
  useEffect(() => {
    if (!open) setDraft(filters);
  }, [open, filters]);

  const facets = useMemo(() => buildMapFacets(vehicles), [vehicles]);

  const previewCount = useMemo(
    () => applyMapFilters(vehicles, draft).length,
    [vehicles, draft],
  );

  const appliedCount = countActiveMapFilters(filters);
  const draftCount = countActiveMapFilters(draft);

  // The chip/list groups all hold plain string lists, so one pair of helpers
  // covers them; the cast is what lets a computed key stay typed as MapFilters.
  function toggle(key: MapFilterListKey, value: string) {
    setDraft((d) => {
      const list = d[key] as string[];
      const next = list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
      return { ...d, [key]: next } as MapFilters;
    });
  }

  function setMany(key: MapFilterListKey, values: string[], on: boolean) {
    setDraft((d) => {
      const list = d[key] as string[];
      const next = on
        ? [...new Set([...list, ...values])]
        : list.filter((v) => !values.includes(v));
      return { ...d, [key]: next } as MapFilters;
    });
  }

  const modeOptions: ChipOption[] = MODE_GROUP_ORDER.map((m) => ({
    value: m,
    label: MODE_LABELS[m],
    count: facets.modes[m],
  }));

  // As other groups get narrowed, options that can no longer match drop out of
  // the route and vehicle lists — pick "Rail" and the buses disappear. Rows the
  // user already ticked stay (via withSelectedOptions) so a selection made
  // before the narrowing can still be undone.
  const modeFilter = new Set(draft.modes);
  const routeShortNamesSelected = new Set(
    facets.routes.filter((r) => draft.routeIds.includes(r.routeId)).map((r) => r.shortName),
  );

  // A route or vehicle can leave the feed at the end of its service while it is
  // still selected; keep those rows so the selection can be undone here.
  const routeOptions: ListOption[] = withSelectedOptions(
    facets.routes
      .filter((r) => modeFilter.size === 0 || modeFilter.has(r.mode))
      .map((r) => ({
        value: r.routeId,
        label: r.shortName,
        sublabel: r.longName,
        dotColor: routeColor(r.color),
        count: r.count,
        group: MODE_LABELS[r.mode],
      })),
    draft.routeIds,
    (routeId) => ({ value: routeId, label: routeId, sublabel: "Not currently running" }),
  );

  const vehicleOptions: ListOption[] = withSelectedOptions(
    facets.vehicles
      .filter((v) => modeFilter.size === 0 || modeFilter.has(v.mode))
      .filter(
        (v) => routeShortNamesSelected.size === 0 || routeShortNamesSelected.has(v.routeShortName),
      )
      .map((v) => ({
        value: v.key,
        label: `#${v.label}`,
        badge: { text: v.routeShortName, color: routeColor(v.routeColor) },
        group: MODE_LABELS[v.mode],
      })),
    draft.vehicleKeys,
    (key) => ({ value: key, label: `#${key}`, sublabel: "Not currently reporting" }),
  );

  const headwayOptions: ChipOption[] = HEADWAY_BANDS.map((b) => ({
    value: b.value,
    label: b.label,
    count: facets.headway[b.value],
    dotColor: headwayColor(b.sample, resolvedTheme),
  }));

  const occupancyOptions: ChipOption[] = OCCUPANCY_ORDER.map((key) => ({
    value: key,
    label: occupancyLabel(key),
    count: facets.occupancy[key] ?? 0,
  }));

  const punctualityOptions: ChipOption[] = PUNCTUALITY_BANDS.map((b) => ({
    value: b.value,
    label: b.label,
    count: facets.punctuality[b.value],
  }));

  const movementOptions: ChipOption[] = MOVEMENT_BANDS.map((b) => ({
    value: b.value,
    label: b.label,
    count: facets.movement[b.value],
  }));

  // Grouped lists read best rail-first; the facet builder sorts by name only.
  const groupRank = (label: string) =>
    MODE_GROUP_ORDER.findIndex((m) => MODE_LABELS[m] === label);
  const byGroup = (a: ListOption, b: ListOption) =>
    groupRank(a.group ?? "") - groupRank(b.group ?? "");

  return (
    <div className="relative flex items-center">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-label="Filter vehicles"
        aria-expanded={open}
        className={cn(
          // Icon-only: the funnel reads as "filter" on its own, and dropping the
          // label keeps the pill row from crowding search on a narrow screen.
          "press flex h-9 shrink-0 items-center justify-center gap-1 rounded-full border shadow-lg shadow-black/30 backdrop-blur-md transition-[background-color,border-color,color,box-shadow] duration-300 ease-out",
          appliedCount > 0 ? "px-2.5" : "w-9",
          appliedCount > 0 || open
            ? "border-accent bg-accent/20 text-accent"
            : "border-line bg-card/90 text-fg-subtle hover:border-line-strong hover:bg-card hover:text-fg-muted",
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
        title="Filter vehicles"
        subtitle={`${vehicles.length} vehicles reporting right now`}
        activeCount={draftCount}
        align="right"
        onReset={() => {
          setDraft(EMPTY_MAP_FILTERS);
          onChange(EMPTY_MAP_FILTERS);
        }}
        onApply={() => {
          onChange(draft);
          setOpen(false);
        }}
        applyLabel="Show"
        applyDisabled={mapFiltersEqual(draft, filters)}
        applyHint={`${previewCount} vehicle${previewCount === 1 ? "" : "s"}`}
      >
        <FilterSection stagger={0} title="Mode" activeCount={draft.modes.length} defaultOpen>
          <FilterChips
            options={modeOptions}
            selected={draft.modes}
            onToggle={(v) => toggle("modes", v)}
            hideEmpty
          />
        </FilterSection>

        <FilterSection stagger={40} title="Routes" activeCount={draft.routeIds.length}>
          <FilterOptionList
            options={[...routeOptions].sort(byGroup)}
            selected={draft.routeIds}
            onToggle={(v) => toggle("routeIds", v)}
            onSetMany={(values, on) => setMany("routeIds", values, on)}
            searchPlaceholder="Search routes…"
            emptyLabel="No routes match that search."
          />
        </FilterSection>

        <FilterSection
          stagger={80}
          title="Vehicles"
          activeCount={draft.vehicleKeys.length}
          hint="Fleet numbers currently reporting, with the route each is serving."
        >
          <FilterOptionList
            options={[...vehicleOptions].sort(byGroup)}
            selected={draft.vehicleKeys}
            onToggle={(v) => toggle("vehicleKeys", v)}
            onSetMany={(values, on) => setMany("vehicleKeys", values, on)}
            searchPlaceholder="Search vehicle or route…"
            emptyLabel="No vehicles match that search."
          />
        </FilterSection>

        <FilterSection
          stagger={120}
          title="Frequency"
          activeCount={draft.headway.length}
          hint="Current headway on the route, using the same colour steps as the markers."
        >
          <FilterChips
            options={headwayOptions}
            selected={draft.headway}
            onToggle={(v) => toggle("headway", v)}
            hideEmpty
          />
        </FilterSection>

        <FilterSection stagger={160} title="Occupancy" activeCount={draft.occupancy.length}>
          <FilterChips
            options={occupancyOptions}
            selected={draft.occupancy}
            onToggle={(v) => toggle("occupancy", v)}
            hideEmpty
          />
        </FilterSection>

        <FilterSection
          stagger={200}
          title="Punctuality"
          activeCount={draft.punctuality.length}
          hint="On time means within RTD's ±5 minutes of schedule."
        >
          <FilterChips
            options={punctualityOptions}
            selected={draft.punctuality}
            onToggle={(v) => toggle("punctuality", v)}
            hideEmpty
          />
        </FilterSection>

        <FilterSection stagger={240} title="Status" activeCount={draft.movement.length}>
          <FilterChips
            options={movementOptions}
            selected={draft.movement}
            onToggle={(v) => toggle("movement", v)}
            hideEmpty
          />
        </FilterSection>
      </FilterPanel>
    </div>
  );
}
