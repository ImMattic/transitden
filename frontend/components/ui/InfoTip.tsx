"use client";

import { useEffect, useId, useRef, useState } from "react";
import { cn } from "@/lib/utils";

interface InfoTipProps {
  children: React.ReactNode;
  className?: string;
  /** Which edge of the button the popover hangs from — pick whichever keeps it
   *  on-screen given where the button sits in its row. */
  align?: "left" | "right";
}

/**
 * Small "?" button that reveals a line or two of explanation on hover (pointer
 * devices) or tap (everything else), instead of that text sitting on the page
 * permanently. Used for the "how to read this" captions that were crowding
 * section headers at narrow widths.
 */
export default function InfoTip({ children, className, align = "left" }: InfoTipProps) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const id = useId();

  useEffect(() => {
    if (!open) return;
    function onPointerDown(e: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return (
    <div ref={wrapRef} className={cn("group/tip relative inline-flex", className)}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-describedby={id}
        aria-expanded={open}
        aria-label="More info"
        className="press flex h-4 w-4 shrink-0 items-center justify-center rounded-full border border-line-strong text-[10px] font-bold leading-none text-fg-subtle transition-colors hover:border-accent hover:text-accent focus-visible:border-accent focus-visible:text-accent"
      >
        ?
      </button>
      <div
        role="tooltip"
        id={id}
        className={cn(
          // Leaflet's own panes/controls run z-index up to ~1000 (see
          // MapStatusBar, StopDialog), and since nothing between this button
          // and a nearby map creates its own stacking context, a low z-index
          // here loses to the map even though we're later in the DOM.
          // normal-case: the "?" trigger sometimes sits inside an uppercase
          // label (e.g. KpiCard's title row), and text-transform inherits —
          // without this the popover copy would shout right along with it.
          "pointer-events-none absolute top-full z-[1000] mt-1.5 w-56 max-w-[calc(100vw-2rem)] rounded-md border border-line bg-card p-2 text-[11px] normal-case leading-snug text-fg-muted opacity-0 shadow-card transition-opacity duration-150",
          "group-hover/tip:pointer-events-auto group-hover/tip:opacity-100",
          open && "pointer-events-auto opacity-100",
          align === "right" ? "right-0" : "left-0",
        )}
      >
        {children}
      </div>
    </div>
  );
}
