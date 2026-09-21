export type SortDirection = "asc" | "desc";

/** The ▲/▼ beside a sortable column header. Every sortable header carries one,
 *  dimmed until its column is the active sort, so the arrows don't shift the
 *  layout as the sort moves. */
export default function SortIcon({ active, dir }: { active: boolean; dir: SortDirection }) {
  return (
    <span
      aria-hidden="true"
      className={`ml-1 inline-block ${active ? "text-fg-muted" : "text-fg-subtle/60"}`}
    >
      {active && dir === "desc" ? "▼" : "▲"}
    </span>
  );
}
