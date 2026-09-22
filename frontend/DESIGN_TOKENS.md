# Design tokens & the Signal palette

Single source of truth for TransitDen's colour system. The runtime values live in
[`app/globals.css`](app/globals.css) (CSS custom properties) and are exposed as
Tailwind utilities in [`tailwind.config.ts`](tailwind.config.ts). This file
explains _what the colours are_ and _why the light and dark themes differ_.

---

## 1. The Signal palette (brand anchors)

Four semantic accents, drawn from RTD's own brand deck. These are the fixed
identity of the palette — the **role → hue** mapping never changes between
themes; only the lightness/chroma _step_ does (see §2).

| Role          | Meaning in the UI                          | Brand anchor        | RTD name     |
| ------------- | ------------------------------------------ | ------------------- | ------------ |
| **Interactive** (`--accent`) | links, focus, active nav, "info" state | `#41C1EF` | RTD Midblue  |
| **Good** (`--ok`)            | on-time, healthy, positive delta       | `#009483` | RTD Teal     |
| **Attention** (`--warn`)     | late-ish, degraded, needs a look       | `#F6871F` | RTD Orange   |
| **Critical** (`--danger`)    | very late, error, big negative         | `#CE0E2D` | RTD Red      |

RTD's other brand/route colours (`rtd-blue`, `rtd-gold`, `rtd-a`…`rtd-w`, …) are
**not** part of this system — they're fixed GTFS/brand values in
`tailwind.config.ts` and are used as-is on both themes.

---

## 2. Why light ≠ dark

> A fully-saturated mid colour _vibrates_ on a near-black surface; the same
> colour, only slightly lightened, _dissolves_ on white.

So each anchor is re-stepped per theme instead of being reused verbatim:

- **Dark theme** (`:root`) — accents are **brighter / a touch less saturated**.
  Several land on or very near the brand anchor itself, which already reads well
  on `#0D0E11`.
- **Light theme** (`:root[data-theme="light"]`) — accents are **deeper and
  more saturated**, so they carry visual weight on white and clear **4.5:1**
  contrast for text use.

| Token       | Dark (`:root`)          | Light (`[data-theme="light"]`) | Light contrast on `#fff` |
| ----------- | ----------------------- | ------------------------------ | ------------------------ |
| `--accent`  | `#41C1EF` `65 193 239`  | `#04789C` `4 120 156`          | 5.0 : 1                  |
| `--ok`      | `#009483` `0 148 131`   | `#007A6B` `0 122 107`          | 5.2 : 1                  |
| `--warn`    | `#F6871F` `246 135 31`  | `#B44D08` `180 77 8`           | 5.2 : 1                  |
| `--danger`  | `#F03E48` `240 62 72`   | `#C50C2B` `197 12 43`          | 6.1 : 1                  |

`--accent-ink` is the colour that goes **on top of** a filled accent chip:
`#0D0E11` on dark, `#FFFFFF` on light.

### Editing the accents

If you retune a light accent, keep it:

1. recognisably the **same hue family** as its dark counterpart / brand anchor,
2. **≥ 4.5:1** on `#FFFFFF` (body-text threshold — check before committing),
3. mirrored into **both** light blocks in `globals.css` — the explicit
   `:root[data-theme="light"]` rule _and_ the
   `@media (prefers-color-scheme: light)` fallback (used for the pre-hydration
   frame / JS-disabled). They must stay identical.

Dark is the palette "we chose" — change it only with a very good reason.

---

## 3. Surface & text tokens

Stored as space-separated RGB triples so Tailwind's
`rgb(var(--x) / <alpha-value>)` can add opacity.

| Token         | Role                                   | Dark        | Light       |
| ------------- | -------------------------------------- | ----------- | ----------- |
| `--canvas`    | page background                        | `#0D0E11`   | `#F4F5F7`   |
| `--card`      | primary raised surface                 | `#16181C`   | `#FFFFFF`   |
| `--raised`    | inset / secondary fill                 | `#1E2126`   | `#ECEEF1`   |
| `--overlay`   | popovers, tooltips, map controls       | `#282C33`   | `#FFFFFF`   |
| `--line`      | default hairline border                | `#2B2F36`   | `#DCDFE5`   |
| `--line-2`    | stronger border / divider             | `#3B414A`   | `#C5CAD2`   |
| `--fg`        | primary text                           | `#ECEEF1`   | `#15181D`   |
| `--fg-muted`  | secondary text                         | `#A2A8B2`   | `#5B616A`   |
| `--fg-subtle` | tertiary / decorative text             | `#7C838E`   | `#7A818B`   |

`--status-alpha` (dark `0.16`, light `0.12`) is the wash strength behind the
`.status-ok / .status-warn / .status-info / .status-danger` helpers — a
semi-transparent tint of the accent under full-strength accent text. Use those
helpers instead of light-only patterns like `bg-red-50 text-red-700`.

---

## 4. Consumers

- **Tailwind classes** — `bg-card`, `text-fg-muted`, `border-line`,
  `bg-accent/10`, `text-ok`, `shadow-card`, … resolve straight from the tokens.
- **Recharts** can't read CSS vars, so [`lib/useChartTheme.ts`](lib/useChartTheme.ts)
  resolves the structural + accent colours via `getComputedStyle` and recomputes
  on the `themechange` event. Its `DARK_FALLBACK` mirrors the dark `:root` block
  for SSR / first paint — **update it if you change a dark token.**
- **Maps** swap CARTO `dark_all` ↔ `light_all` basemaps via `lib/useTheme.ts`.
- **Theme switch UI** — [`components/ui/ThemeMenu.tsx`](components/ui/ThemeMenu.tsx)
  in the navbar (collapsed icon button that unfolds into light / auto / dark).
