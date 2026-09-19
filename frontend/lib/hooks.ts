"use client";
import { useQuery } from "@tanstack/react-query";
import {
  fetchVehicles,
  fetchVehiclesByRoute,
  fetchRoutes,
  fetchRailShapes,
  fetchRouteShape,
  fetchRouteStops,
  fetchStopsSearch,
  fetchStopInfo,
  fetchHistorical,
  fetchOnTime,
  fetchFrequency,
  fetchAlerts,
  fetchOverview,
  fetchOnTimeTrend,
  fetchHeatmap,
  fetchDistribution,
  fetchWorstStops,
  fetchServiceDelivery,
  fetchScheduleFrequency,
  fetchOccupancy,
  fetchRidership,
  fetchActiveVehicles,
  fetchVehicleTrip,
  fetchLimits,
  fetchGames,
  fetchSportsTeams,
  type HistoricalParams,
  type ActiveVehiclesParams,
  type VehicleTripParams,
  type RouteScope,
  type DashboardRange,
} from "./api";

// Analytics rollups change slowly (hourly/daily aggregates) — refresh every 5 min.
const ANALYTICS_INTERVAL = 300_000;

// Poll interval for real-time data (ms). RTD's GTFS-RT protobuf feed refreshes
// roughly every 30 seconds — fetching faster than the data changes is wasted work.
const REALTIME_INTERVAL = 30_000;

/**
 * The API's time-range caps. Fixed for the life of a deploy, so fetch once and
 * never revalidate; callers fall back to DEFAULT_RANGE_LIMITS while it loads.
 */
export function useLimits() {
  return useQuery({
    queryKey: ["limits"],
    queryFn: fetchLimits,
    staleTime: Infinity,
    retry: 1,
  });
}

export function useVehicles() {
  return useQuery({
    queryKey: ["vehicles"],
    queryFn: fetchVehicles,
    refetchInterval: REALTIME_INTERVAL,
    staleTime: 0,
  });
}

export function useVehiclesByRoute(routeId: string) {
  return useQuery({
    queryKey: ["vehicles", routeId],
    queryFn: () => fetchVehiclesByRoute(routeId),
    refetchInterval: REALTIME_INTERVAL,
    staleTime: 0,
    enabled: Boolean(routeId),
  });
}

export function useRoutes() {
  return useQuery({
    queryKey: ["routes"],
    queryFn: fetchRoutes,
    staleTime: Infinity, // static data
  });
}

export function useHistorical(params: HistoricalParams) {
  return useQuery({
    queryKey: ["historical", params],
    queryFn: () => fetchHistorical(params),
    enabled: Object.keys(params).length > 0,
  });
}

export function useOnTime(range: DashboardRange, scope?: RouteScope) {
  return useQuery({
    queryKey: ["ontime", range, scope],
    queryFn: () => fetchOnTime(range, scope),
    // Multi-day on-time stats barely move minute to minute.
    refetchInterval: 300_000,
    staleTime: 300_000,
  });
}

export function useFrequency(scope?: RouteScope) {
  return useQuery({
    queryKey: ["frequency", scope],
    queryFn: () => fetchFrequency(scope),
    refetchInterval: 30_000,
    staleTime: 30_000,
  });
}

export function useAlerts() {
  return useQuery({
    queryKey: ["alerts"],
    queryFn: fetchAlerts,
    refetchInterval: 30_000,
    staleTime: 30_000,
  });
}

export function useOverview(range: DashboardRange, scope?: RouteScope) {
  return useQuery({
    queryKey: ["overview", range, scope],
    queryFn: () => fetchOverview(range, scope),
    refetchInterval: ANALYTICS_INTERVAL,
    staleTime: ANALYTICS_INTERVAL,
  });
}

export function useOnTimeTrend(range: DashboardRange, scope?: RouteScope, granularity: "hour" | "day" = "day") {
  return useQuery({
    queryKey: ["ontimeTrend", range, scope, granularity],
    queryFn: () => fetchOnTimeTrend(range, scope, granularity),
    refetchInterval: ANALYTICS_INTERVAL,
    staleTime: ANALYTICS_INTERVAL,
  });
}

export function useHeatmap(range: DashboardRange, scope?: RouteScope) {
  return useQuery({
    queryKey: ["heatmap", range, scope],
    queryFn: () => fetchHeatmap(range, scope),
    refetchInterval: ANALYTICS_INTERVAL,
    staleTime: ANALYTICS_INTERVAL,
  });
}

export function useDistribution(range: DashboardRange, scope?: RouteScope) {
  return useQuery({
    queryKey: ["distribution", range, scope],
    queryFn: () => fetchDistribution(range, scope),
    refetchInterval: ANALYTICS_INTERVAL,
    staleTime: ANALYTICS_INTERVAL,
  });
}

