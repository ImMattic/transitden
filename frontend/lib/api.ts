import type {
  ActiveVehiclesResponse,
  AlertsResponse,
  DistributionResponse,
  FrequencyResponse,
  HeatmapResponse,
  HistoricalResponse,
  LimitsResponse,
  OccupancyResponse,
  OnTimeResponse,
  OverviewResponse,
  RidershipResponse,
  RouteShape,
  RouteStopsResponse,
  RailShapesResponse,
  RealtimeResponse,
  RoutesResponse,
  ScheduleFrequencyResponse,
  ServiceDeliveryResponse,
  StopInfo,
  StopsSearchResponse,
  TrendResponse,
  VehicleTripResponse,
  WorstStopsResponse,
  GamesResponse,
  SportsTeamsResponse,
  SimSessionResponse,
  SimStartRequest,
  SimStatusResponse,
} from "./types";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiError(res.status, `API ${res.status}: ${text}`);
  }
  return res.json() as Promise<T>;
}

// ── Meta ───────────────────────────────────────────────────────────────────

export function fetchLimits(): Promise<LimitsResponse> {
  return apiFetch("/api/v1/meta/limits");
}

// ── Realtime ───────────────────────────────────────────────────────────────

export function fetchVehicles(): Promise<RealtimeResponse> {
  return apiFetch("/api/v1/realtime/vehicles");
}

export function fetchVehiclesByRoute(routeId: string): Promise<RealtimeResponse> {
  return apiFetch(`/api/v1/realtime/vehicles/${encodeURIComponent(routeId)}`);
}

// ── Routes ─────────────────────────────────────────────────────────────────

export function fetchRoutes(): Promise<RoutesResponse> {
  return apiFetch("/api/v1/routes");
}

export function fetchRailShapes(): Promise<RailShapesResponse> {
  return apiFetch("/api/v1/routes/shapes");
}

export function fetchRouteShape(routeId: string): Promise<RouteShape> {
  return apiFetch(`/api/v1/routes/shape/${encodeURIComponent(routeId)}`);
}

export function fetchRouteStops(routeId: string): Promise<RouteStopsResponse> {
  return apiFetch(`/api/v1/routes/stops/${encodeURIComponent(routeId)}`);
}

// ── Stops ──────────────────────────────────────────────────────────────────

export function fetchStopsSearch(q: string): Promise<StopsSearchResponse> {
  return apiFetch(`/api/v1/stops/search?${new URLSearchParams({ q })}`);
}

export function fetchStopInfo(stopId: string): Promise<StopInfo> {
  return apiFetch(`/api/v1/stops/${encodeURIComponent(stopId)}`);
}

// ── Historical ────────────────────────────────────────────────────────────

export interface HistoricalParams {
  route_id?: string;
  start?: string;
  end?: string;
  limit?: number;
  page?: number;
}

export function fetchHistorical(params: HistoricalParams = {}): Promise<HistoricalResponse> {
  const qs = new URLSearchParams();
  if (params.route_id) qs.set("route_id", params.route_id);
  if (params.start) qs.set("start", params.start);
  if (params.end) qs.set("end", params.end);
  if (params.limit) qs.set("limit", String(params.limit));
  if (params.page) qs.set("page", String(params.page));
  const query = qs.toString() ? `?${qs}` : "";
  return apiFetch(`/api/v1/historical/vehicles${query}`);
}

// ── Stats ──────────────────────────────────────────────────────────────────

/** Route restriction shared by the dashboard analytics endpoints. Empty / all
 *  fields absent means "the whole system". Modes are resolved to route_ids by
 *  the API. */
export interface RouteScope {
  routeIds?: string[];
  modes?: string[];
}

function scopeParams(scope?: RouteScope): Record<string, string | undefined> {
  return {
    route_ids: scope?.routeIds?.length ? scope.routeIds.join(",") : undefined,
    modes: scope?.modes?.length ? scope.modes.join(",") : undefined,
  };
}

/** An absolute calendar-day window (`"YYYY-MM-DD"`, date-only) for the Dashboard's
 *  analytics endpoints — see lib/dashboardDateRange.ts for how one is built. */
export interface DashboardRange {
  start: string;
  end: string;
}

function rangeParams(range: DashboardRange): Record<string, string> {
  return { start: range.start, end: range.end };
}

export function fetchOnTime(range: DashboardRange, scope?: RouteScope): Promise<OnTimeResponse> {
  return apiFetch(withParams("/api/v1/stats/ontime", { ...rangeParams(range), ...scopeParams(scope) }));
}

export function fetchFrequency(scope?: RouteScope): Promise<FrequencyResponse> {
  return apiFetch(withParams("/api/v1/stats/frequency", { ...scopeParams(scope) }));
}

export function fetchAlerts(): Promise<AlertsResponse> {
  return apiFetch("/api/v1/stats/alerts");
}

// ── Analytics ────────────────────────────────────────────────────────────────

function withParams(base: string, params: Record<string, string | number | undefined>): string {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== "") qs.set(k, String(v));
  }
  const query = qs.toString();
  return query ? `${base}?${query}` : base;
}

export function fetchOverview(range: DashboardRange, scope?: RouteScope): Promise<OverviewResponse> {
  return apiFetch(withParams("/api/v1/stats/overview", { ...rangeParams(range), ...scopeParams(scope) }));
}

export function fetchOnTimeTrend(
  range: DashboardRange,
  scope?: RouteScope,
  granularity: "hour" | "day" = "day",
): Promise<TrendResponse> {
  return apiFetch(
    withParams("/api/v1/stats/ontime/trend", { ...rangeParams(range), granularity, ...scopeParams(scope) }),
  );
}

