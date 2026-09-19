// ── Shared domain types mirroring the backend Pydantic schemas ──────────────

/**
 * The server's own request caps, from /api/v1/meta/limits. Date pickers read
 * these so they can disable out-of-range days instead of letting the API reject
 * the window afterwards.
 */
export interface LimitsResponse {
  vehicles_max_span_hours: number;
  historical_max_span_days: number;
  export_max_span_days: number;
  dashboard_max_span_days: number;
  data_retention_days: number;
}

export interface RailShape {
  route_id: string;
  short_name: string;
  color: string;         // hex string e.g. "#008348"
  shapes: [number, number][][];  // array of polylines, each is [[lat,lon],...]
}

export interface RailShapesResponse {
  shapes: RailShape[];
}

export interface RouteShape {
  route_id: string;
  short_name: string;
  route_type: string;
  color: string;
  shapes: [number, number][][];
}

export interface RouteInfo {
  route_id: string;
  short_name: string;
  long_name: string;
  route_type: string;
  type_name: "light_rail" | "heavy_rail" | "commuter_rail" | "bus" | "other";
  color: string;
  agency_id: string;
}

export interface VehiclePosition {
  vehicle_id: string | null;
  vehicle_label: string | null;
  trip_id: string | null;
  route_id: string;
  route_short_name: string;
  route_long_name: string;
  route_color: string;
  route_type: string;
  latitude: number | null;
  longitude: number | null;
  bearing: number | null;
  current_stop_sequence: number | null;
  current_status: number | null;
  current_status_label: string | null;
  stop_id: string | null;
  stop_name: string | null;
  occupancy_status: string | null;
  timestamp: string;
  delay_seconds: number | null;
  is_late: boolean | null;
  headway_minutes: number | null;
}

export interface RealtimeResponse {
  updated_at: string;
  vehicles: VehiclePosition[];
  route_headways: Record<string, number>;
  total_vehicles?: number | null;
  vehicles_with_location?: number | null;
  unique_vehicle_keys?: number | null;
}

export interface VehicleHistoryPoint {
  vehicle_id: string | null;
  vehicle_label: string | null;
  trip_id: string | null;
  route_id: string;
  route_short_name: string | null;
  latitude: number | null;
  longitude: number | null;
  bearing: number | null;
  current_status: number | null;
  stop_id: string | null;
  occupancy_status: string | null;
  timestamp: string;
  delay_seconds: number | null;
}

export interface HistoricalResponse {
  start: string;
  end: string;
  page?: number;
  limit?: number;
  returned: number;
  total?: number;
  total_pages?: number;
  vehicles: VehicleHistoryPoint[];
}

export interface OnTimeRouteStats {
  route_id: string;
  route_short_name: string;
  total_observations: number;
  on_time: number;
  late: number;
  early: number;
  on_time_pct: number;
  avg_delay_seconds: number;
}

export interface OnTimeResponse {
  period_days: number;
  range_start?: string;
  range_end?: string;
  routes: OnTimeRouteStats[];
  overall: { on_time_pct: number; avg_delay_seconds: number };
}

export interface FrequencyRouteStats {
  route_id: string;
  route_short_name: string;
  avg_headway_minutes: number;
  min_headway_minutes: number;
  max_headway_minutes: number;
  vehicle_count: number;
}

export interface FrequencyResponse {
  computed_at: string;
  routes: FrequencyRouteStats[];
}

export interface StuckAlert {
  vehicle_id: string | null;
  vehicle_label: string | null;
  route_id: string;
  route_short_name: string;
  latitude: number | null;
  longitude: number | null;
  stop_id: string | null;
  stop_name: string | null;
  stuck_since: string;
  minutes_stuck: number;
}

export interface AlertsResponse {
  computed_at: string;
  alerts: StuckAlert[];
}

export interface RoutesResponse {
  routes: RouteInfo[];
}

export interface RouteStop {
  stop_id: string;
  stop_name: string;
  stop_lat: number;
  stop_lon: number;
}

export interface RouteStopsResponse {
  route_id: string;
  stops: RouteStop[];
}

export interface StopRoute {
  route_id: string;
  short_name: string;
  long_name: string;
  color: string;
  route_type: string;
}

export interface StopInfo {
  stop_id: string;
  stop_name: string;
  stop_desc: string;
  stop_lat: number;
  stop_lon: number;
  is_rail: boolean;
  routes: StopRoute[];
}

export interface StopsSearchResponse {
  query: string;
  stops: StopInfo[];
}

// ── Deep analytics (api/v1/stats/* analytics endpoints) ─────────────────────

export interface MetricWithDelta {
  value: number;
  previous: number | null;
}

export interface OverviewResponse {
  period_days: number;
  range_start?: string;
  range_end?: string;
  on_time_pct: MetricWithDelta;
  avg_delay_seconds: MetricWithDelta;
  delay_stddev_seconds: number;
  service_delivered_pct: MetricWithDelta;
  observed_trips: number;
  scheduled_trips: number;
  routes_tracked: number;
  total_observations: number;
  latest_ridership_month: string | null;
  latest_ridership_total: number | null;
  prev_ridership_total: number | null;
}

