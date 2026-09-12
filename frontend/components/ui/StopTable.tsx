import { formatDelay, formatTime } from "@/lib/utils";

interface StopRow {
  stop_id: string | null;
  stop_name?: string | null;
  stop_sequence: number | null;
  arrival_time?: string | null;
  delay_seconds?: number | null;
}

interface Props {
  stops: StopRow[];
  className?: string;
}

export default function StopTable({ stops, className }: Props) {
  if (!stops.length) {
    return <p className="text-sm text-fg-subtle">No stop data available.</p>;
  }

  return (
    // Denser padding/text on mobile — same trade as WorstStopsTable: shrink
    // the chrome so more fits before handing off to horizontal scroll.
    <div className={`overflow-x-auto rounded border border-line ${className ?? ""}`}>
      <table className="min-w-full text-xs sm:text-sm">
        <thead className="bg-raised text-[10px] uppercase text-fg-subtle sm:text-xs">
          <tr>
            <th className="px-2 py-1.5 text-left sm:px-3 sm:py-2">#</th>
            <th className="px-2 py-1.5 text-left sm:px-3 sm:py-2">Stop</th>
            <th className="px-2 py-1.5 text-right sm:px-3 sm:py-2">Sched.</th>
            <th className="px-2 py-1.5 text-right sm:px-3 sm:py-2">Delay</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {stops.map((s, i) => (
            <tr key={i} className="hover:bg-raised">
              <td className="px-2 py-1.5 text-fg-subtle sm:px-3 sm:py-2">{s.stop_sequence ?? i + 1}</td>
              <td className="px-2 py-1.5 font-medium text-fg sm:px-3 sm:py-2">
                {s.stop_name ?? s.stop_id ?? "—"}
              </td>
              <td className="px-2 py-1.5 text-right text-fg-muted sm:px-3 sm:py-2">
                {s.arrival_time ? formatTime(s.arrival_time) : "—"}
              </td>
              <td
                className={`px-2 py-1.5 text-right font-mono sm:px-3 sm:py-2 ${
                  (s.delay_seconds ?? 0) > 300
                    ? "text-danger"
                    : (s.delay_seconds ?? 0) < -300
                      ? "text-warn"
                      : "text-ok"
                }`}
              >
                {formatDelay(s.delay_seconds ?? null)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
