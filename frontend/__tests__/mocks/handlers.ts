import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import type {
  RealtimeResponse,
  OnTimeResponse,
  FrequencyResponse,
  RoutesResponse,
  RailShapesResponse,
  HistoricalResponse,
  LimitsResponse,
  GamesResponse,
} from "@/lib/types";

export const MOCK_VEHICLE = {
  vehicle_id: "V1",
  vehicle_label: "101",
  trip_id: "T1",
  route_id: "R1",
  route_short_name: "15L",
  route_long_name: "East Colfax Local",
  route_color: "003DA5",
  route_type: "3",
  latitude: 39.7392,
  longitude: -104.9903,
  bearing: 90,
  current_stop_sequence: 5,
  current_status: 2,
  current_status_label: "IN_TRANSIT_TO",
  stop_id: "S1",
  stop_name: "Colfax & Broadway",
  occupancy_status: null,
  timestamp: "2026-06-20T12:00:00Z",
  delay_seconds: null,
  is_late: null,
  headway_minutes: 12.0,
};

const MOCK_ONTIME_ROUTE = {
  route_id: "R1",
  route_short_name: "15L",
  total_observations: 100,
  on_time: 85,
  late: 10,
  early: 5,
  on_time_pct: 85.0,
  avg_delay_seconds: 45.0,
};

const MOCK_FREQUENCY_ROUTE = {
  route_id: "R1",
  route_short_name: "15L",
  avg_headway_minutes: 12.0,
  min_headway_minutes: 10.0,
  max_headway_minutes: 15.0,
  vehicle_count: 8,
};

export const handlers = [
  http.get("/api/v1/meta/limits", () =>
    HttpResponse.json<LimitsResponse>({
      vehicles_max_span_hours: 24,
      historical_max_span_days: 7,
      export_max_span_days: 31,
      dashboard_max_span_days: 366,
      data_retention_days: 365,
    })
  ),
  http.get("/api/v1/realtime/vehicles", () =>
    HttpResponse.json<RealtimeResponse>({
      updated_at: "2026-06-20T12:00:00Z",
      vehicles: [MOCK_VEHICLE as any],
      route_headways: { R1: 12.0 },
      total_vehicles: 1,
      vehicles_with_location: 1,
      unique_vehicle_keys: null,
    })
  ),

  http.get("/api/v1/realtime/vehicles/:routeId", () =>
    HttpResponse.json<RealtimeResponse>({
      updated_at: "2026-06-20T12:00:00Z",
      vehicles: [MOCK_VEHICLE as any],
      route_headways: { R1: 12.0 },
      total_vehicles: 1,
      vehicles_with_location: 1,
      unique_vehicle_keys: null,
    })
  ),

  http.get("/api/v1/routes", () =>
    HttpResponse.json<RoutesResponse>({ routes: [] })
  ),

  http.get("/api/v1/routes/shapes", () =>
    HttpResponse.json<RailShapesResponse>({ shapes: [] })
  ),

  http.get("/api/v1/routes/shape/:routeId", () =>
    HttpResponse.json({ route_id: "W", shapes: [], route_type: "0", name: "W Line" })
  ),

  http.get("/api/v1/historical/vehicles", () =>
    HttpResponse.json<HistoricalResponse>({
      start: "2026-06-19T12:00:00Z",
      end: "2026-06-20T12:00:00Z",
      page: 1,
      limit: 200,
      returned: 0,
      total: 0,
      total_pages: 0,
      vehicles: [],
    })
  ),

  http.get("/api/v1/stats/ontime", () =>
    HttpResponse.json<OnTimeResponse>({
      period_days: 7,
      routes: [MOCK_ONTIME_ROUTE],
      overall: { on_time_pct: 85.0, avg_delay_seconds: 45.0 },
    })
  ),

  http.get("/api/v1/stats/frequency", () =>
    HttpResponse.json<FrequencyResponse>({
      computed_at: "2026-06-20T12:00:00Z",
      routes: [MOCK_FREQUENCY_ROUTE],
    })
  ),

  // No home game is the ordinary answer — most days in Denver have none.
  http.get("/api/v1/sports/games", () =>
    HttpResponse.json<GamesResponse>({
      games: [],
      generated_at: "2026-06-20T12:00:00Z",
      clock_rate: 1,
      simulated: false,
      sim_expires_at: null,
    })
  ),
];

export const server = setupServer(...handlers);
