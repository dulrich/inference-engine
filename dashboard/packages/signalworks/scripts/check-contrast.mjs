#!/usr/bin/env bun
// WCAG-AA token gate for the fleet's Svelte frontends.
//
//   bun signalworks/scripts/check-contrast.mjs <path/to/app.css>              (v2 — default)
//   bun signalworks/scripts/check-contrast.mjs --signalworks <paths…>         (v3 — Signalworks contract)
//
// DEFAULT (v2) enforces references/color-critical-surround-theming.md sections 4 and 6,
// and the per-level token contract in references/visual-language.md, over one app.css:
//   1. every declared on-surface pair clears AA (>=4.5 text; >=3.0 accent-as-UI-glyph)
//      at all three surround levels (med = :root defaults; dark/light = overrides);
//   2. surface tokens are achromatic (max(R,G,B) - min(R,G,B) small) at every level;
//   3. no color literal outside :root / :root[data-surround=...] token blocks, except
//      declarations marked `/* chroma-chip */` on the same line;
//   4. the status quintet is never used as text -- `color: var(--ok)` and friends are
//      fills; status-as-text goes through the per-level `--<status>-text` tokens;
//   5. a declared `--<status>-text` keeps its fill's hue (within HUE_TOLERANCE), so the
//      text and the dot/pill it labels read as the same status;
//   6. `--accent-wash` (the row-hover tint) still carries AA text once composited over
//      the surface it washes.
// Prints "AA PASS" and exits 0, or the failing pairs/tokens/lines and exits 1.
//
// --signalworks (v3) validates the Signalworks contract (CC-01…CC-18, extended AA) across
// one or more paths (dirs expand to *.css + *.svelte recursively, and their index.html is
// probed for the CC-14 boot check). A *.svelte file contributes its <style> blocks to the
// CC-01/CC-05/CC-08/CC-09/CC-15/CC-16/CC-18 lint, its markup to CC-08 (page-rail),
// CC-11 (link-out anatomy), CC-18 (sw-nav glyph placement) + CC-14
// (render-stability assists), and its script side to the CC-17 series-path check;
// token blocks are merged across package + tool CSS. CC-08 is positive page-rail
// conformance (container carries .page/.page.prose/.page.wide; app CSS never sizes it) plus
// a >600px non-quantized width/max-width lint (≤600px exempt). CC-05 additionally requires a
// .row base rule declaring color: var(--text). CC-14 assists are warnings except the boot
// marker, which is an exact error. CC-16 seg enforcement (2026-07-19): base.css must
// ship the .seg-group/.seg utilities (frame + 32px/650/radius-0 asserted); tool rules
// whose subject is a canon seg/chip class are layout-accommodations-only
// (SEG_LAYOUT_EXEMPT; sole chrome carve-out: color on .seg-tint…active hue carriers).
// The CC-18 weight-per-surface table warns on off-table font-weight for control/pill
// surfaces and on nonzero per-segment border-radius for seg-named look-alikes. The default path stays byte-compatible v2 so un-migrated
// tools keep passing untouched.
// Same output/exit-code contract: FAIL lines + nonzero on error-level findings; warnings
// (WARN lines) are informational and do not fail the gate.

const LEVELS = ["dark", "med", "light"];
const SURFACE_TOKENS = [
  "--bg", "--panel", "--panel-2", "--inset", "--line",
  "--backdrop", "--header-bg", "--glow",
];
const ACHROMA_SPREAD_MAX = 8;

// The constant-chroma status quintet: fills only (dots, pills, chart series).
const STATUS_FILLS = ["--ok", "--warn", "--error", "--info", "--user"];
// Max hue drift, in degrees, between --<status>-text and its --<status> fill.
const HUE_TOLERANCE = 10;
// Below this HSL saturation a color has no meaningful hue, so the hue check is skipped.
const HUE_MIN_SAT = 0.1;
// Renamed in the 2026-07-15 contract pass -> the quintet-aligned names.
const DEPRECATED_TOKENS = { "--danger-text": "--error-text", "--warning-text": "--warn-text" };

// [fg, bg, min ratio, required?] — optional pairs are skipped when a token is absent.
const PAIRS = [
  ["--text", "--bg", 4.5, true],
  ["--text", "--panel", 4.5, true],
  ["--text", "--panel-2", 4.5, false],
  ["--text", "--inset", 4.5, false],
  ["--text-dim", "--bg", 4.5, false],
  ["--text-dim", "--panel", 4.5, false],
  ["--muted", "--bg", 4.5, false],
  ["--muted", "--panel", 4.5, false],
  ["--muted", "--panel-2", 4.5, false], // muted text on a raised surface: badges, table heads
  ["--heading", "--bg", 4.5, false],
  ["--heading", "--panel", 4.5, false],
  ["--accent-text", "--bg", 4.5, true],
  ["--accent-text", "--panel", 4.5, true],
  ["--accent-text", "--panel-2", 4.5, false],
  ["--on-accent", "--accent", 4.5, true],
  ["--on-accent", "--accent-hover", 4.5, false],
  ["--accent", "--bg", 3.0, true], // accent fill as a UI glyph on the page bg
  // Status-as-text: every declared -text token, on every surface it can land on.
  ...STATUS_FILLS.flatMap((f) =>
    ["--bg", "--panel", "--panel-2", "--inset"].map((s) => [`${f}-text`, s, 4.5, false]),
  ),
];
const REQUIRED_TOKENS = ["--bg", "--panel", "--text", "--accent", "--on-accent", "--accent-text"];
// [wash token, surface it washes, foregrounds that must stay legible on top, min ratio]
const WASHES = [["--accent-wash", "--panel", ["--text", "--muted"], 4.5]];

function parseColor(value) {
  const v = value.trim();
  let m = /^#([0-9a-f]{3})$/i.exec(v);
  if (m) {
    const [r, g, b] = m[1].split("").map((c) => parseInt(c + c, 16));
    return { r, g, b, a: 1 };
  }
  m = /^#([0-9a-f]{6})$/i.exec(v);
  if (m) {
    const n = parseInt(m[1], 16);
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255, a: 1 };
  }
  m = /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)$/i.exec(v);
  if (m) return { r: +m[1], g: +m[2], b: +m[3], a: m[4] === undefined ? 1 : +m[4] };
  return null;
}

const lin = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
const luminance = ({ r, g, b }) => 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
function ratio(fg, bg) {
  const [l1, l2] = [luminance(fg), luminance(bg)];
  const [hi, lo] = l1 > l2 ? [l1, l2] : [l2, l1];
  return (hi + 0.05) / (lo + 0.05);
}
// Composite a translucent color over an opaque one (simple source-over).
const composite = (fg, bg) => ({
  r: fg.r * fg.a + bg.r * (1 - fg.a),
  g: fg.g * fg.a + bg.g * (1 - fg.a),
  b: fg.b * fg.a + bg.b * (1 - fg.a),
  a: 1,
});
function hsl({ r, g, b }) {
  r /= 255; g /= 255; b /= 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
  let h = 0;
  if (d) {
    if (mx === r) h = ((g - b) / d) % 6;
    else if (mx === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h *= 60; if (h < 0) h += 360;
  }
  const l = (mx + mn) / 2;
  return { h, s: d === 0 ? 0 : d / (1 - Math.abs(2 * l - 1)), l };
}
const hueGap = (a, b) => { const d = Math.abs(a - b) % 360; return d > 180 ? 360 - d : d; };
const spreadOf = ({ r, g, b }) => Math.max(r, g, b) - Math.min(r, g, b);

// Resolve a token value to a color: plain literal, var(--x) reference, or color-mix().
// color-mix percentages that do not sum to 100% are a bug (the browser renormalizes them,
// so the authored intent and the painted result silently diverge) — reported, not guessed.
// `bag` is the per-level token map; `warn` collects mix warnings (shared shape v2/v3).
function resolveColor(value, bag, warn, depth = 0) {
  if (value === undefined || depth > 8) return null;
  const v = value.trim();
  const direct = parseColor(v);
  if (direct) return direct;

  let m = /^var\(\s*(--[\w-]+)\s*(?:,\s*(.+))?\)$/.exec(v);
  if (m) return resolveColor(bag[m[1]] ?? m[2], bag, warn, depth + 1);

  m = /^color-mix\(\s*in\s+srgb\s*,\s*(.+)\)$/i.exec(v);
  if (m) {
    const parts = m[1].split(/\s*,\s*(?![^()]*\))/);
    if (parts.length !== 2) return null;
    const operands = parts.map((p) => {
      const pm = /^(.*?)\s+([\d.]+)%$/.exec(p.trim());
      return pm ? { color: resolveColor(pm[1], bag, warn, depth + 1), pct: +pm[2] } : { color: resolveColor(p, bag, warn, depth + 1), pct: null };
    });
    if (operands.some((o) => !o.color)) return null;
    let [a, b] = operands;
    if (a.pct === null && b.pct === null) { a.pct = 50; b.pct = 50; }
    else if (a.pct === null) a.pct = 100 - b.pct;
    else if (b.pct === null) b.pct = 100 - a.pct;
    const sum = a.pct + b.pct;
    if (Math.abs(sum - 100) > 0.01) {
      warn.push(`color-mix percentages sum to ${sum}%, not 100% — the browser renormalizes (${v})`);
    }
    const wa = a.pct / sum, wb = b.pct / sum;
    return {
      r: a.color.r * wa + b.color.r * wb,
      g: a.color.g * wa + b.color.g * wb,
      b: a.color.b * wa + b.color.b * wb,
      a: a.color.a * wa + b.color.a * wb,
    };
  }
  return null;
}

