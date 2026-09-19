"use client";
import { memo, useMemo, useState } from "react";
import type { FrequencyRouteStats } from "@/lib/types";
import { bestTextOn, headwayColor } from "@/lib/utils";
import { useTheme } from "@/lib/useTheme";

type SortKey = keyof Pick<
  FrequencyRouteStats,
  "route_short_name" | "vehicle_count" | "avg_headway_minutes" | "min_headway_minutes"
>;
type SortDir = "asc" | "desc";

const PAGE_SIZE = 15;

interface Props {
  routes: FrequencyRouteStats[];
  onRowClick?: (routeId: string) => void;
}

function SortIcon({ active, dir }: { active: boolean; dir: SortDir }) {
  return (
    <span className={`ml-1 inline-block ${active ? "text-fg-muted" : "text-fg-subtle/60"}`}>
      {active && dir === "desc" ? "▼" : "▲"}
    </span>
  );
}

function FrequencyBadge({ minutes, mode }: { minutes: number; mode: "dark" | "light" }) {
  const color = headwayColor(minutes, mode);
  const label =
    minutes <= 0
      ? "—"
      : minutes <= 15
        ? "High"
        : minutes <= 30
          ? "Moderate"
          : "Low";
  return (
    <span
      className="rounded-full px-2 py-0.5 text-xs font-semibold"
      style={{ backgroundColor: color, color: bestTextOn(color) }}
    >
      {label}
    </span>
  );
}

function FrequencyTable({ routes, onRowClick }: Props) {
  const { resolvedTheme } = useTheme();
  const [sortKey, setSortKey] = useState<SortKey>("avg_headway_minutes");
  const [sortDir, setSortDir] = useState<SortDir>("asc");
  const [page, setPage] = useState(0);

  const sorted = useMemo(() => {
    const copy = routes.filter((r) => r.avg_headway_minutes > 0);
    copy.sort((a, b) => {
      const av = a[sortKey];
      const bv = b[sortKey];
      if (typeof av === "string" && typeof bv === "string") {
        return sortDir === "asc" ? av.localeCompare(bv) : bv.localeCompare(av);
      }
      return sortDir === "asc" ? (av as number) - (bv as number) : (bv as number) - (av as number);
    });
    return copy;
  }, [routes, sortKey, sortDir]);

  const totalPages = Math.ceil(sorted.length / PAGE_SIZE);
  const pageRows = sorted.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE);

  function handleSort(key: SortKey) {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("asc");
    }
    setPage(0);
  }

  if (!routes.length) {
    return <p className="text-sm text-fg-subtle py-4">No frequency data yet.</p>;
  }

  const thClass =
    "px-2 py-1.5 cursor-pointer select-none whitespace-nowrap hover:text-fg sm:px-3 sm:py-2";
  const tdClass = "px-2 py-1.5 sm:px-3 sm:py-2";

  return (
    <div className="space-y-2">
      {/* Denser padding/text on mobile — same trade as WorstStopsTable: shrink
          the chrome so more columns fit before handing off to horizontal scroll. */}
      <div className="overflow-x-auto rounded border border-line">
        <table className="min-w-full text-xs text-fg-muted sm:text-sm">
          <thead className="bg-raised text-[10px] uppercase text-fg-subtle sm:text-xs">
            <tr>
              <th
                className={`${thClass} text-left`}
                onClick={() => handleSort("route_short_name")}
              >
                <span className="sm:hidden">Rt.</span>
                <span className="hidden sm:inline">Route</span>
                <SortIcon active={sortKey === "route_short_name"} dir={sortDir} />
              </th>
              <th
                className={`${thClass} text-right`}
                onClick={() => handleSort("vehicle_count")}
              >
                <span className="sm:hidden">Veh.</span>
                <span className="hidden sm:inline">Vehicles</span>
                <SortIcon active={sortKey === "vehicle_count"} dir={sortDir} />
              </th>
              <th
                className={`${thClass} text-right`}
                onClick={() => handleSort("avg_headway_minutes")}
              >
                <span className="sm:hidden">Avg</span>
                <span className="hidden sm:inline">
                  Avg headway
                  <span className="ml-1 font-normal normal-case opacity-50">(est.)</span>
                </span>
                <SortIcon active={sortKey === "avg_headway_minutes"} dir={sortDir} />
              </th>
              <th
                className={`${thClass} text-right`}
                onClick={() => handleSort("min_headway_minutes")}
              >
                <span className="sm:hidden">Range</span>
                <span className="hidden sm:inline">Range (30 min)</span>
                <SortIcon active={sortKey === "min_headway_minutes"} dir={sortDir} />
              </th>
              <th className={`${thClass} text-center`}>
                <span className="sm:hidden">Freq.</span>
                <span className="hidden sm:inline">Frequency</span>
              </th>
              <th className="w-6"></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {pageRows.map((r) => (
              <tr
                key={r.route_id}
                className={`group hover:bg-raised ${onRowClick ? "cursor-pointer" : ""}`}
                onClick={() => onRowClick?.(r.route_id)}
              >
                <td className={`${tdClass} font-bold text-fg`}>{r.route_short_name}</td>
                <td className={`${tdClass} text-right`}>{r.vehicle_count}</td>
                <td className={`${tdClass} text-right`}>
                  {r.avg_headway_minutes > 0 ? `${r.avg_headway_minutes} min` : "—"}
                </td>
                <td className={`${tdClass} text-right text-fg-subtle`}>
                  {r.min_headway_minutes > 0 && r.min_headway_minutes !== r.max_headway_minutes
                    ? `${r.min_headway_minutes}–${r.max_headway_minutes} min`
                    : r.avg_headway_minutes > 0
                      ? `~${r.avg_headway_minutes} min`
                      : "—"}
                </td>
                <td className={`${tdClass} text-center`}>
                  <FrequencyBadge minutes={r.avg_headway_minutes} mode={resolvedTheme} />
                </td>
                <td className="pr-3 text-fg-subtle group-hover:text-fg-muted transition-colors select-none">›</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {totalPages > 1 && (
        <div className="flex items-center justify-end gap-3 text-xs text-fg-subtle">
          <span>
            {page * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE + PAGE_SIZE, sorted.length)} of{" "}
            {sorted.length}
          </span>
          <button
            onClick={() => setPage((p) => p - 1)}
            disabled={page === 0}
            className="px-2 py-0.5 rounded hover:bg-raised disabled:opacity-30 disabled:cursor-not-allowed"
          >
            ‹
          </button>
          <button
            onClick={() => setPage((p) => p + 1)}
            disabled={page >= totalPages - 1}
            className="px-2 py-0.5 rounded hover:bg-raised disabled:opacity-30 disabled:cursor-not-allowed"
          >
            ›
          </button>
        </div>
      )}
    </div>
  );
}

export default memo(FrequencyTable);
