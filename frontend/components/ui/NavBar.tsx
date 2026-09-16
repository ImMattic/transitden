"use client";
import Image from "next/image";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

type Rect = { left: number; width: number };
import { cn } from "@/lib/utils";
import ThemeMenu from "./ThemeMenu";

const NAV_LINKS = [
  { href: "/", label: "Live Map" },
  { href: "/dashboard", label: "Dashboard" },
  { href: "/trips", label: "Trips" },
];

export default function NavBar() {
  const pathname = usePathname();
  const [menuOpen, setMenuOpen] = useState(false);

  const linkRefs = useRef<(HTMLAnchorElement | null)[]>([]);
  const [hoveredIndex, setHoveredIndex] = useState<number | null>(null);
  // Two pills: `activeRect` is the persistent selected-page highlight, `hoverRect`
  // is the pointer-following preview. Both keep their last geometry when idle so
  // they animate in place rather than collapsing to the left edge.
  const [activeRect, setActiveRect] = useState<Rect | null>(null);
  const [hoverRect, setHoverRect] = useState<Rect | null>(null);

  const activeIndex = NAV_LINKS.findIndex((link) => link.href === pathname);
  const hoveringOther = hoveredIndex !== null && hoveredIndex !== activeIndex;
  // The preview pill emerges from the selected highlight: before we've measured
  // the hovered tab (and once the pointer leaves) it sits on the active tab, so
  // it appears to spawn out of — and retract back into — the selected pill.
  const hoverPos =
    hoveredIndex !== null ? hoverRect ?? activeRect : activeRect;

  const measure = useCallback(() => {
    const rectFor = (i: number): Rect | null => {
      const el = i >= 0 ? linkRefs.current[i] : null;
      return el ? { left: el.offsetLeft, width: el.offsetWidth } : null;
    };
    setActiveRect((prev) => rectFor(activeIndex) ?? prev);
    if (hoveredIndex !== null) {
      const r = rectFor(hoveredIndex);
      if (r) setHoverRect(r);
    }
  }, [activeIndex, hoveredIndex]);

  useEffect(() => {
    measure();
    window.addEventListener("resize", measure);
    // Re-measure once web fonts settle, since glyph widths shift the links.
    document.fonts?.ready.then(measure).catch(() => {});
    return () => window.removeEventListener("resize", measure);
  }, [measure]);

  return (
    <header className="pointer-events-none fixed inset-x-0 top-0 z-[1100] flex justify-center px-4 pt-3">
      <div className="pointer-events-auto w-full max-w-7xl">
        <div className="flex items-center gap-4 rounded-full border border-rtd-darkred bg-rtd-red/95 px-4 py-2 text-white shadow-xl shadow-black/30 backdrop-blur-md sm:gap-8 sm:px-5">
          <Link
            href="/"
            className="flex shrink-0 items-baseline gap-1 text-lg font-extrabold tracking-tight text-white transition-opacity hover:opacity-80 sm:text-xl"
            aria-label="TransitDen — Live Map"
          >
            {/* `transitden_logo_white_trimmed.png` is `transitden_logo_white_512.png`
                with its transparent padding cropped off (the source canvas carried
                13px of empty margin on each side and 59px top/bottom around the
                arch mark) — untrimmed, that dead space sat between the visible
                glyph and the box we align to, so bottom-aligning the box didn't
                bottom-align the icon itself.
                  Height is 0.72em + 3px, not 1em — `em` is the font's full
                em-box, which is taller than what actually reads as "text
                height": Inter publishes a capHeight of 1467/2048 units,
                ≈0.72em, so a 1em-tall icon oversized itself above the caps by
                the remaining ~0.28em no matter how the leftover +Npx on top
                was tuned (that's the bug every earlier "+10px" / "+1px" /
                "+0.25px" pass was chasing — the miss was in the base unit, not
                the addend). The +2px on top of the corrected 0.72em base is
                what makes the mark read a bit bigger than the wordmark, same
                as the original ask. Width is `auto`
                so the crop's own aspect ratio holds instead of being forced
                square. The link is `items-baseline`, so the icon's bottom edge
                still lands on the text baseline. `alt=""` since the link
                already carries the name. `priority` because it sits above the
                fold on every page. `unoptimized` because the source is a few
                KB PNG and the standalone runtime image has no sharp — the
                optimizer would fail at runtime for no gain. */}
            <Image
              src="/transitden_logo_white_trimmed.png"
              alt=""
              width={488}
              height={394}
              priority
              unoptimized
              className="h-[calc(0.72em+1px)] w-auto"
            />
            TransitDen
          </Link>
          <nav
            className="relative hidden sm:flex gap-1"
            onMouseLeave={() => setHoveredIndex(null)}
          >
            {/* Selected-page highlight — stays on the active tab, dims to a faint
                trace while you preview another, then glides over on navigation. */}
            <span
              aria-hidden="true"
              className={cn(
                "pointer-events-none absolute inset-y-0 rounded-full transition-all duration-300 ease-out motion-reduce:transition-none",
                activeRect && activeIndex >= 0 ? "opacity-100" : "opacity-0",
                hoveringOther ? "bg-rtd-darkred/40" : "bg-rtd-darkred",
              )}
              style={{ left: activeRect?.left ?? 0, width: activeRect?.width ?? 0 }}
            />
            {/* Hover preview — solid pill that follows the pointer between tabs. */}
            <span
              aria-hidden="true"
              className={cn(
                "pointer-events-none absolute inset-y-0 rounded-full bg-rtd-darkred transition-all duration-300 ease-out motion-reduce:transition-none",
                hoveringOther ? "opacity-100" : "opacity-0",
              )}
              style={{ left: hoverPos?.left ?? 0, width: hoverPos?.width ?? 0 }}
            />
            {NAV_LINKS.map((link, i) => (
              <Link
                key={link.href}
                href={link.href}
                ref={(el) => {
                  linkRefs.current[i] = el;
                }}
                onMouseEnter={() => setHoveredIndex(i)}
                onFocus={() => setHoveredIndex(i)}
                onBlur={() => setHoveredIndex(null)}
                className={cn(
                  "relative z-10 text-sm font-bold px-3 py-1.5 rounded-full transition-colors",
                  i === activeIndex || i === hoveredIndex
                    ? "text-white"
                    : "text-white/80 hover:text-white",
                )}
              >
                {link.label}
              </Link>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-3 sm:gap-4">
            <ThemeMenu />
            <button
              type="button"
              aria-label="Toggle navigation menu"
              aria-expanded={menuOpen}
              onClick={() => setMenuOpen((open) => !open)}
              className="sm:hidden -mr-1 grid h-8 w-8 place-items-center text-white/80 transition-colors hover:text-white"
            >
              <span className="relative block h-4 w-6" aria-hidden="true">
                <span
                  className={cn(
                    "absolute left-0 block h-0.5 w-6 rounded-full bg-current transition-all duration-300 ease-in-out motion-reduce:transition-none",
                    menuOpen ? "top-1/2 -translate-y-1/2 rotate-45" : "top-0",
                  )}
                />
                <span
                  className={cn(
                    "absolute left-0 top-1/2 block h-0.5 w-6 -translate-y-1/2 rounded-full bg-current transition-all duration-200 ease-in-out motion-reduce:transition-none",
                    menuOpen ? "scale-x-0 opacity-0" : "scale-x-100 opacity-100",
                  )}
                />
                <span
                  className={cn(
                    "absolute left-0 block h-0.5 w-6 rounded-full bg-current transition-all duration-300 ease-in-out motion-reduce:transition-none",
                    menuOpen ? "bottom-1/2 translate-y-1/2 -rotate-45" : "bottom-0",
                  )}
                />
              </span>
            </button>
          </div>
        </div>
        <div
          className={cn(
            "grid overflow-hidden transition-[grid-template-rows,opacity,margin] duration-300 ease-out sm:hidden motion-reduce:transition-none",
            menuOpen ? "mt-2 grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0",
          )}
        >
          <div className="min-h-0">
            <nav
              aria-hidden={!menuOpen}
              className={cn(
                "flex flex-col gap-1 rounded-2xl border border-rtd-darkred bg-rtd-red/95 p-2 text-white shadow-xl shadow-black/30 backdrop-blur-md transition-transform duration-300 ease-out motion-reduce:transition-none",
                menuOpen ? "translate-y-0" : "-translate-y-2",
              )}
            >
              {NAV_LINKS.map((link, i) => (
                <Link
                  key={link.href}
                  href={link.href}
                  tabIndex={menuOpen ? undefined : -1}
                  onClick={() => setMenuOpen(false)}
                  style={{ transitionDelay: menuOpen ? `${80 + i * 55}ms` : "0ms" }}
                  className={cn(
                    "rounded-xl px-3 py-2 text-sm font-bold transition-all duration-300 ease-out motion-reduce:transition-none motion-reduce:translate-x-0 motion-reduce:opacity-100",
                    menuOpen ? "translate-x-0 opacity-100" : "-translate-x-3 opacity-0",
                    pathname === link.href
                      ? "bg-rtd-darkred text-white"
                      : "text-white/80 hover:bg-rtd-darkred/60 hover:text-white",
                  )}
                >
                  {link.label}
                </Link>
              ))}
            </nav>
          </div>
        </div>
      </div>
    </header>
  );
}