// ======================================================================================
//  v2 — default invocation (byte-compatible with the pre-Signalworks gate)
// ======================================================================================
async function runV2(paths) {
  const file = paths[0];
  if (!file) {
    console.error("usage: bun packages/signalworks/scripts/check-contrast.mjs <path/to/app.css>");
    process.exit(2);
  }
  const css = await Bun.file(file).text();

  const isTokenSelector = (sel) =>
    sel.split(",").every((s) => /^:root(\[data-surround="(dark|med|light)"\])?$/.test(s.trim()));
  const levelOf = (sel) => /data-surround="(dark|med|light)"/.exec(sel)?.[1] ?? "med";
  // `color: var(--ok)` — the status quintet used as text. The leading [^-\w] guard keeps
  // `background-color:` / `border-color:` (fills, which are legitimate) from matching.
  const fillAsTextRe = new RegExp(
    `(^|[^-\\w])color\\s*:\\s*var\\(\\s*(${STATUS_FILLS.join("|")})\\s*[,)]`,
    "i",
  );

  const blockRe = /([^{}]+)\{([^{}]*)\}/g;
  const raw = { dark: {}, med: {}, light: {} }; // level -> token -> raw value string
  const outside = []; // { line, text } — color literals outside token blocks
  const fillAsText = []; // { line, text, token } — quintet used as text
  let match;
  while ((match = blockRe.exec(css)) !== null) {
    const sel = match[1].replace(/\/\*[\s\S]*?\*\//g, "").trim();
    const body = match[2];
    if (isTokenSelector(sel)) {
      const level = levelOf(sel);
      // Strip comments BEFORE splitting: a `;` inside a comment would otherwise split
      // mid-comment and swallow the declaration after it. When that declaration is an
      // optional token, every check on it is skipped and the gate goes silently blind —
      // the same class of dead check this gate exists to prevent.
      for (const decl of body.replace(/\/\*[\s\S]*?\*\//g, "").split(";")) {
        const m = /^\s*(--[\w-]+)\s*:\s*(.+)$/s.exec(decl);
        if (m) raw[level][m[1]] = m[2].trim();
      }
    } else {
      const bodyStart = match.index + match[1].length;
      for (const [i, lineText] of body.split("\n").entries()) {
        if (lineText.includes("chroma-chip")) continue;
        const line = css.slice(0, bodyStart).split("\n").length + i;
        const fm = fillAsTextRe.exec(lineText);
        if (fm) fillAsText.push({ line, text: lineText.trim(), token: fm[2] });
        if (/#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(/i.test(lineText)) {
          outside.push({ line, text: lineText.trim() });
        }
      }
    }
  }

  // Cascade: each level = :root (med) defaults overlaid with that level's block.
  const rawByLevel = Object.fromEntries(
    LEVELS.map((lv) => [lv, { ...raw.med, ...(lv === "med" ? {} : raw[lv]) }]),
  );

  const mixWarnings = [];
  const resolve = (value, level) => resolveColor(value, rawByLevel[level], mixWarnings);

  const tokens = Object.fromEntries(LEVELS.map((lv) => [lv, {}]));
  for (const lv of LEVELS) {
    for (const [name, value] of Object.entries(rawByLevel[lv])) {
      const before = mixWarnings.length;
      const color = resolve(value, lv);
      // tag any mix warnings raised while resolving this level with the level prefix
      for (let i = before; i < mixWarnings.length; i++) mixWarnings[i] = `[${lv}] ${mixWarnings[i]}`;
      if (color) tokens[lv][name] = { value, color };
    }
  }

  // --- checks ----------------------------------------------------------------------------
  const failures = [];

  for (const t of REQUIRED_TOKENS) {
    if (!tokens.med[t]) failures.push(`missing required token ${t} (base :root block)`);
  }
  for (const [old, replacement] of Object.entries(DEPRECATED_TOKENS)) {
    if (LEVELS.some((lv) => rawByLevel[lv][old] !== undefined)) {
      failures.push(`deprecated token ${old}: rename to ${replacement} (see references/visual-language.md)`);
    }
  }

  for (const lv of LEVELS) {
    for (const t of SURFACE_TOKENS) {
      const entry = tokens[lv][t];
      if (!entry) continue;
      const { r, g, b } = entry.color;
      const spread = Math.max(r, g, b) - Math.min(r, g, b);
      if (spread > ACHROMA_SPREAD_MAX) {
        failures.push(`[${lv}] surface ${t}: ${entry.value} is not achromatic (RGB spread ${spread})`);
      }
    }

    let checked = 0;
    for (const [fg, bg, min, required] of PAIRS) {
      const f = tokens[lv][fg];
      const g = tokens[lv][bg];
      if (!f || !g) {
        if (required && !failures.some((x) => x.includes(`missing required token`))) {
          failures.push(`[${lv}] required pair ${fg}/${bg}: token missing`);
        }
        continue;
      }
      if (f.color.a < 1 || g.color.a < 1) continue; // alpha tokens are checked as washes
      checked++;
      const r = ratio(f.color, g.color);
      if (r < min) {
        failures.push(`[${lv}] ${fg} on ${bg}: ${r.toFixed(2)} < ${min} (${f.value} on ${g.value})`);
      }
    }
    if (checked === 0) failures.push(`[${lv}] no checkable pairs — token blocks not found?`);

    // A -text token must carry its fill's hue, or the prose and the pill disagree.
    for (const fill of STATUS_FILLS) {
      const f = tokens[lv][fill];
      const t = tokens[lv][`${fill}-text`];
      if (!f || !t) continue;
      const [hf, ht] = [hsl(f.color), hsl(t.color)];
      if (hf.s < HUE_MIN_SAT || ht.s < HUE_MIN_SAT) continue;
      const gap = hueGap(hf.h, ht.h);
      if (gap > HUE_TOLERANCE) {
        failures.push(
          `[${lv}] ${fill}-text hue ${ht.h.toFixed(0)} is ${gap.toFixed(0)}deg from ${fill} hue ${hf.h.toFixed(0)} ` +
          `(max ${HUE_TOLERANCE}) — ${t.value} vs ${f.value}`,
        );
      }
    }

    // A translucent wash must leave the text on top of it legible.
    for (const [wash, base, foregrounds, min] of WASHES) {
      const w = tokens[lv][wash];
      const b = tokens[lv][base];
      if (!w || !b) continue;
      const surface = w.color.a < 1 ? composite(w.color, b.color) : w.color;
      for (const fg of foregrounds) {
        const f = tokens[lv][fg];
        if (!f || f.color.a < 1) continue;
        const r = ratio(f.color, surface);
        if (r < min) {
          failures.push(
            `[${lv}] ${fg} on ${wash} over ${base}: ${r.toFixed(2)} < ${min} (${w.value} over ${b.value})`,
          );
        }
      }
    }
  }

  for (const { line, text, token } of fillAsText) {
    failures.push(
      `status fill used as text at ${file}:${line}: ${text} — ${token} is a fill (dot/pill/series); ` +
      `use var(${token}-text) for prose`,
    );
  }
  for (const w of mixWarnings) failures.push(w);
  for (const { line, text } of outside) {
    failures.push(`color literal outside token blocks at ${file}:${line}: ${text} (use var(--...) or mark /* chroma-chip */)`);
  }

  if (failures.length) {
    for (const f of failures) console.error(`FAIL ${f}`);
    console.error(`\n${failures.length} failure(s) in ${file}`);
    process.exit(1);
  }
  console.log(`AA PASS (${file}: 3 levels checked)`);
}

// ======================================================================================
//  v3 — --signalworks (CC-01…CC-15 + extended AA), the Signalworks contract gate
// ======================================================================================

import { readdirSync, statSync, existsSync } from "node:fs";
import { join } from "node:path";

// CC-02: the achromatic surround stack (surfaces text lands on). Canon numbers the rule:
// "max RGB channel spread ≤ 4, gate-checked" (design round-trip R2; dark --panel-2
// #202124 sits blessed at the ceiling).
const V3_ACHROMA_MAX = 4;
const V3_SURFACES = ["--bg", "--panel", "--panel-2", "--inset"];
// Border/effect surrounds: must stay roughly neutral, but not held to the surface stack's
// tight ≤4 (a hairline may carry a faint tint). Uses the v2 achromatic bound.
const V3_NEUTRAL = ["--line", "--header-bg", "--glow", "--backdrop"];
const V3_NEUTRAL_MAX = ACHROMA_SPREAD_MAX;
// CC-04: chip-bg token must sit within this RGB-distance of composite(hue @5% over --panel).
const CHIP_TOLERANCE = 4;
// CC-04: pill hue (label/border) graphical contrast floor (WCAG 1.4.11).
const PILL_MIN = 3.0;
// CC-01 in-block: HSL saturation above which a literal counts as genuinely hued.
const CHROMA_SAT_MIN = 0.15;
// CC-08: the only px content max-widths the contract sanctions.
const ALLOWED_WIDTHS = new Set([760, 1180, 1480, 1540]);
// CC-10: the canonical accent family table (orange = default / status-heavy; green = content).
const CANON_ACCENT = {
  orange: {
    med:   { "--accent": "#ff8a3d", "--on-accent": "#1a1108", "--accent-hover": "#ffa05c", "--accent-text": "#ffbe91" },
    dark:  { "--accent": "#ff8a3d", "--on-accent": "#1a1108", "--accent-hover": "#ffa05c", "--accent-text": "#ff8a3d" },
    light: { "--accent": "#f07714", "--on-accent": "#1a1108", "--accent-hover": "#d96a0e", "--accent-text": "#7a3800" },
  },
  green: {
    med:   { "--accent": "#b4d66d", "--on-accent": "#12180f", "--accent-hover": "#c6e685", "--accent-text": "#c6e685" },
    dark:  { "--accent": "#b4d66d", "--on-accent": "#12180f", "--accent-hover": "#c6e685", "--accent-text": "#b4d66d" },
    light: { "--accent": "#7ea83a", "--on-accent": "#12180f", "--accent-hover": "#6d9430", "--accent-text": "#2a3d0f" },
  },
};

// CC-01 in-block: which token names may legitimately carry a chromatic literal inside a
// :root block. Everything else in a token block must be achromatic. Note: `--text` etc. end
// in "text" but are achromatic, so they never reach this predicate — it only gates chroma.
const chromaAllowed = (name) =>
  /-hue$/.test(name) ||
  /-chip-bg(-hover)?$/.test(name) ||
  /^--viz-[1-5]$/.test(name) ||
  name === "--sel-tint" ||
  /^--accent(-hover|-text|-sweep)?$/.test(name) ||
  name === "--on-accent" ||
  /^--focus-(border|shadow)$/.test(name) ||
  /^--(ok|warn|error|info|user)(-text)?$/.test(name);

const isTokenSelectorV3 = (sel) =>
  sel.split(",").every((s) => /^:root(\[data-(surround|accent)="[^"]+"\])*$/.test(s.trim()));
const isPackageFile = (p) => /signalworks[\/\\]css[\/\\]/.test(p);
const isBaseCss = (p) => /(^|[\/\\])base\.css$/.test(p);
const lineAt = (content, offset) => content.slice(0, offset).split("\n").length;

// CC-08 positive: does a selector's SUBJECT compound target the page container?
// (the <main> element, or the package `.page` class the tool must not re-size.) Only the
// last compound is the subject, so `.page > section { max-width }` — subject `section` —
// is correctly ignored while `main.page` / `.page` are caught.
function targetsContainer(sel) {
  return sel.split(",").some((part) => {
    const compounds = part.trim().split(/[\s>+~]+/).filter(Boolean);
    const subject = compounds[compounds.length - 1] || "";
    return /^main(?![\w-])/.test(subject) || /\.page(?![\w-])/.test(subject);
  });
}

// CC-11 control-height: is a selector part's SUBJECT a button-like control?
// Form-field subjects (input/textarea/select — 38px text fields, 18–22px checkbox
// subjects) and row-affordance classes (.row, .row-button, account rows) are
// block-level geometry, out of scope. A class-bearing subject is judged by its
// class names (button.generate is a named CTA, not a chrome control); only a
// bare <button> element subject is implicitly button-like (main button,
// .tabs button, .segmented button).
const CONTROL_CLASS_RE =
  /^(?:btn|button|toggle|seg|segmented|chip|tabs?|control)$|^(?:btn|seg)-|-(?:btn|button|toggle|chip|seg|control)$/;
function controlSubject(sel) {
  return sel.split(",").some((part) => {
    const compounds = part.trim().replace(/:(?:not|has|is|where)\([^)]*\)/g, "").split(/[\s>+~]+/).filter(Boolean);
    const subject = compounds[compounds.length - 1] || "";
    if (/^(?:input|textarea|select)(?![\w-])/i.test(subject)) return false;
    const classes = [...subject.matchAll(/\.([\w-]+)/g)].map((m) => m[1].toLowerCase());
    if (classes.some((c) => /(?:^|-)row(?:-|$)/.test(c))) return false;
    if (classes.length) return classes.some((c) => CONTROL_CLASS_RE.test(c));
    return /^button(?![\w-])/i.test(subject);
  });
}

// CC-18: a tool rule targeting .sw-nav may only declare layout accommodations (the pft
// narrow-viewport overflow-x is the sanctioned precedent); chrome/typography properties
// are app-level overrides of the canon nav and error.
const SWNAV_LAYOUT_EXEMPT =
  /^(?:overflow(?:-[xy])?|scroll-[\w-]+|scrollbar-[\w-]+|-webkit-overflow-scrolling|flex(?:-[\w-]+)?|order|align-self|margin(?:-[\w-]+)?|min-width|max-width|width)$/;

// CC-16: a tool rule whose SUBJECT is a canon seg/chip class may only declare layout
// accommodations. The exempt set is the WP-2..WP-8 fleet inventory: positioning context
// for hidden-input label segments, flex/width for full-width groups, margin for toolbar
// placement, appearance/margin UA resets on real <button> segments. Chrome/typography
// stays package-owned; sole chrome carve-out is segTintCarrier below.
const SEG_CANON_RE = /\.(?:seg|seg-group|seg-icon|seg-tint|seg-ok|seg-neutral|chip-toggle)(?![\w-])/;
const SEG_LAYOUT_EXEMPT =
  /^(?:display|order|align-self|justify-content|flex(?:-[\w-]+)?|grid(?:-[\w-]+)?|margin(?:-[\w-]+)?|min-width|max-width|width|position|inset|top|right|bottom|left|z-index|overflow(?:-[xy])?|(?:-webkit-)?appearance)$/;
function segSubjectOf(part) {
  const compounds = part.trim().replace(/:(?:not|has|is|where)\([^)]*\)/g, "").split(/[\s>+~]+/).filter(Boolean);
  return compounds[compounds.length - 1] || "";
}
function segCanonSubject(sel) {
  return sel.split(",").some((part) => SEG_CANON_RE.test(segSubjectOf(part)));
}
// The ratified hue-carrier mechanism: `.seg-tint.<marker>.active { color: var(--*-text) }`
// (the canon tint derives from currentColor, so the tool names the hue). Exempts `color`
// only when EVERY selector part is such a carrier.
function segTintCarrier(sel) {
  const parts = sel.split(",").map((p) => p.trim()).filter(Boolean);
  return parts.length > 0 && parts.every((p) =>
    /\.seg-tint(?![\w-])/.test(p) && /\.active(?![\w-])/.test(p));
}
// CC-16/CC-18 look-alike heuristics: pill-ish subjects (status/tag pills, badges) and
// seg-NAMED tool classes that are not canon subjects (.segmented, .seg-readout style
// names — canon subjects error above instead of warning here).
function pillSubject(sel) {
  return sel.split(",").some((part) => {
    const classes = [...segSubjectOf(part).matchAll(/\.([\w-]+)/g)].map((m) => m[1].toLowerCase());
    return classes.some((c) => /(?:^|-)(?:pill|badge)(?:-|$)/.test(c));
  });
}
function segNamedSubject(sel) {
  // Part-wide (not subject-only): the pre-migration look-alike shape puts the seg
  // name on the ancestor (.segmented button { border-radius: 6px }).
  return sel.split(",").some((part) => {
    const classes = [...part.matchAll(/\.([\w-]+)/g)].map((m) => m[1].toLowerCase());
    return classes.some((c) => /(?:^|-)seg(?:-|$|mented)/.test(c));
  });
}

// CC-18 ambient-readout: does a selector name a readout-ish subject that is NOT a
// componentized pattern? Component-owned weight wins (the precedence clause): CC-04
// pills/badges, CC-16 seg labels, buttons, tabs and labels keep their canon weights,
// as do heading-element subjects (h1–h6, th, dt, legend — CC-09's business).
const READOUT_RE = /status|metric|countdown|readout|playhead|media[-_]?(?:state|resolution)/i;
const COMPONENTIZED_RE = /pill|badge|seg|btn|button|tab|heading|label/i;
function readoutSubject(sel) {
  return sel.split(",").some((part) => {
    const p = part.trim();
    const compounds = p.replace(/:(?:not|has|is|where)\([^)]*\)/g, "").split(/[\s>+~]+/).filter(Boolean);
    const subject = compounds[compounds.length - 1] || "";
    if (/^(?:h[1-6]|th|dt|legend|summary)(?![\w-])/i.test(subject)) return false;
    if (COMPONENTIZED_RE.test(p)) return false;
    return READOUT_RE.test(p);
  });
}

// CC-11 regression guard: --h-compact was retired in the 2026-07-19 single-height
// revision — any reference (definition or var() read) reopens the split.
function checkHCompact(content, file, errors) {
  for (const [idx, lineText] of content.split("\n").entries()) {
    if (/--h-compact\b/.test(lineText)) {
      errors.push(`CC-11 --h-compact reference at ${file}:${idx + 1}: ${lineText.trim()} (retired — one control height fleet-wide, var(--h-control))`);
    }
  }
}

// CC-14 assist: extract each {#each …} tag's inner text (between `{#each` and the tag's
// matching `}`), brace-aware so object destructuring `{ a, b }` and `${…}` keyed template
// literals don't truncate it.
function eachTags(content) {
  const out = [];
  let i = 0;
  while ((i = content.indexOf("{#each", i)) >= 0) {
    let depth = 0, j = i;
    for (; j < content.length; j++) {
      const ch = content[j];
      if (ch === "{") depth++;
      else if (ch === "}") { depth--; if (depth === 0) break; }
    }
    out.push(content.slice(i + "{#each".length, j).trim());
    i = j + 1;
  }
  return out;
}

function collectInputs(paths, errors) {
  const cssFiles = [];
  const svelteFiles = [];
  const htmlSet = new Set();
  // The dir walk now collects .svelte too, so a tool's scoped component styles enter the
  // CC-01/CC-05/CC-08/CC-09 scan (test:theme passes apps/web/src, not just app.css).
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const full = join(dir, name);
      const st = statSync(full);
      if (st.isDirectory()) walk(full);
      else if (name.endsWith(".css")) cssFiles.push(full);
      else if (name.endsWith(".svelte")) svelteFiles.push(full);
    }
  };
  // CC-14 clause 4 needs the tool's index.html for the surround-boot ordering check. A src
  // dir input (apps/web/src) sits one level below it (apps/web/index.html); a web-root input
  // holds it directly. Probe both, dedup, skip silently when absent (ad-hoc *.css runs).
  const addHtml = (dir) => {
    for (const cand of [join(dir, "index.html"), join(dir, "..", "index.html")]) {
      if (existsSync(cand)) htmlSet.add(cand);
    }
  };
  for (const p of paths) {
    let st;
    try { st = statSync(p); } catch { errors.push(`CC-00 input path not found: ${p}`); continue; }
    if (st.isDirectory()) { walk(p); addHtml(p); }
    else if (p.endsWith(".css")) cssFiles.push(p);
    else if (p.endsWith(".svelte")) svelteFiles.push(p);
    else if (p.endsWith(".html")) htmlSet.add(p);
    else errors.push(`CC-00 unsupported input (want dir / *.css / *.svelte): ${p}`);
  }
  return { cssFiles, svelteFiles, htmlFiles: [...htmlSet] };
}