export interface TrendPoint {
  t: string;
  on_time_pct: number;
  avg_delay_seconds: number;
  observations: number;
}

export interface TrendResponse {
  period_days: number;
  range_start?: string;
  range_end?: string;
  granularity: string;
  route_id: string | null;
  points: TrendPoint[];
}

export interface HeatmapCell {
  dow: number; // 0=Sun … 6=Sat (local)
  hour: number; // 0–23 (local)
  on_time_pct: number;
  avg_delay_seconds: number;
  observations: number;
}

export interface HeatmapResponse {
  period_days: number;
  range_start?: string;
  range_end?: string;
  route_id: string | null;
  cells: HeatmapCell[];
}

export interface DistributionBin {
  key: string;
  label: string;
  count: number;
  pct: number;
}

export interface DistributionResponse {
  period_days: number;
  range_start?: string;
  range_end?: string;
  route_id: string | null;
  total: number;
  avg_delay_seconds: number;
  stddev_seconds: number;
  bins: DistributionBin[];
}

export interface WorstStop {
  stop_id: string;
  stop_name: string | null;
  route_id: string | null;
  observations: number;
  on_time_pct: number;
  avg_delay_seconds: number;
}

export interface WorstStopsResponse {
  period_days: number;
  range_start?: string;
  range_end?: string;
  route_id: string | null;
  stops: WorstStop[];
}

export interface ServiceDeliveryRoute {
  route_id: string;
  route_short_name: string;
  observed_trips: number;
  scheduled_trips: number;
  delivered_pct: number;
}

export interface ServiceDeliveryResponse {
  period_days: number;
  range_start?: string;
  range_end?: string;
  observed_trips: number;
  scheduled_trips: number;
  delivered_pct: number;
  routes: ServiceDeliveryRoute[];
}

export interface HourHeadway {
  hour: number;
  headway_minutes: number | null;
}

export interface ScheduleFrequencyRoute {
  route_id: string;
  route_short_name: string;
  weekday_trips: number;
  saturday_trips: number;
  sunday_trips: number;
  span_start: string | null;
  span_end: string | null;
  headways_by_hour: HourHeadway[];
}

export interface ScheduleFrequencyResponse {
  route_id: string | null;
  routes: ScheduleFrequencyRoute[];
}

export interface OccupancyHourPoint {
  hour: number;
  empty: number;
  many_seats: number;
  few_seats: number;
  standing: number;
  crushed: number;
  full: number;
  not_accepting: number;
  unknown: number;
  total: number;
}

export interface DirectionInfo {
  direction_id: number;
  headsign: string;
}

export interface OccupancyResponse {
  period_days: number;
  range_start?: string;
  range_end?: string;
  route_id: string | null;
  direction: number | null;
  reported: boolean;
  empty: number;
  many_seats: number;
  few_seats: number;
  standing: number;
  crushed: number;
  full: number;
  not_accepting: number;
  low: number;
  medium: number;
  high: number;
  unknown: number;
  samples: number;
  standing_pct: number | null;
  by_hour: OccupancyHourPoint[];
  directions: DirectionInfo[];
}

// ── Vehicle drill-down ───────────────────────────────────────────────────────

export interface ActiveVehicle {
  vehicle_label: string | null;
  vehicle_id: string | null;
  trip_id: string | null;
  route_id: string;
  route_short_name: string | null;
  route_color: string | null;
  route_type: string | null;
  /** rail / bus / other, from the route's GTFS route_type. */
  mode: string;
  start_time: string;
  end_time: string;
  duration_minutes: number;
  start_stop_name: string | null;
  end_stop_name: string | null;
  /** False when the trip ended without a geofenced arrival at its terminus stop_sequence. */
  reached_terminus: boolean;
  /** Still reporting positions as of the request — the server's read of "live". */
  in_progress: boolean;
  trip_status: "in_progress" | "complete" | "incomplete";
  last_latitude: number | null;
  last_longitude: number | null;
  last_occupancy_status: string | null;
  last_delay_seconds: number | null;
  /** Mean of every geofenced stop_arrival_events delay on this trip; null with no observed arrivals. */
  avg_delay_seconds: number | null;
  /** Share of those arrivals within ±ontime_threshold_seconds of schedule. */
  on_time_pct: number | null;
  observation_count: number;
  stop_arrival_count: number;
}

export interface TripRouteFacet {
  route_id: string;
  route_short_name: string | null;
  route_color: string | null;
  mode: string;
  trip_count: number;
}

export interface TripVehicleFacet {
  vehicle_label: string;
  /** Every route this fleet number served in the window, in the order first seen. */
  route_short_names: string[];
  route_color: string | null;
  mode: string;
  trip_count: number;
}

/** Per-option counts for the Trip Explorer's filter menu, taken before filters. */
export interface TripFacets {
  trip_count: number;
  /**
   * True when a route filter was in force, which the API applies in SQL — these
   * counts then describe the chosen routes rather than the whole window.
   */
  route_scoped: boolean;
  routes: TripRouteFacet[];
  vehicles: TripVehicleFacet[];
  statuses: Record<string, number>;
  modes: Record<string, number>;
  occupancy: Record<string, number>;
  max_duration_minutes: number;
}