export function fetchHeatmap(range: DashboardRange, scope?: RouteScope): Promise<HeatmapResponse> {
  return apiFetch(withParams("/api/v1/stats/ontime/heatmap", { ...rangeParams(range), ...scopeParams(scope) }));
}

export function fetchDistribution(range: DashboardRange, scope?: RouteScope): Promise<DistributionResponse> {
  return apiFetch(withParams("/api/v1/stats/delay/distribution", { ...rangeParams(range), ...scopeParams(scope) }));
}

export function fetchWorstStops(range: DashboardRange, scope?: RouteScope, limit = 15): Promise<WorstStopsResponse> {
  return apiFetch(withParams("/api/v1/stats/stops/worst", { ...rangeParams(range), limit, ...scopeParams(scope) }));
}

export function fetchServiceDelivery(range: DashboardRange, scope?: RouteScope): Promise<ServiceDeliveryResponse> {
  return apiFetch(withParams("/api/v1/stats/service-delivery", { ...rangeParams(range), ...scopeParams(scope) }));
}

export function fetchScheduleFrequency(routeId?: string): Promise<ScheduleFrequencyResponse> {
  return apiFetch(withParams("/api/v1/stats/frequency/schedule", { route_id: routeId }));
}

export function fetchOccupancy(range: DashboardRange, routeId?: string, direction?: number): Promise<OccupancyResponse> {
  return apiFetch(withParams("/api/v1/stats/occupancy", { ...rangeParams(range), route_id: routeId, direction }));
}

export function fetchRidership(routeId?: string, months = 24): Promise<RidershipResponse> {
  return apiFetch(withParams("/api/v1/stats/ridership", { route_id: routeId, months }));
}

// ── Vehicle drill-down ────────────────────────────────────────────────────────

export interface ActiveVehiclesParams {
  start?: string;
  end?: string;
  /** Comma-separated; each of these ORs within itself and ANDs with the rest. */
  route_ids?: string;
  modes?: string;
  vehicle_labels?: string;
  status?: string;
  occupancy?: string;
  min_duration_minutes?: number;
  max_duration_minutes?: number;
  min_avg_delay_seconds?: number;
  max_avg_delay_seconds?: number;
  min_on_time_pct?: number;
  max_on_time_pct?: number;
  strict?: boolean;
  limit?: number;
  offset?: number;
}

export function fetchActiveVehicles(params: ActiveVehiclesParams = {}): Promise<ActiveVehiclesResponse> {
  return apiFetch(withParams("/api/v1/vehicles/active", {
    start: params.start,
    end: params.end,
    route_ids: params.route_ids,
    modes: params.modes,
    vehicle_labels: params.vehicle_labels,
    status: params.status,
    occupancy: params.occupancy,
    min_duration_minutes: params.min_duration_minutes,
    max_duration_minutes: params.max_duration_minutes,
    min_avg_delay_seconds: params.min_avg_delay_seconds,
    max_avg_delay_seconds: params.max_avg_delay_seconds,
    min_on_time_pct: params.min_on_time_pct,
    max_on_time_pct: params.max_on_time_pct,
    strict: params.strict ? "true" : undefined,
    limit: params.limit,
    offset: params.offset,
  }));
}

export interface VehicleTripParams {
  trip_id?: string;
  start?: string;
  end?: string;
}

export function fetchVehicleTrip(vehicleLabel: string, params: VehicleTripParams = {}): Promise<VehicleTripResponse> {
  return apiFetch(withParams(`/api/v1/vehicles/${encodeURIComponent(vehicleLabel)}/trip`, { trip_id: params.trip_id, start: params.start, end: params.end }));
}

// ── Denver home games ──────────────────────────────────────────────────────

export function fetchGames(): Promise<GamesResponse> {
  return apiFetch("/api/v1/sports/games");
}

export function fetchSportsTeams(): Promise<SportsTeamsResponse> {
  return apiFetch("/api/v1/sports/teams");
}

// ── Game simulator ─────────────────────────────────────────────────────────
// Every call carries the session token in a header rather than a cookie: the
// simulator is a single unlisted page, and a header can't be sent by a
// cross-site form the way an ambient cookie can.

function simHeaders(token: string): HeadersInit {
  return { "Content-Type": "application/json", "X-Sim-Token": token };
}

export function createSimSession(password: string): Promise<SimSessionResponse> {
  return apiFetch("/api/v1/sports/sim/session", {
    method: "POST",
    body: JSON.stringify({ password }),
  });
}

export function fetchSimStatus(token: string): Promise<SimStatusResponse> {
  return apiFetch("/api/v1/sports/sim", { headers: simHeaders(token) });
}

export function startSim(token: string, body: SimStartRequest): Promise<SimStatusResponse> {
  return apiFetch("/api/v1/sports/sim", {
    method: "POST",
    headers: simHeaders(token),
    body: JSON.stringify(body),
  });
}

export function stopSim(token: string): Promise<SimStatusResponse> {
  return apiFetch("/api/v1/sports/sim", {
    method: "DELETE",
    headers: simHeaders(token),
  });
}

// ── Export ─────────────────────────────────────────────────────────────────

export function exportUrl(params: {
  format: "csv" | "json";
  route_id?: string;
  start?: string;
  end?: string;
  limit?: number;
}): string {
  const qs = new URLSearchParams({ format: params.format });
  if (params.route_id) qs.set("route_id", params.route_id);
  if (params.start) qs.set("start", params.start);
  if (params.end) qs.set("end", params.end);
  if (params.limit) qs.set("limit", String(params.limit));
  return `${BASE}/api/v1/export/vehicles?${qs}`;
}