async function runV3(paths) {
  if (!paths.length) {
    console.error("usage: bun packages/signalworks/scripts/check-contrast.mjs --signalworks <dir|*.css|*.svelte>…");
    process.exit(2);
  }
  const errors = [];
  const warnings = [];
  const { cssFiles, svelteFiles, htmlFiles } = collectInputs(paths, errors);

  // Token store: family -> level -> token -> { value, file, line }
  const store = { orange: { dark: {}, med: {}, light: {} }, green: { dark: {}, med: {}, light: {} } };
  const defSites = new Map(); // "family|level|token" -> [{ file, line, tool }]

  // CC-09 eyebrow dock: base.css owns it (.eyebrow sheds its UA margin, the adjacent
  // page title docks 8px above); no other input may redeclare an eyebrow margin.
  let sawBaseCss = false;
  let dockEyebrowZero = false;
  let dockTitleMargin = false;
  // CC-05: the .row base rule must declare color: var(--text) somewhere in the merged CSS.
  let rowColorDeclared = false;
  // CC-18: base.css must pin the sw-nav pill typography and the --panel-2 active block.
  let swNavFontDeclared = false;
  let swNavActiveDeclared = false;
  // CC-16: base.css must ship the seg utilities (.seg-group frame + 32px/650/radius-0 .seg).
  let segGroupDeclared = false;
  let segBaseDeclared = false;
  // CC-17: the vendored payload must carry tokens/dataviz.css (--viz-1..5).
  let sawDataviz = false;

  // Scan one stylesheet body (a .css file, or one .svelte <style> block).
  //  opts.svelte  -> restrict findings to CC-01 (literals) + CC-05 (hover/mix/sel-tint/wash)
  //                  + the block-level CC-15/CC-16 selector heuristics
  //  opts.lineBase-> line offset for <style> blocks inside a .svelte file
  function scan(content, file, opts = {}) {
    const { svelte = false, lineBase = 0 } = opts;
    const tool = !isPackageFile(file);
    const blockRe = /([^{}]+)\{([^{}]*)\}/g;
    const fillAsTextRe = new RegExp(
      `(^|[^-\\w])color\\s*:\\s*var\\(\\s*(${STATUS_FILLS.join("|")})\\s*[,)]`,
      "i",
    );
    let match;
    while ((match = blockRe.exec(content)) !== null) {
      const sel = match[1].replace(/\/\*[\s\S]*?\*\//g, "").trim();
      const rawBody = match[2];
      const bodyStart = match.index + match[1].length + 1;

      if (!svelte && isTokenSelectorV3(sel)) {
        const family = /data-accent="green"/.test(sel) ? "green" : "orange";
        const level = /data-surround="(dark|med|light)"/.exec(sel)?.[1] ?? "med";
        // Iterate declarations by offset so token-def line numbers stay accurate.
        const declRe = /(--[\w-]+)\s*:\s*([^;]*)/g;
        let dm;
        while ((dm = declRe.exec(rawBody)) !== null) {
          const token = dm[1];
          const value = dm[2].replace(/\/\*[\s\S]*?\*\//g, "").trim();
          if (!value) continue;
          const line = lineBase + lineAt(content, bodyStart + dm.index);
          // CC-05: --accent-wash is abolished (drop v2's wash-AA pair in this mode).
          if (token === "--accent-wash") {
            errors.push(`CC-05 --accent-wash is abolished at ${file}:${line} (removed from the contract)`);
          }
          // Duplicate token definition (same family+level) across the merged inputs.
          const key = `${family}|${level}|${token}`;
          const sites = defSites.get(key) ?? [];
          sites.push({ file, line, tool });
          defSites.set(key, sites);
          store[family][level][token] = { value, file, line };
          // CC-01 in-block chroma: a *hued* literal is only allowed on allowlisted tokens.
          // Gate on HSL saturation (not raw RGB spread) so near-neutral text tokens like
          // --muted/--text-faint (sat ~0.02) don't trip it — only genuine color does.
          const lit = parseColor(value);
          if (lit && !V3_SURFACES.includes(token) && !V3_NEUTRAL.includes(token) &&
              hsl(lit).s > CHROMA_SAT_MIN && !chromaAllowed(token)) {
            errors.push(`CC-01 chromatic literal on ${token} at ${file}:${line}: ${value} (only --*-hue, --*-chip-bg, --sel-tint, accent & status tokens may carry chroma)`);
          }
        }
        continue;
      }

      // --- non-token rule (or any block in a .svelte <style>) ---------------------------
      const hover = /:hover(\b|[^-\w])/.test(sel);
      const isPrimaryBtn = /\.btn-primary\b/.test(sel);
      const isDangerBtn = /\.btn-danger\b/.test(sel);
      // CC-05: the .row base rule must declare color: var(--text) (button rows don't inherit
      // the UA `buttontext` default). Track across all merged CSS; verdict rendered below.
      if (!svelte && sel.split(",").some((p) => p.trim() === ".row") &&
          /(^|[^-\w])color\s*:\s*var\(\s*--text\s*\)/.test(rawBody.replace(/\/\*[\s\S]*?\*\//g, ""))) {
        rowColorDeclared = true;
      }
      // CC-08 positive: a consumer's top-level page container (subject `main` / `.page`) must
      // not carry width/max-width in app CSS in any form — the package's .page rail owns page
      // sizing. Package CSS (which defines .page) is exempt.
      if (!svelte && tool && targetsContainer(sel)) {
        const cm = /(?:^|[^-\w])(max-width|width)\s*:\s*([^;{}]+)/i.exec(
          rawBody.replace(/\/\*[\s\S]*?\*\//g, ""),
        );
        if (cm) {
          const line = lineBase + lineAt(content, match.index);
          errors.push(`CC-08 app CSS sizes the page container at ${file}:${line}: ${cm[1]}: ${cm[2].trim()} (${sel.trim()}) — remove it; .page / .page.prose / .page.wide own page width`);
        }
      }
      // CC-15: app CSS / svelte styles must not declare geometry on bare element
      // selectors (button/a/select/input/textarea) — package components declare their
      // geometry exhaustively, but a bare rule's reach is uncontrolled and the next
      // undeclared property would be filled uncontested (the QA-remediation defect
      // class). Scope to a container (`main button`) or a class instead. Element +
      // pseudo-class parts (button:disabled) count as bare; compound/descendant/class
      // selectors are exempt, as are package files.
      if (tool) {
        const bare = sel.split(",").map((p) => p.trim()).filter((p) =>
          /^(button|a|select|input|textarea)(\s*::?[\w-]+(\([^)]*\))?)*$/i.test(p));
        if (bare.length > 0) {
          const gm = /(?:^|[^-\w])((?:border(?:-(?:top|bottom)-(?:left|right))?-radius)|min-height|min-width|height|width|padding(?:-[\w-]+)?)\s*:/i.exec(
            rawBody.replace(/\/\*[\s\S]*?\*\//g, (c) => " ".repeat(c.length)),
          );
          if (gm) {
            const line = lineBase + lineAt(content, bodyStart + gm.index);
            errors.push(`CC-15 bare element selector \`${bare.join(", ")}\` declares geometry (${gm[1]}) at ${file}:${line} — scope it (e.g. \`main ${bare[0].split(":")[0]}\`) so package header controls never inherit app geometry`);
          }
        }
      }
      // CC-11 control height: button-like rules pin to the 34px --h-control box
      // (32px flush inner segments). A height/min-height px literal outside
      // {32, 34} on a control subject is the icon-button drift class (38–40px
      // boxes); var(--h-control) and non-px values pass untouched. A deliberate
      // exception (128px profile tiles) escapes with /* size-exempt */ on the
      // declaration line. Package files own their geometry and are exempt.
      if (tool && controlSubject(sel)) {
        const body = rawBody.replace(/\/\*[\s\S]*?\*\//g, (c) => " ".repeat(c.length));
        const hRe = /(?:^|[^-\w])(min-height|height)\s*:\s*([^;{}]+)/gi;
        let hm;
        while ((hm = hRe.exec(body)) !== null) {
          const declLine = lineAt(content, bodyStart + hm.index + hm[0].indexOf(hm[1]));
          if ((content.split("\n")[declLine - 1] ?? "").includes("size-exempt")) continue;
          for (const pm of hm[2].matchAll(/(\d+(?:\.\d+)?)px/g)) {
            const px = +pm[1];
            if (px !== 34 && px !== 32) {
              errors.push(`CC-11 control height drift at ${file}:${lineBase + declLine} (${sel.trim().replace(/\s+/g, " ")}): ${hm[1]}: ${hm[2].trim()} — controls pin to var(--h-control) (34px outer box; 32px flush inner segments); mark /* size-exempt */ for a deliberate exception`);
            }
          }
        }
      }
      // CC-16: active seg/toggle affordances are a --panel-2 block (radio) or the
      // currentColor block-tint (semantic) — never a fill, never a full-width
      // underline. Selector heuristic in CC-15's style: any .active / aria-state
      // compound (`:not(.active)` guards stripped first). Both findings are
      // errors: the last pre-CC-16 fill debt was migrated in the 2026-07 polish
      // rounds, so a fill active is a regression, and the chromatic underline
      // never had fleet precedent.
      const activeSel =
        /\.active(?![\w-])|\[aria-(?:pressed|current|selected)\b/.test(sel.replace(/:not\([^)]*\)/g, ""));
      if (activeSel && !isPrimaryBtn) {
        const body = rawBody.replace(/\/\*[\s\S]*?\*\//g, (c) => " ".repeat(c.length));
        const fillM = /(?:^|[^-\w])background(?:-color)?\s*:\s*var\(\s*--(accent(?:-hover)?|ok|warn|error|info|user)\s*[,)]/i.exec(body);
        if (fillM) {
          const line = lineBase + lineAt(content, bodyStart + fillM.index);
          errors.push(`CC-16 fill active at ${file}:${line} (${sel.trim()}): background var(--${fillM[1]}) — actives are a --panel-2 block (radio) or currentColor block-tint (semantic), never a fill; migrate this seg group onto the canon anatomy`);
        }
        const underM = /(?:^|[^-\w])(border-bottom(?:-color)?)\s*:[^;]*var\(\s*--(accent(?:-hover|-text)?|ok|warn|error|info|user|viz-[1-5])(?![\w-])/i.exec(body);
        if (underM) {
          const line = lineBase + lineAt(content, bodyStart + underM.index);
          errors.push(`CC-16 full-width underline active at ${file}:${line} (${sel.trim()}): ${underM[1]} carries --${underM[2]} — active states are blocks/tints, never chromatic underlines`);
        }
      }
      // CC-18: .sw-nav chrome/typography is package-owned (no app-level overrides) —
      // a tool rule targeting it may only declare layout accommodations per
      // SWNAV_LAYOUT_EXEMPT; anything else restyles the canon nav and errors.
      if (tool && /\.sw-nav(?![\w-])/.test(sel)) {
        const body = rawBody.replace(/\/\*[\s\S]*?\*\//g, (c) => " ".repeat(c.length));
        const declRe2 = /(?:^|[;\s])([-a-z]+)\s*:/gi;
        let dn;
        while ((dn = declRe2.exec(body)) !== null) {
          const prop = dn[1].toLowerCase();
          if (SWNAV_LAYOUT_EXEMPT.test(prop)) continue;
          const line = lineBase + lineAt(content, bodyStart + dn.index);
          errors.push(`CC-18 app-level .sw-nav override at ${file}:${line} (${sel.trim().replace(/\s+/g, " ")}): ${prop} — sw-nav chrome/typography lives in base.css; only layout accommodations (overflow, flex, margin, width bounds) may be tool-scoped`);
        }
      }
      // CC-16: canon seg/chip chrome is package-owned — a tool rule whose subject is a
      // canon class may only declare layout accommodations per SEG_LAYOUT_EXEMPT.
      // Sole chrome carve-out: `color` on .seg-tint…active hue carriers (the tint
      // derives from currentColor). Descendant rules (.home-toolbar .seg svg) and
      // tool-named seg-ish classes (.seg-readout, .seg-hidden-input) have non-canon
      // subjects and fall to the warn heuristics instead.
      if (tool && segCanonSubject(sel)) {
        const body = rawBody.replace(/\/\*[\s\S]*?\*\//g, (c) => " ".repeat(c.length));
        const declRe3 = /(?:^|[;\s])([-a-z]+)\s*:/gi;
        let sd;
        while ((sd = declRe3.exec(body)) !== null) {
          const prop = sd[1].toLowerCase();
          if (SEG_LAYOUT_EXEMPT.test(prop)) continue;
          if (prop === "color" && segTintCarrier(sel)) continue;
          const line = lineBase + lineAt(content, bodyStart + sd.index);
          errors.push(`CC-16 app-level seg override at ${file}:${line} (${sel.trim().replace(/\s+/g, " ")}): ${prop} — seg/chip chrome and typography live in base.css; only layout accommodations may be tool-scoped (sole carve-out: color on a .seg-tint…active hue carrier)`);
        }
      }
      // CC-18 positive: base.css pins .sw-nav a to .76rem/800 and the active pill to the
      // --panel-2 block + --heading label (location never rides on weight/color alone).
      // Flags render a verdict after the scan, CC-09-style.
      if (!svelte && isBaseCss(file) && /\.sw-nav\s+a\b/.test(sel)) {
        const body = rawBody.replace(/\/\*[\s\S]*?\*\//g, " ");
        if (/font-size\s*:\s*0?\.76rem/.test(body) && /(?:^|[^-\w])font-weight\s*:\s*800(?!\d)/.test(body)) {
          swNavFontDeclared = true;
        }
        if (/\.sw-nav\s+a\.active(?![\w-])/.test(sel) &&
            /(?:^|[^-\w])background(?:-color)?\s*:\s*var\(\s*--panel-2\s*\)/.test(body) &&
            /(?:^|[^-\w])color\s*:\s*var\(\s*--heading\s*\)/.test(body)) {
          swNavActiveDeclared = true;
        }
      }
      // CC-16 positive: base.css ships the seg utilities — .seg-group owns the frame
      // (overflow clip + --r-control container corners) and .seg pins the flush
      // segment (32px, weight 650, radius 0). Verdict rendered after the scan.
      if (!svelte && isBaseCss(file)) {
        const parts = sel.split(",").map((p) => p.trim());
        const body = rawBody.replace(/\/\*[\s\S]*?\*\//g, " ");
        if (parts.includes(".seg-group") &&
            /(?:^|[^-\w])overflow\s*:\s*hidden/.test(body) &&
            /(?:^|[^-\w])border-radius\s*:\s*var\(\s*--r-control\s*\)/.test(body)) {
          segGroupDeclared = true;
        }
        if (parts.includes(".seg") &&
            /(?:^|[^-\w])height\s*:\s*32px/.test(body) &&
            /(?:^|[^-\w])font-weight\s*:\s*650(?!\d)/.test(body) &&
            /(?:^|[^-\w])border-radius\s*:\s*0(?![\w.%])/.test(body)) {
          segBaseDeclared = true;
        }
      }
      // CC-18 ambient-readout weight: un-componentized polled/status readouts recede
      // (≤500, never bold). Warn-level — "readout-ish" is a name heuristic, not a
      // structural fact; componentized names and heading subjects are exempt.
      if (tool) {
        const body = rawBody.replace(/\/\*[\s\S]*?\*\//g, (c) => " ".repeat(c.length));
        const fwM = /(?:^|[^-\w])font-weight\s*:\s*(\d+|bold)\b/i.exec(body);
        if (fwM) {
          const w = fwM[1].toLowerCase() === "bold" ? 700 : +fwM[1];
          if (w > 500 && readoutSubject(sel)) {
            const line = lineBase + lineAt(content, bodyStart + fwM.index);
            warnings.push(`CC-18 ambient readout weight ${w} at ${file}:${line} (${sel.trim().replace(/\s+/g, " ")}) — un-componentized status/metric readouts recede to ≤500; component-owned weights (pills/badges/segs) keep theirs`);
          }
        }
      }
      // CC-18 weight table (warn): control-ish and pill-ish tool rules pin to the
      // canonical weight-per-surface table — buttons/segs/link-out 650, status pills +
      // count badges 700, tag/neutral pills 400 (a pill-ish NAME can't tell status
      // from tag, so both table values pass), readout-named controls recede ≤500.
      // Warn-level: names are heuristics, not structure.
      if (tool) {
        const body = rawBody.replace(/\/\*[\s\S]*?\*\//g, (c) => " ".repeat(c.length));
        const fwT = /(?:^|[^-\w])font-weight\s*:\s*(\d+|bold)\b/i.exec(body);
        if (fwT) {
          const w = fwT[1].toLowerCase() === "bold" ? 700 : +fwT[1];
          const line = lineBase + lineAt(content, bodyStart + fwT.index);
          const selShort = sel.trim().replace(/\s+/g, " ");
          if (pillSubject(sel)) {
            if (w !== 700 && w !== 400) {
              warnings.push(`CC-18 off-table pill weight ${w} at ${file}:${line} (${selShort}) — status pills/count badges are 700, tag/neutral pills 400`);
            }
          } else if (controlSubject(sel)) {
            if (READOUT_RE.test(sel)) {
              if (w > 500) {
                warnings.push(`CC-18 readout seg weight ${w} at ${file}:${line} (${selShort}) — readout segments recede to ≤500`);
              }
            } else if (w !== 650) {
              warnings.push(`CC-18 off-table control weight ${w} at ${file}:${line} (${selShort}) — buttons/segs/link-out pin to 650`);
            }
          }
        }
        // CC-16 look-alike radius (warn): a seg-NAMED tool class that is not a canon
        // subject declaring a nonzero border-radius breaks the flush-segment anatomy —
        // the hand-rolled seg-group drift class.
        if (!segCanonSubject(sel) && segNamedSubject(sel)) {
          const brM = /(?:^|[^-\w])border-radius\s*:\s*([^;{}]+)/i.exec(body);
          if (brM && !/^\s*0(?:px)?\s*$/.test(brM[1])) {
            const line = lineBase + lineAt(content, bodyStart + brM.index);
            warnings.push(`CC-16 per-segment radius at ${file}:${line} (${sel.trim().replace(/\s+/g, " ")}): border-radius: ${brM[1].trim()} — segments are flush (radius 0); only the .seg-group container carries --r-control corners`);
          }
        }
      }
      // CC-11: .btn-* variants belong to base.css only.
      if (!svelte && /\.btn-[\w-]+/.test(sel) && !isBaseCss(file)) {
        const line = lineBase + lineAt(content, match.index);
        for (const m of sel.matchAll(/\.btn-[\w-]+/g)) {
          warnings.push(`CC-11 .btn-* variant ${m[0]} defined outside base.css at ${file}:${line}`);
        }
      }
      // CC-09: the eyebrow dock lives in base.css. Comments become equal-length
      // whitespace so declaration offsets (and thus line numbers) survive the strip.
      if (/\.eyebrow\b/.test(sel)) {
        const body = rawBody.replace(/\/\*[\s\S]*?\*\//g, (c) => " ".repeat(c.length));
        const mm = /(?:^|[^-\w])margin(?:-top|-bottom|-block(?:-start|-end)?)?\s*:\s*[^;}]+/i.exec(body);
        if (!svelte && isBaseCss(file)) {
          if (/\.eyebrow\s*\+\s*h[12]\b/.test(sel)) {
            if (/(?:^|[^-\w])margin(?:-top)?\s*:\s*8px\b/i.test(body)) dockTitleMargin = true;
          } else if (/(?:^|[^-\w])margin\s*:\s*0(?:px)?\s*(?:[;}]|$)/im.test(body)) {
            dockEyebrowZero = true;
          }
        } else if (mm) {
          const line = lineBase + lineAt(content, bodyStart + mm.index);
          errors.push(`CC-09 .eyebrow margin redeclared outside base.css at ${file}:${line}: ${mm[0].trim()} (the eyebrow dock lives in base.css)`);
        }
      }
      // CC-05: --sel-tint only in a rule that also carries the 3px accent inset rail.
      if (/var\(\s*--sel-tint\s*\)/.test(rawBody)) {
        const railed = /box-shadow[^;]*inset[^;]*var\(\s*--accent\s*\)/.test(rawBody);
        if (!railed) {
          const line = lineBase + lineAt(content, bodyStart);
          errors.push(`CC-05 --sel-tint used without the 3px accent inset rail at ${file}:${line} (${sel.trim()})`);
        }
      }

      for (const [i, lineText] of rawBody.split("\n").entries()) {
        const stripped = lineText.replace(/\/\*[\s\S]*?\*\//g, "");
        const line = lineBase + lineAt(content, bodyStart) + i;
        // CC-05: no color-mix() anywhere in rule bodies (runtime accent mixing is banned).
        // Single carve-out (CC-16, 2026-07-18): the canonical block-tint active for
        // semantic multi-toggles — color-mix(in srgb, currentColor 12%, transparent) —
        // is sanctioned outside :hover rules (CC-05 hover never tints).
        const isBlockTint = /color-mix\(\s*in srgb\s*,\s*currentColor 12%\s*,\s*transparent\s*\)/i.test(stripped);
        if (/color-mix\(/i.test(stripped) && !(isBlockTint && !hover)) {
          errors.push(`CC-05 color-mix() is banned at ${file}:${line}: ${lineText.trim()} (sole exception: CC-16 block-tint color-mix(in srgb, currentColor 12%, transparent) outside :hover)`);
        }
        // CC-05: any --accent-wash reference.
        if (/--accent-wash\b/.test(stripped)) {
          errors.push(`CC-05 --accent-wash reference at ${file}:${line}: ${lineText.trim()}`);
        }
        // CC-05: hover background must be a surface move (--panel/--panel-2/transparent);
        // accent backgrounds are legal only on the primary button.
        if (hover) {
          const bm = /(?:^|[^-\w])(background(?:-color)?)\s*:\s*([^;]+)/i.exec(stripped);
          if (bm) {
            const val = bm[2].trim().replace(/\s*!important$/i, "");
            const ok = /^var\(\s*--panel(-2)?\s*\)$/.test(val) || /^transparent$/i.test(val) ||
              (isPrimaryBtn && /^var\(\s*--accent(-hover)?\s*\)$/.test(val)) ||
              (isDangerBtn && /^var\(\s*--error-chip-bg(-hover)?\s*\)$/.test(val));
            if (!ok) {
              errors.push(`CC-05 hover ${bm[1]} is not a sanctioned surface move at ${file}:${line}: ${lineText.trim()} (allowed: --panel, --panel-2, transparent; accent bg only on .btn-primary; danger tint deepen --error-chip-bg-hover only on .btn-danger)`);
            }
          }
        }
        // CC-01: color literal outside a :root token block (applies to CSS and .svelte).
        if (!lineText.includes("chroma-chip") && /#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(/i.test(stripped)) {
          errors.push(`CC-01 color literal outside token blocks at ${file}:${line}: ${lineText.trim()} (use var(--...) or mark /* chroma-chip */)`);
        }
        // CC-08 negative: px width / max-width literals > 600 outside the quantized set.
        // Reads every px literal in the value — inside min()/clamp()/calc() too — which
        // closes the old min() escape. Small widths (≤600px: table cells, truncation) are
        // exempt. Applies to CSS + scoped svelte styles alike. (Media-query preludes never
        // reach here: they live in the block selector, not a declaration body.)
        const wm = /(?:^|[^-\w])(max-width|width)\s*:\s*([^;{}]+)/i.exec(stripped);
        if (wm) {
          for (const pm of wm[2].matchAll(/(\d+)px/g)) {
            const px = +pm[1];
            if (px > 600 && !ALLOWED_WIDTHS.has(px)) {
              errors.push(`CC-08 non-quantized ${wm[1]} ${px}px at ${file}:${line}: ${lineText.trim()} (page-scale widths use var(--w-*), 100%, none, fit-content, or 760/1180/1480/1540; ≤600px exempt)`);
            }
          }
        }
        if (svelte) continue; // .svelte contributes to CC-01/CC-05/CC-08/CC-09 only

        // CC-04: status fill used as text.
        const fm = fillAsTextRe.exec(lineText);
        if (fm) {
          errors.push(`CC-04 status fill used as text at ${file}:${line}: ${lineText.trim()} — ${fm[2]} is a fill; use var(${fm[2]}-text) for prose`);
        }
      }
    }
  }

  for (const file of cssFiles) {
    if (isBaseCss(file)) sawBaseCss = true;
    if (/(^|[\/\\])dataviz\.css$/.test(file)) sawDataviz = true;
    const content = await Bun.file(file).text();
    checkHCompact(content, file, errors);
    scan(content, file, { svelte: false });
  }
  let sawMain = false;
  for (const file of svelteFiles) {
    const content = await Bun.file(file).text();
    checkHCompact(content, file, errors);
    for (const sm of content.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/gi)) {
      const lineBase = lineAt(content, sm.index) - 1;
      scan(sm[1], file, { svelte: true, lineBase });
    }
    // CC-08 positive: the tool's top-level page container is the <main> element — it must
    // carry .page / .page.prose / .page.wide (or size via --w-* tokens).
    for (const mm of content.matchAll(/<main\b[^>]*>/gi)) {
      sawMain = true;
      const tag = mm[0];
      const clsAttr = /class\s*=\s*"([^"]*)"/.exec(tag);
      const classes = clsAttr ? clsAttr[1].split(/\s+/).filter(Boolean) : [];
      const hasPage = classes.includes("page") || /class:page\b/.test(tag) || /--w-[\w-]+/.test(tag);
      if (!hasPage) {
        errors.push(`CC-08 page container <main> lacks a quantized rail at ${file}:${lineAt(content, mm.index)}: ${tag.trim()} (add .page / .page.prose / .page.wide, or size via --w-* tokens)`);
      }
    }
    // CC-11 link-out anatomy + CC-18 glyph placement (markup side). A .link-out anchor
    // is a declared external-destination control: it must wrap its label in a <span>
    // (base.css underlines .link-out > span) and carry the external-link glyph. An
    // anchor with a statically external destination (target="_blank" / obsidian:// /
    // absolute http(s) href) on a control-scope class must BE a .link-out; dynamic
    // hrefs the scan can't classify stay with the human checklist row. Card-scope
    // links (no control class) are outside the clause.
    const anchorRe = /<a\s[^>]*>/gi;
    let am;
    while ((am = anchorRe.exec(content)) !== null) {
      const tag = am[0];
      const aLine = lineAt(content, am.index);
      const aCls = /class\s*=\s*"([^"]*)"/.exec(tag);
      const aClasses = aCls ? aCls[1].split(/\s+/).filter(Boolean) : [];
      const close = content.indexOf("</a>", am.index);
      const inner = close >= 0 ? content.slice(am.index + tag.length, close) : "";
      if (aClasses.includes("link-out")) {
        if (!/<ExternalLink[\s/>]|external-link/.test(inner)) {
          errors.push(`CC-11 .link-out without the external-link glyph at ${file}:${aLine} — the trailing 12px glyph is mandatory on external-destination controls`);
        }
        if (!/<span[\s>]/.test(inner)) {
          errors.push(`CC-11 .link-out label not span-wrapped at ${file}:${aLine} — base.css underlines .link-out > span; a bare text label loses the underline`);
        }
      } else {
        const external = /target\s*=\s*"_blank"/.test(tag) || /href\s*=\s*"(?:https?:\/\/|obsidian:)/.test(tag);
        if (external && aClasses.some((c) => /(?:^|-)(?:btn|button|chip|action|control)(?:-|$)/.test(c))) {
          errors.push(`CC-11 external-destination control link is not a .link-out at ${file}:${aLine}: ${tag.trim().replace(/\s+/g, " ")} — control-scope external links use canon .link-out (underlined span label + 12px glyph)`);
        }
      }
    }
    // CC-18: the external-link glyph is forbidden inside .sw-nav — in-tool nav never
    // carries the external affordance (CC-11 owns the glyph, external destinations only).
    for (const nm of content.matchAll(/<nav\b[^>]*class\s*=\s*"[^"]*\bsw-nav\b[^"]*"[^>]*>/gi)) {
      const close = content.indexOf("</nav>", nm.index);
      const inner = close >= 0 ? content.slice(nm.index + nm[0].length, close) : "";
      if (/<ExternalLink[\s/>]|external-link/.test(inner)) {
        errors.push(`CC-18 external-link glyph inside .sw-nav at ${file}:${lineAt(content, nm.index)} — forbidden on in-tool nav; nav pills are plain text`);
      }
    }
    // CC-17: chart-series color paths read --viz-1..5, never the status family. A
    // runtime token read (getPropertyValue) alongside a quoted status-token literal
    // is the exact borrow CC-17 retired (Token Burn pre-migration). A series that
    // IS the status metric attests the semantic with /* status-series */ on the
    // line. <style> blocks are blanked line-preservingly first — status tokens in
    // scoped CSS are CC-04's business, not this check's.
    if (/getPropertyValue/.test(content)) {
      const script = content.replace(/<style[^>]*>[\s\S]*?<\/style>/gi, (c) => c.replace(/[^\n]/g, " "));
      for (const [idx, lineText] of script.split("\n").entries()) {
        if (/["'`]--(ok|warn|error|info|user)(-text)?["'`]/.test(lineText) && !lineText.includes("verdict-series")) {
          errors.push(`CC-17 status token read in a series color path at ${file}:${idx + 1}: ${lineText.trim()} (series are never verdicts — map --viz-1..5 by index, or mark /* verdict-series */ when the series IS the status metric)`);
        }
      }
    }
    // CC-14 static assists (best-effort; the fleet probes in scripts/flash-probe/ are the
    // authoritative acceptance). (a) polled {#each} must be keyed; (b) {@html} must be
    // guarded on content, not modifiedAt. Warnings — static confidence is low.
    const polled = /setInterval|PollControl|poll/i.test(content);
    if (polled) {
      for (const inner of eachTags(content)) {
        const asIdx = inner.search(/\bas\b/);
        const iterable = (asIdx >= 0 ? inner.slice(0, asIdx) : inner).trim();
        if (iterable.startsWith("[")) continue; // static inline array literal — no key needed
        if (!/\)\s*$/.test(inner.trim())) {
          warnings.push(`CC-14 polled {#each ${iterable.slice(0, 40).replace(/\s+/g, " ")} …} is unkeyed in ${file} — key it {#each … (item.id)} so refresh reconciles without DOM churn (acceptance: scripts/flash-probe/)`);
        }
      }
    }
    if (/\{@html/.test(content)) {
      let flagged = false;
      for (const hm of content.matchAll(/\{@html([^}]*)\}/g)) if (/modifiedAt/.test(hm[1])) flagged = true;
      if (/modifiedAt\s*(===|!==|==|!=|<=|>=|<|>)|(===|!==|==|!=|<=|>=|<|>)\s*[\w.$]*modifiedAt/.test(content)) flagged = true;
      if (flagged) {
        warnings.push(`CC-14 {@html} appears guarded on modifiedAt in ${file} — compare rendered source by content, not mtime, or an mtime-only bump forces a re-render + scroll reset (acceptance: scripts/flash-probe/detail-probe)`);
      }
    }
  }
  if (svelteFiles.length && !sawMain) {
    warnings.push("CC-08 no <main> page container found among svelte inputs (positive rail check skipped)");
  }

  // CC-14 clause 4: the surround-boot inline script must sit in <head> before any stylesheet
  // (and before the module entry that imports the app CSS) or first paint flashes the wrong
  // surround. Statically exact — the marker either leads or it does not.
  for (const file of htmlFiles) {
    const html = await Bun.file(file).text();
    const bootPos = html.indexOf("/* signalworks:boot v1 */");
    if (bootPos < 0) {
      errors.push(`CC-14 surround-boot marker missing in ${file}: inline /* signalworks:boot v1 */ in <head> before any stylesheet (acceptance: scripts/flash-probe/)`);
      continue;
    }
    const sheet = html.search(/<link[^>]+rel\s*=\s*["']stylesheet["']/i);
    const mod = html.search(/<script[^>]+type\s*=\s*["']module["']/i);
    if (sheet >= 0 && bootPos > sheet) {
      errors.push(`CC-14 surround-boot runs after a <link rel="stylesheet"> in ${file} — move the boot script above it (acceptance: scripts/flash-probe/)`);
    }
    if (mod >= 0 && bootPos > mod) {
      errors.push(`CC-14 surround-boot runs after the module entry (imports app CSS) in ${file} — move the boot script above it (acceptance: scripts/flash-probe/)`);
    }
  }

  // --- CC-09 eyebrow dock verdict (WARN when no base.css was among the inputs, so
  // ad-hoc single-file invocations stay low-false-positive; test:theme always passes
  // the package dir and therefore always enforces) ---------------------------------------
  if (sawBaseCss) {
    if (!dockEyebrowZero || !dockTitleMargin) {
      const missing = [
        ...(dockEyebrowZero ? [] : [".eyebrow { margin: 0 }"]),
        ...(dockTitleMargin ? [] : [".eyebrow + h1 { margin-top: 8px }"]),
      ].join(" and ");
      errors.push(`CC-09 eyebrow dock missing in base.css: need ${missing} (type card: docked 8px above the page title)`);
    }
  } else {
    warnings.push("CC-09 eyebrow dock not checked: no base.css among inputs");
  }

  // --- CC-05 .row base-color verdict (button rows don't inherit body color) -------------
  // Gated on base.css presence (like CC-09) so ad-hoc single-file runs stay low-false-
  // positive; test:theme always passes the package dir and therefore always enforces.
  if (sawBaseCss) {
    if (!rowColorDeclared) {
      errors.push(`CC-05 .row base rule missing color: var(--text) — button rows (<button class="row">) don't inherit body color; without it the UA buttontext default renders black at every surround`);
    }
  } else {
    warnings.push("CC-05 .row base color not checked: no base.css among inputs");
  }

  // --- CC-18 sw-nav canon verdict (gated on base.css presence like CC-09/CC-05) ---------
  if (sawBaseCss) {
    if (!swNavFontDeclared) {
      errors.push(`CC-18 base.css missing the sw-nav pill typography: .sw-nav a must declare font-size: 0.76rem and font-weight: 800 (weight-as-affordance — nav targets read pressable)`);
    }
    if (!swNavActiveDeclared) {
      errors.push(`CC-18 base.css missing the sw-nav active block: .sw-nav a.active must carry background: var(--panel-2) + color: var(--heading) (location never rides on weight/color alone)`);
    }
  } else {
    warnings.push("CC-18 sw-nav canon not checked: no base.css among inputs");
  }

  // --- CC-16 seg-utilities verdict (gated on base.css presence like CC-18) --------------
  if (sawBaseCss) {
    if (!segGroupDeclared) {
      errors.push(`CC-16 base.css missing the .seg-group container utility: it must declare overflow: hidden and border-radius: var(--r-control) (the container owns the frame; segments stay flush)`);
    }
    if (!segBaseDeclared) {
      errors.push(`CC-16 base.css missing the .seg segment utility: it must declare height: 32px, font-weight: 650 and border-radius: 0 (flush 32px inner segments inside the 34px --h-control frame)`);
    }
  } else {
    warnings.push("CC-16 seg utilities not checked: no base.css among inputs");
  }

  // --- CC-17 payload verdict (gated like CC-09/CC-05: the full package payload
  // among inputs implies dataviz.css must ride along; ad-hoc runs skip with a
  // warning) -----------------------------------------------------------------------------
  if (sawBaseCss && !sawDataviz) {
    errors.push(`CC-17 tokens/dataviz.css missing from the vendored payload (defines --viz-1..5; check signalworks/install.sh propagation)`);
  } else if (!sawBaseCss && !sawDataviz) {
    warnings.push("CC-17 dataviz series tokens not checked: no package payload among inputs");
  }

  // --- duplicate token definitions (tool CSS must not redefine merged tokens) ----------
  for (const [key, sites] of defSites) {
    if (sites.length > 1 && sites.some((s) => s.tool)) {
      const [family, level, token] = key.split("|");
      const where = sites.map((s) => `${s.file}:${s.line}`).join(", ");
      errors.push(`CC-00 duplicate token definition ${token} (${family}/${level}) across inputs: ${where}`);
    }
  }

  // --- resolve cascades -----------------------------------------------------------------
  // Surfaces + status are accent-independent -> resolve on the orange (default) cascade.
  // Accent tokens are resolved per defined family for CC-10 + accent AA pairs.
  const mixWarn = [];
  const rawOf = (family, level) => {
    const flat = {};
    for (const [tok, e] of Object.entries(store.orange.med)) flat[tok] = e.value;
    if (level !== "med") for (const [tok, e] of Object.entries(store.orange[level])) flat[tok] = e.value;
    if (family === "green") {
      for (const [tok, e] of Object.entries(store.green.med)) flat[tok] = e.value;
      if (level !== "med") for (const [tok, e] of Object.entries(store.green[level])) flat[tok] = e.value;
    }
    return flat;
  };
  const colorsOf = (family, level) => {
    const bag = rawOf(family, level);
    const out = {};
    for (const [tok, val] of Object.entries(bag)) {
      const c = resolveColor(val, bag, mixWarn);
      if (c) out[tok] = { value: val, color: c };
    }
    return out;
  };
  const orange = Object.fromEntries(LEVELS.map((lv) => [lv, colorsOf("orange", lv)]));
  const familiesPresent = ["orange", ...(Object.values(store.green).some((o) => Object.keys(o).length) ? ["green"] : [])];

  // --- required tokens ------------------------------------------------------------------
  for (const t of REQUIRED_TOKENS) {
    if (!orange.med[t]) errors.push(`CC-00 missing required token ${t} (base :root block)`);
  }

  // --- CC-17 series-token discipline (viz tokens are accent-independent; resolved on
  // the orange cascade). Constant on Dark/Med — an explicit dark override may only
  // restate the Med value — and lightness-shifted (darker) on Light, CC-04 discipline.
  if (sawDataviz) {
    const rgbEq = (a, b) =>
      Math.round(a.r) === Math.round(b.r) && Math.round(a.g) === Math.round(b.g) && Math.round(a.b) === Math.round(b.b);
    for (let i = 1; i <= 5; i++) {
      const tok = `--viz-${i}`;
      const med = orange.med[tok], dark = orange.dark[tok], light = orange.light[tok];
      if (!med?.color) {
        errors.push(`CC-17 ${tok} missing from the base :root block (tokens/dataviz.css defines --viz-1..5)`);
        continue;
      }
      if (dark?.color && !rgbEq(dark.color, med.color)) {
        errors.push(`CC-17 ${tok} diverges Dark vs Med: ${dark.value} vs ${med.value} (series colors are constant on Dark/Med)`);
      }
      if (!light?.color || rgbEq(light.color, med.color)) {
        errors.push(`CC-17 ${tok} is not lightness-shifted at [light]: ${light?.value ?? "missing"} (Light carries darkened series variants, CC-04 discipline)`);
      } else if (luminance(light.color) >= luminance(med.color)) {
        errors.push(`CC-17 ${tok} light variant ${light.value} is not darker than Med ${med.value} (the light-surround shift darkens for legibility on paper)`);
      }
    }
  }

  // --- per-level surface + status + AA checks (orange cascade) --------------------------
  for (const lv of LEVELS) {
    const T = orange[lv];
    const col = (t) => T[t]?.color;

    // CC-02: surface stack achromatic ≤3; neutral surrounds ≤8.
    for (const t of V3_SURFACES) {
      const e = T[t]; if (!e) continue;
      const sp = spreadOf(e.color);
      if (sp > V3_ACHROMA_MAX) errors.push(`CC-02 surface ${t} not achromatic at [${lv}]: ${e.value} (RGB spread ${sp} > ${V3_ACHROMA_MAX})`);
    }
    for (const t of V3_NEUTRAL) {
      const e = T[t]; if (!e) continue;
      const sp = spreadOf(e.color);
      if (sp > V3_NEUTRAL_MAX) errors.push(`CC-02 surround ${t} not neutral at [${lv}]: ${e.value} (RGB spread ${sp} > ${V3_NEUTRAL_MAX})`);
    }

    // CC-03: luminance ordering of the surface stack.
    const L = (t) => (col(t) ? luminance(col(t)) : null);
    const order = lv === "light"
      ? [["--inset", "--bg", ">="], ["--bg", "--panel", ">"], ["--panel", "--panel-2", ">"]]
      : [["--panel-2", "--panel", ">"], ["--panel", "--bg", ">"], ["--bg", "--inset", ">"]];
    for (const [hi, lo, op] of order) {
      const a = L(hi), b = L(lo);
      if (a === null || b === null) continue;
      const ok = op === ">=" ? a >= b : a > b;
      if (!ok) errors.push(`CC-03 surface order [${lv}] expects ${hi} ${op} ${lo}, got lum ${a.toFixed(4)} vs ${b.toFixed(4)} (${T[hi].value} vs ${T[lo].value})`);
    }

    // CC-04: status-as-text hue alignment (a -text keeps its fill's hue).
    for (const fill of STATUS_FILLS) {
      const f = col(fill), t = col(`${fill}-text`);
      if (!f || !t) continue;
      const [hf, ht] = [hsl(f), hsl(t)];
      if (hf.s < HUE_MIN_SAT || ht.s < HUE_MIN_SAT) continue;
      const gap = hueGap(hf.h, ht.h);
      if (gap > HUE_TOLERANCE) errors.push(`CC-04 ${fill}-text hue ${ht.h.toFixed(0)} is ${gap.toFixed(0)}deg off ${fill} hue ${hf.h.toFixed(0)} at [${lv}]`);
    }

    // CC-04: chip-bg ≈ composite(hue @5% over --panel); pill hue graphical contrast ≥3.
    const panel = col("--panel"), bg = col("--bg");
    for (const s of ["ok", "warn", "error", "info", "user"]) {
      const hue = col(`--${s}-hue`), chip = col(`--${s}-chip-bg`);
      if (!hue || !panel) continue;
      const overPanel = composite({ ...hue, a: 0.05 }, panel);
      if (chip) {
        const d = Math.hypot(chip.r - overPanel.r, chip.g - overPanel.g, chip.b - overPanel.b);
        if (d > CHIP_TOLERANCE) errors.push(`CC-04 --${s}-chip-bg at [${lv}] is ${d.toFixed(1)} off composite(--${s}-hue @5% over --panel): ${T[`--${s}-chip-bg`].value} vs derived (${overPanel.r.toFixed(0)},${overPanel.g.toFixed(0)},${overPanel.b.toFixed(0)})`);
      }
      // Pill hue against the pill fill: error over --panel, warn over --bg (panel-derived tint).
      const rP = ratio(hue, chip ?? overPanel);
      if (rP < PILL_MIN) errors.push(`CC-04 pill --${s}-hue graphical contrast ${rP.toFixed(2)} < ${PILL_MIN} on its chip over --panel at [${lv}]`);
      if (bg) {
        const overBg = composite({ ...hue, a: 0.05 }, bg);
        const rB = ratio(hue, overBg);
        if (rB < PILL_MIN) warnings.push(`CC-04 pill --${s}-hue graphical contrast ${rB.toFixed(2)} < ${PILL_MIN} composited over --bg at [${lv}] (panel-derived chip reused on --bg; bundle open item)`);
      }
    }

    // Extended AA pairs (accent-independent side).
    const aa = (fg, bgTok, min, label) => {
      const f = col(fg), b = col(bgTok);
      if (!f || !b || f.a < 1 || b.a < 1) return;
      const r = ratio(f, b);
      if (r < min) errors.push(`${label} ${fg} on ${bgTok} ${r.toFixed(2)} < ${min} at [${lv}]`);
    };
    aa("--text", "--bg", 4.5, "AA");
    aa("--text", "--panel", 4.5, "AA");
    aa("--text", "--panel-2", 4.5, "AA");
    aa("--text", "--inset", 4.5, "AA");
    aa("--text-dim", "--bg", 4.5, "AA");
    aa("--text-dim", "--panel", 4.5, "AA");
    aa("--muted", "--bg", 4.5, "AA");
    aa("--muted", "--panel", 4.5, "AA");
    aa("--muted", "--panel-2", 4.5, "AA");
    aa("--heading", "--bg", 3.0, "AA(heading, display-size 3:1)");
    aa("--heading", "--panel", 3.0, "AA(heading, display-size 3:1)");
    // CC-04: status -text at 4.5 on every surface it can land on.
    for (const s of STATUS_FILLS) for (const surf of ["--bg", "--panel", "--panel-2", "--inset"]) aa(`${s}-text`, surf, 4.5, "CC-04");
    // --text-faint placeholder policy: 3:1 on --inset (WCAG-exempt from 4.5).
    aa("--text-faint", "--inset", 3.0, "AA(--text-faint placeholder 3:1)");
  }

  // --- accent-family checks per present family (CC-10 + accent AA pairs) -----------------
  for (const family of familiesPresent) {
    for (const lv of LEVELS) {
      const T = family === "orange" ? orange[lv] : colorsOf("green", lv);
      const col = (t) => T[t]?.color;
      const canon = CANON_ACCENT[family][lv];
      for (const [tok, hex] of Object.entries(canon)) {
        const got = col(tok); const want = parseColor(hex);
        if (!got) { errors.push(`CC-10 ${family} accent token ${tok} missing at [${lv}]`); continue; }
        if (Math.round(got.r) !== want.r || Math.round(got.g) !== want.g || Math.round(got.b) !== want.b) {
          errors.push(`CC-10 ${family} ${tok} at [${lv}] is ${T[tok].value} — canonical family requires ${hex}`);
        }
      }
      const aa = (fg, bgTok, min, label) => {
        const f = col(fg), b = col(bgTok);
        if (!f || !b || f.a < 1 || b.a < 1) return;
        const r = ratio(f, b);
        if (r < min) errors.push(`${label} [${family}] ${fg} on ${bgTok} ${r.toFixed(2)} < ${min} at [${lv}]`);
      };
      // Accent legibility runs through --accent-text (prose/glyphs) and --on-accent (on the
      // fill). The v2 "--accent on --bg 3:1" glyph pair is intentionally dropped in v3: CC-10's
      // constant-shift ships a vivid Light accent (#f07714) that cannot clear 3:1 on paper —
      // the extended AA list replaces it with the --accent-text pairs above.
      aa("--accent-text", "--bg", 4.5, "AA");
      aa("--accent-text", "--panel", 4.5, "AA");
      aa("--accent-text", "--inset", 4.5, "AA");
      aa("--on-accent", "--accent", 4.5, "AA");
    }
  }

  for (const w of mixWarn) warnings.push(`CC-01 ${w}`);

  // --- report ---------------------------------------------------------------------------
  for (const w of warnings) console.error(`WARN ${w}`);
  if (errors.length) {
    for (const e of errors) console.error(`FAIL ${e}`);
    console.error(`\n${errors.length} failure(s), ${warnings.length} warning(s) across ${cssFiles.length} css / ${svelteFiles.length} svelte file(s)`);
    process.exit(1);
  }
  console.log(`AA PASS (signalworks: ${cssFiles.length} css / ${svelteFiles.length} svelte file(s), 3 levels checked, ${warnings.length} warning(s))`);
}

// ======================================================================================
//  entry
// ======================================================================================
const argv = process.argv.slice(2);
const signalworks = argv.includes("--signalworks");
const inputPaths = argv.filter((a) => !a.startsWith("--"));
if (signalworks) await runV3(inputPaths);
else await runV2(inputPaths);