export interface ActiveVehiclesResponse {
  start: string;
  end: string;
  /** Trips matching every filter — what pagination counts against. */
  vehicle_count: number;
  /** Trips in the window before any filter, for "N of M" copy. */
  window_count: number;
  vehicles: ActiveVehicle[];
  facets: TripFacets;
}

export interface VehicleStopEvent {
  stop_id: string;
  stop_name: string | null;
  stop_lat: number | null;
  stop_lon: number | null;
  stop_sequence: number;
  stop_headsign?: string | null;
  /** RTD schedule-adherence timepoint (vs. an intermediate stop). */
  is_timepoint?: boolean;
  /** GTFS pickup_type / drop_off_type — "1" = not available (terminus). */
  pickup_type?: string | null;
  drop_off_type?: string | null;
  /** null when the stop has no schedule time and no observed arrival. */
  scheduled_time: string | null;
  /** false when this scheduled stop was never geofenced on this run. */
  observed?: boolean;
  /**
   * What `actual_time` measures. The origin is timed by "departure" — when the
   * vehicle pulled away from the stop — since it sits there on layover long
   * before the trip starts. Every other stop is an "arrival". null when unobserved.
   */
  event_type?: "arrival" | "departure" | null;
  actual_time: string | null;
  delay_seconds: number | null;
  occupancy_status: string | null;
  actual_lat: number | null;
  actual_lon: number | null;
  actual_bearing: number | null;
  /**
   * Which rule produced `actual_time`. All but "terminus_fallback" are
   * measurements; that one is the vehicle's closest approach to the end of the
   * line after its feed cut out short of the geofence, so it is a lower bound
   * on the real arrival. null for rows written before the column existed.
   */
  detection_method?:
    | "geofence"
    | "segment"
    | "origin_departure"
    | "terminus_fallback"
    | null;
}

export interface VehiclePositionTrack {
  latitude: number;
  longitude: number;
  bearing: number | null;
  timestamp: string;
  current_status: number | null;
  occupancy_status: string | null;
}

export interface VehicleTripResponse {
  vehicle_label: string | null;
  vehicle_id: string | null;
  trip_id: string | null;
  route_id: string | null;
  route_short_name: string | null;
  route_long_name: string | null;
  route_color: string | null;
  route_type: string | null;
  start: string;
  end: string;
  stops: VehicleStopEvent[];
  scheduled_stop_count?: number;
  observed_stop_count?: number;
  positions: VehiclePositionTrack[];
  avg_delay_seconds: number | null;
  on_time_pct: number | null;
  observation_count: number;
}

export interface RidershipPoint {
  month: string;
  boardings: number;
}

export interface RidershipRoute {
  route_id: string;
  route_short_name: string;
  boardings: number;
}

export interface RidershipResponse {
  route_id: string | null;
  available: boolean;
  latest_month: string | null;
  latest_total: number | null;
  prev_total: number | null;
  series: RidershipPoint[];
  by_route_latest: RidershipRoute[];
}

// ── Denver home games (map status carousel) ─────────────────────────────────

export type GameState = "pre" | "in" | "post";
export type GameResult = "win" | "loss" | "draw";

/** One home game, in whatever phase it's currently in. Mirrors the backend
 *  GameSlide — see backend/app/schemas/sports.py. */
export interface GameSlide {
  id: string;
  league: string;
  sport_emoji: string;
  team_key: string;
  team_abbr: string;
  team_name: string;
  opponent_abbr: string;
  opponent_name: string;
  /** Hex without '#'. */
  color: string;
  alt_color: string;
  state: GameState;
  start: string;
  end: string | null;
  detail: string;
  team_score: number | null;
  opponent_score: number | null;
  result: GameResult | null;
  venue: string | null;
  simulated: boolean;
}

export interface GamesResponse {
  games: GameSlide[];
  /** Server time the payload was built — the client anchors its clock here. */
  generated_at: string;
  /** Virtual seconds per real second; >1 only while the simulator runs. */
  clock_rate: number;
  simulated: boolean;
  sim_expires_at: string | null;
}

export interface SportsTeam {
  key: string;
  name: string;
  abbr: string;
  league: string;
  emoji: string;
  color: string;
  alt_color: string;
}

export interface SportsTeamsResponse {
  teams: SportsTeam[];
}

// ── Game simulator (unlisted /sim page) ─────────────────────────────────────

export interface SimSessionResponse {
  token: string;
  expires_at: string;
}

export interface SimGameRequest {
  team_key: string;
  opponent_abbr: string;
  opponent_name: string;
  lead_minutes: number;
  game_minutes: number;
  final_team_score: number;
  final_opponent_score: number;
}

export interface SimStartRequest {
  games: SimGameRequest[];
  speed: number;
  duration_minutes: number;
  hide_real: boolean;
}

export interface SimStatusResponse {
  active: boolean;
  started_at: string | null;
  expires_at: string | null;
  speed: number | null;
  hide_real: boolean | null;
  elapsed_virtual_minutes: number | null;
  games: GameSlide[];
}