export function useWorstStops(range: DashboardRange, scope?: RouteScope, limit = 15) {
  return useQuery({
    queryKey: ["worstStops", range, scope, limit],
    queryFn: () => fetchWorstStops(range, scope, limit),
    refetchInterval: ANALYTICS_INTERVAL,
    staleTime: ANALYTICS_INTERVAL,
  });
}

export function useServiceDelivery(range: DashboardRange, scope?: RouteScope) {
  return useQuery({
    queryKey: ["serviceDelivery", range, scope],
    queryFn: () => fetchServiceDelivery(range, scope),
    refetchInterval: ANALYTICS_INTERVAL,
    staleTime: ANALYTICS_INTERVAL,
  });
}

export function useScheduleFrequency(routeId?: string) {
  return useQuery({
    queryKey: ["scheduleFrequency", routeId],
    queryFn: () => fetchScheduleFrequency(routeId),
    staleTime: Infinity, // derived from static GTFS
  });
}

export function useOccupancy(range: DashboardRange, routeId?: string, direction?: number) {
  return useQuery({
    queryKey: ["occupancy", range, routeId, direction],
    queryFn: () => fetchOccupancy(range, routeId, direction),
    refetchInterval: ANALYTICS_INTERVAL,
    staleTime: ANALYTICS_INTERVAL,
  });
}

export function useRidership(routeId?: string, months = 24) {
  return useQuery({
    queryKey: ["ridership", routeId, months],
    queryFn: () => fetchRidership(routeId, months),
    staleTime: ANALYTICS_INTERVAL,
  });
}

export function useActiveVehicles(params: ActiveVehiclesParams) {
  return useQuery({
    queryKey: ["activeVehicles", params],
    queryFn: () => fetchActiveVehicles(params),
    enabled: Boolean(params.start || params.end),
    // Hold the last result while a new page or filter set is in flight. The
    // response carries the filter menu's own option counts, so dropping to
    // undefined would empty the menu underneath whoever is using it.
    placeholderData: (previous) => previous,
  });
}

export function useVehicleTrip(
  vehicleLabel: string,
  params: VehicleTripParams,
  options: { live?: boolean } = {},
) {
  return useQuery({
    queryKey: ["vehicleTrip", vehicleLabel, params],
    queryFn: () => fetchVehicleTrip(vehicleLabel, params),
    enabled: Boolean(vehicleLabel),
    // While the trip is still in progress, keep polling so new stops/positions
    // stream in as they're geofenced — see isTripInProgress in lib/utils.
    refetchInterval: options.live ? REALTIME_INTERVAL : false,
  });
}

/**
 * Denver home games for the map's status carousel.
 *
 * The poll interval follows how fast the answer can change, which on most days
 * is "not at all" — Denver has no home game, the endpoint returns an empty list,
 * and two minutes is plenty. A simulation is the opposite case: its clock runs
 * at up to 600×, so the payload has to be re-read every few seconds or the
 * countdown drifts away from the phase the server thinks it's in.
 *
 * `throwOnError` is deliberately left off and errors are swallowed by callers:
 * if this endpoint is unreachable the map simply shows no game slides.
 */
export function useGames() {
  return useQuery({
    queryKey: ["games"],
    queryFn: fetchGames,
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!data) return 120_000;
      if (data.simulated) return 5_000;
      return data.games.some((g) => g.state === "in") ? 30_000 : 120_000;
    },
    staleTime: 0,
    retry: 1,
  });
}

/** The tracked clubs — only the simulator page needs these. */
export function useSportsTeams() {
  return useQuery({
    queryKey: ["sportsTeams"],
    queryFn: fetchSportsTeams,
    staleTime: Infinity,
  });
}

export function useRailShapes() {
  return useQuery({
    queryKey: ["railShapes"],
    queryFn: fetchRailShapes,
    staleTime: Infinity, // static GTFS data never changes at runtime
  });
}

export function useRouteShape(routeId?: string) {
  return useQuery({
    queryKey: ["routeShape", routeId],
    queryFn: () => fetchRouteShape(routeId!),
    staleTime: Infinity,
    enabled: Boolean(routeId),
  });
}

export function useRouteStops(routeId?: string) {
  return useQuery({
    queryKey: ["routeStops", routeId],
    queryFn: () => fetchRouteStops(routeId!),
    staleTime: Infinity,
    enabled: Boolean(routeId),
  });
}

export function useStopsSearch(query: string) {
  return useQuery({
    queryKey: ["stopsSearch", query],
    queryFn: () => fetchStopsSearch(query),
    staleTime: 60_000,
    enabled: query.trim().length >= 2,
  });
}

export function useStopInfo(stopId?: string) {
  return useQuery({
    queryKey: ["stopInfo", stopId],
    queryFn: () => fetchStopInfo(stopId!),
    staleTime: Infinity,
    enabled: Boolean(stopId),
  });
}
