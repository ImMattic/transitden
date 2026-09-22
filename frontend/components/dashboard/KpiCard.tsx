import { Card } from "@/components/ui/Card";
import InfoTip from "@/components/ui/InfoTip";
import { cn } from "@/lib/utils";

interface Props {
  title: string;
  value: string;
  subtitle?: string;
  /** "What does this number mean?" behind a hover/tap "?" beside the title —
   *  for metrics whose name doesn't carry its own definition. */
  hint?: React.ReactNode;
  /** Signed change vs. the previous period (already computed in display units). */
  delta?: number | null;
  /** Suffix shown after the delta number, e.g. "pts" or "s". */
  deltaSuffix?: string;
  /** When true, a negative delta is good (e.g. delay, headway). Default: up is good. */
  lowerIsBetter?: boolean;
  accentColor?: string;
  /** Which edge the hint popover hangs from — "right" for cards in the last
   *  column, so it doesn't run off the viewport. */
  hintAlign?: "left" | "right";
}

function deltaTone(delta: number, lowerIsBetter: boolean): "good" | "bad" | "flat" {
  if (Math.abs(delta) < 0.05) return "flat";
  const improving = lowerIsBetter ? delta < 0 : delta > 0;
  return improving ? "good" : "bad";
}

export default function KpiCard({
  title,
  value,
  subtitle,
  hint,
  delta,
  deltaSuffix = "",
  lowerIsBetter = false,
  accentColor,
  hintAlign = "left",
}: Props) {
  const showDelta = delta !== null && delta !== undefined && Number.isFinite(delta);
  const tone = showDelta ? deltaTone(delta as number, lowerIsBetter) : "flat";
  const toneClass =
    tone === "good" ? "text-ok" : tone === "bad" ? "text-danger" : "text-fg-subtle";
  const arrow = !showDelta ? "" : (delta as number) > 0 ? "▲" : (delta as number) < 0 ? "▼" : "—";

  return (
    <Card className="flex flex-col justify-between">
      {/* A div, not a p: InfoTip renders a div, and the HTML parser implicitly
          closes an open <p> at a block-level start tag — which would desync
          hydration against the server-rendered markup. */}
      <div className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-fg-subtle">
        {title}
        {hint && <InfoTip align={hintAlign}>{hint}</InfoTip>}
      </div>
      <p
        className="mt-1 text-3xl font-bold text-fg"
        style={accentColor ? { color: accentColor } : undefined}
      >
        {value}
      </p>
      <div className="mt-1 flex items-center gap-2">
        {showDelta && (
          <span className={cn("text-xs font-semibold", toneClass)}>
            {arrow} {Math.abs(delta as number).toFixed(1)}
            {deltaSuffix}
          </span>
        )}
        {subtitle && <span className="text-xs text-fg-subtle">{subtitle}</span>}
      </div>
    </Card>
  );
}
