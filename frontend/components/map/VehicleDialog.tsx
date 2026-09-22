import { cn, formatDelay, formatTime, formatDateTime, routeColor, headwayColor } from "@/lib/utils";
import { useTheme } from "@/lib/useTheme";
import type { VehiclePosition } from "@/lib/types";

interface Props {
  vehicle: VehiclePosition;
  onClose: () => void;
}

const STATUS_LABELS: Record<number, string> = {
  0: "Arriving at",
  1: "Stopped at",
  2: "In transit to",
};

export default function VehicleDialog({ vehicle: v, onClose }: Props) {
  const { resolvedTheme } = useTheme();
  const delay = v.delay_seconds ?? 0;
  const isLate = delay > 300;
  const isEarly = delay < -300;
  const delayText = (isLate || isEarly) ? formatDelay(v.delay_seconds) : "";

  return (
    <div className="animate-dialog-in absolute bottom-6 left-1/2 z-[9999] w-80 -translate-x-1/2 rounded-xl bg-overlay/95 shadow-2xl ring-1 ring-line-strong backdrop-blur-sm">
      {/* Header */}
      <div
        className="flex items-center justify-between rounded-t-xl px-4 py-3 text-white"
        style={{ backgroundColor: `#${v.route_color || "003DA5"}` }}
      >
        <div>
          <span className="text-lg font-bold">{v.route_short_name}</span>
          {v.vehicle_label && (
            <span className="ml-2 text-sm opacity-80">#{v.vehicle_label}</span>
          )}
          <p className="text-xs opacity-70 mt-0.5">{v.route_long_name}</p>
        </div>
        <button
          onClick={onClose}
          aria-label="Close"
          className="rounded-full p-1 hover:bg-white/20 transition-colors"
        >
          <svg className="h-5 w-5" viewBox="0 0 20 20" fill="currentColor">
            <path d="M6.28 5.22a.75.75 0 0 0-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 1 0 1.06 1.06L10 11.06l3.72 3.72a.75.75 0 1 0 1.06-1.06L11.06 10l3.72-3.72a.75.75 0 0 0-1.06-1.06L10 8.94 6.28 5.22Z" />
          </svg>
        </button>
      </div>

      {/* Body */}
      <div className="px-4 py-3 space-y-2 text-sm text-fg">
        {/* Current stop */}
        <div>
          <p className="text-fg-subtle text-xs">
            {STATUS_LABELS[v.current_status ?? -1] ?? "At"}
          </p>
          <p className="font-medium text-fg">{v.stop_name ?? v.stop_id ?? "Unknown stop"}</p>
        </div>

        {/* On-time status, with the trip-page link sitting inline alongside it */}
        <div className="flex flex-wrap items-center gap-2">
          <span
            className={cn(
              "rounded-full px-2 py-0.5 text-xs font-semibold",
              isLate ? "status-danger" : isEarly ? "status-warn" : "status-ok",
            )}
          >
            {isLate ? "Late" : isEarly ? "Early" : "On time"}
          </span>
          {delayText && <span className="font-mono text-fg-muted">{delayText}</span>}

          {/* View trip — same translucent pill treatment as the status tag, but
              in a neutral theme wash so it doesn't read as a warning. Jumps to
              this vehicle's trip page. */}
          {v.vehicle_label && (
            <a
              href={`/trips/trip/${encodeURIComponent(v.vehicle_label)}${
                v.trip_id ? `?trip_id=${encodeURIComponent(v.trip_id)}` : ""
              }`}
              className="inline-flex items-center gap-1 rounded-full border border-fg/20 bg-fg/[0.08] px-2 py-0.5 text-xs font-semibold text-fg transition-opacity hover:opacity-80"
            >
              <span className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-current" />
              View Trip
              <svg className="h-3 w-3 shrink-0" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                <path
                  fillRule="evenodd"
                  d="M7.21 14.77a.75.75 0 0 1 .02-1.06L10.94 10 7.23 6.29a.75.75 0 0 1 1.06-1.06l4.24 4.24a.75.75 0 0 1 0 1.06l-4.24 4.24a.75.75 0 0 1-1.08 0Z"
                  clipRule="evenodd"
                />
              </svg>
            </a>
          )}
        </div>

        {/* Headway — the value carries the same translucent tag treatment as the
            on-time pill, tinted to whichever frequency bucket it lands in (the
            colours the legend and vehicle outlines use). */}
        {v.headway_minutes !== null && (
          <p className="text-fg-subtle text-xs">
            Real-time headway:{" "}
            {(() => {
              const c = headwayColor(v.headway_minutes, resolvedTheme);
              return (
                <span
                  className="rounded-full border px-2 py-0.5 text-xs font-semibold"
                  style={{ color: c, backgroundColor: `${c}29`, borderColor: `${c}4D` }}
                >
                  {v.headway_minutes} min
                </span>
              );
            })()}
          </p>
        )}

        {/* Occupancy */}
        {v.occupancy_status && v.occupancy_status !== "UNKNOWN" && (
          <p className="text-fg-subtle text-xs">
            Occupancy:{" "}
            <span className="font-medium text-fg-muted">
              {v.occupancy_status.replace(/_/g, " ")}
            </span>
          </p>
        )}

        {/* Last updated */}
        <p className="text-fg-subtle text-xs">
          Updated {formatDateTime(v.timestamp)}
        </p>

        {/* Links */}
        <div className="pt-1.5 border-t border-line flex flex-wrap gap-x-3 gap-y-1">
          <a
            href={`https://app.rtd-denver.com/route/${v.route_short_name}/schedule`}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-xs text-accent hover:underline"
          >
            RTD schedule
            <svg className="h-3 w-3 opacity-70" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path fillRule="evenodd" d="M4.25 5.5a.75.75 0 00-.75.75v8.5c0 .414.336.75.75.75h8.5a.75.75 0 00.75-.75v-4a.75.75 0 011.5 0v4A2.25 2.25 0 0112.75 17h-8.5A2.25 2.25 0 012 14.75v-8.5A2.25 2.25 0 014.25 4h5a.75.75 0 010 1.5h-5z" clipRule="evenodd"/>
              <path fillRule="evenodd" d="M6.194 12.753a.75.75 0 001.06.053L16.5 4.44v2.81a.75.75 0 001.5 0v-4.5a.75.75 0 00-.75-.75h-4.5a.75.75 0 000 1.5h2.553l-9.056 8.194a.75.75 0 00-.053 1.06z" clipRule="evenodd"/>
            </svg>
          </a>
          <a
            href={
              v.route_type !== "3"
                ? `https://www.greaterdenvertransit.com/rtd-${v.route_short_name.toLowerCase()}line/`
                : `https://www.greaterdenvertransit.com/rtd-route${v.route_short_name}/`
            }
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-xs text-accent hover:underline"
          >
            GDT overview
            <svg className="h-3 w-3 opacity-70" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path fillRule="evenodd" d="M4.25 5.5a.75.75 0 00-.75.75v8.5c0 .414.336.75.75.75h8.5a.75.75 0 00.75-.75v-4a.75.75 0 011.5 0v4A2.25 2.25 0 0112.75 17h-8.5A2.25 2.25 0 012 14.75v-8.5A2.25 2.25 0 014.25 4h5a.75.75 0 010 1.5h-5z" clipRule="evenodd"/>
              <path fillRule="evenodd" d="M6.194 12.753a.75.75 0 001.06.053L16.5 4.44v2.81a.75.75 0 001.5 0v-4.5a.75.75 0 00-.75-.75h-4.5a.75.75 0 000 1.5h2.553l-9.056 8.194a.75.75 0 00-.053 1.06z" clipRule="evenodd"/>
            </svg>
          </a>
        </div>
      </div>
    </div>
  );
}
