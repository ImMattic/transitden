"use client";
import dynamic from "next/dynamic";
import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useVehicles, useStopInfo } from "@/lib/hooks";
import type { StopInfo, VehiclePosition } from "@/lib/types";
import VehicleDialog from "@/components/map/VehicleDialog";
import StopDialog from "@/components/map/StopDialog";
import MapStatusBar from "@/components/map/MapStatusBar";
import LoadingSpinner from "@/components/ui/LoadingSpinner";
import {
  EMPTY_MAP_FILTERS,
  applyMapFilters,
  vehicleKey,
  type MapFilters,
} from "@/lib/mapFilters";

// Leaflet must be loaded client-side only
const VehicleMap = dynamic(() => import("@/components/map/VehicleMap"), {
  ssr: false,
  loading: () => <LoadingSpinner label="Loading map…" />,
});

function HomePageInner() {
  const { data, isLoading, isError, dataUpdatedAt } = useVehicles();
  const [selected, setSelected] = useState<VehiclePosition | null>(null);
  const [selectedStopId, setSelectedStopId] = useState<string | null>(null);
  const [searchFlyTo, setSearchFlyTo] = useState<{ lat: number; lng: number; zoom?: number } | null>(null);
  const [filters, setFilters] = useState<MapFilters>(EMPTY_MAP_FILTERS);
  const searchParams = useSearchParams();

  const { data: selectedStop } = useStopInfo(selectedStopId ?? undefined);

  const flyTo = useMemo(() => {
    const lat = searchParams.get("lat");
    const lng = searchParams.get("lng");
    if (!lat || !lng) return null;
    return { lat: parseFloat(lat), lng: parseFloat(lng) };
  }, [searchParams]);

  const targetVehicleId = searchParams.get("vehicle_id");

  // Auto-select the vehicle referenced by the URL params once the vehicle list loads.
  useEffect(() => {
    if (!targetVehicleId || !data?.vehicles) return;
    const match = data.vehicles.find((v) => v.vehicle_id === targetVehicleId);
    if (match) setSelected(match);
  }, [targetVehicleId, data?.vehicles]);

  const handleSearchSelect = useCallback((vehicle: VehiclePosition) => {
    setSelectedStopId(null);
    setSelected(vehicle);
    if (vehicle.latitude !== null && vehicle.longitude !== null) {
      setSearchFlyTo({ lat: vehicle.latitude, lng: vehicle.longitude, zoom: 15 });
    }
  }, []);

  const handleSearchStopSelect = useCallback((stop: StopInfo) => {
    setSelected(null);
    setSelectedStopId(stop.stop_id);
    setSearchFlyTo({ lat: stop.stop_lat, lng: stop.stop_lon, zoom: 16 });
  }, []);

  // Clicking a stop marker on the map: keep the vehicle selected (so route stops
  // remain visible) but surface the stop dialog. Clicking the same stop again closes it.
  const handleMapStopClick = useCallback((stopId: string) => {
    setSelectedStopId((prev: string | null) => (prev === stopId ? null : stopId));
  }, []);

  // Stable identity so the memoized marker layer isn't rebuilt every render.
  const handleVehicleClick = useCallback((vehicle: VehiclePosition) => {
    setSelectedStopId(null);
    setSelected((prev) => {
      if (!prev) return vehicle;

      const prevKey = prev.vehicle_id ?? prev.trip_id;
      const nextKey = vehicle.vehicle_id ?? vehicle.trip_id;

      // Clicking the same vehicle again toggles the dialog closed.
      if (prevKey && nextKey && prevKey === nextKey) {
        return null;
      }

      if (
        !prevKey &&
        !nextKey &&
        prev.route_id === vehicle.route_id &&
        prev.vehicle_label &&
        vehicle.vehicle_label &&
        prev.vehicle_label === vehicle.vehicle_label
      ) {
        return null;
      }

      return vehicle;
    });
  }, []);

  const vehicles = useMemo(() => data?.vehicles ?? [], [data?.vehicles]);

  // The map draws the filtered set; the status pill and the filter menu still
  // see the whole feed, so "12 of 987" and the per-option counts stay truthful.
  const visibleVehicles = useMemo(
    () => applyMapFilters(vehicles, filters),
    [vehicles, filters],
  );

  // Picking a vehicle out of search or a deep link is an explicit request for
  // that one, so it keeps its marker even when the filters would hide it —
  // otherwise the map flies to an empty patch of Denver. Matched by identity,
  // not object reference: every poll hands back fresh objects, and appending a
  // stale twin of a vehicle already on the map would draw it twice.
  const mapVehicles = useMemo(() => {
    if (!selected) return visibleVehicles;
    const key = vehicleKey(selected);
    if (visibleVehicles.some((v) => vehicleKey(v) === key)) return visibleVehicles;
    return [...visibleVehicles, selected];
  }, [visibleVehicles, selected]);

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      {/* Status + search + filter widget — tucked beneath the nav island */}
      <MapStatusBar
        vehicles={vehicles}
        filteredVehicles={visibleVehicles}
        isLoading={isLoading}
        isError={isError}
        dataUpdatedAt={dataUpdatedAt}
        filters={filters}
        onFiltersChange={setFilters}
        onSelect={handleSearchSelect}
        onSelectStop={handleSearchStopSelect}
      />

      {/* Map — fills the whole area; the nav island and status pill float over it */}
      <div className="relative min-h-0 flex-1">
        {isError ? (
          <div className="absolute inset-0 flex items-center justify-center">
            <div className="text-center text-fg-subtle">
              <p className="text-lg font-medium text-fg">Backend unreachable</p>
              <p className="text-sm mt-1">Make sure the API server is running.</p>
            </div>
          </div>
        ) : (
          <VehicleMap
            vehicles={mapVehicles}
            onVehicleClick={handleVehicleClick}
            selectedVehicle={selected}
            flyTo={searchFlyTo ?? flyTo}
            selectedStop={selectedStop}
            onStopClick={handleMapStopClick}
          />
        )}

        {/* The headway colour key now lives inside the map itself, mounted as a
            Leaflet control between the attribution bar and the zoom buttons —
            see VehicleMap. */}

        {/* Vehicle dialog — hidden while a stop dialog is open */}
        {selected && !selectedStop && (
          <VehicleDialog vehicle={selected} onClose={() => setSelected(null)} />
        )}
        {/* Stop dialog — closing it returns to vehicle dialog if one was open */}
        {selectedStop && (
          <StopDialog
            stop={selectedStop}
            vehicles={visibleVehicles}
            onClose={() => setSelectedStopId(null)}
          />
        )}
      </div>
    </div>
  );
}

export default function HomePage() {
  return (
    <Suspense fallback={<LoadingSpinner label="Loading map…" />}>
      <HomePageInner />
    </Suspense>
  );
}
