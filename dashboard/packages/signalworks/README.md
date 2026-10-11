# @fleet-tools/signalworks

The fleet's shared design package: Signalworks tokens, the base contract CSS, and the
three shared chrome components (AppHeader, SurroundControl, PollControl). This directory
at the repo root is the **source of truth**; tools consume a **vendored copy** at
`<tool>/packages/signalworks/`, synced by `signalworks/install.sh` and picked up by each
tool's existing Bun workspace globs as a real import.

**Vendored copies are never edited.** Fix things here, then re-sync
(`bash signalworks/install.sh --all`) — `install.sh --verify` hash-checks every
registered consumer against this source (CC-13's byte-identical rule, machine-enforced)
and `signalworks/scripts/check-contrast.mjs --signalworks` (source location; consumers run
the vendored copy at `packages/signalworks/scripts/check-contrast.mjs`) gates the
token/contract rules.

## Provenance

- `css/tokens/*.css` is a **byte-verbatim export** from the Signalworks design bundle
  (Claude Design round-trip, 2026-07). The same files live in
  `skills/signalworks-design/tokens/`; both must stay hash-identical to the bundle.
  Never hand-edit — token changes come back through a design-bundle round trip.
- `css/base.css` and the components implement the bundle's component cards and contract
  rules CC-01…CC-20 (`skills/signalworks-design/brand/rules.html`). The design docs live
  in `references/visual-language.md`, `references/color-critical-surround-theming.md`,
  and `references/polling-refresh-standard.md`.
- Canon v3: `.row`'s base rule now sets `color: var(--text)` (CC-05 — `<button class="row">`
  is intended markup and buttons don't inherit body color, so without it the UA default
  `buttontext` renders black at every surround). `.icon-label` (CC-11, `display: inline-flex;
  align-items: center; gap: 6px`) is the sanctioned pure-layout modifier for a glyph beside a
  visible label, composable with any `.btn-*` — consumers should drop any app-local copy.

- `css/fonts/` self-hosts **Inter v4.1** (InterVariable + Italic, woff2), licensed under
  the SIL Open Font License 1.1 (`css/fonts/OFL.txt`). `css/fonts.css` declares the
  `@font-face` pair (weight `100 900`, `font-display: swap`) so `--font-ui` resolves to
  the real brand font on LAN deployments — no external font network, no build step.

## Layout

```
package.json          exports: "." (uncompiled svelte src), "./css", "./css/*"
css/signalworks.css   fonts + tokens + base, in order — the one import tools need
css/fonts.css         @font-face for self-hosted Inter (css/fonts/, OFL 1.1)
css/tokens/*.css      verbatim bundle tokens (surrounds, accents, status, layout)
css/base.css          fleet contract surface: type, buttons, inputs, pills, rows, .page
src/                  AppHeader, SurroundControl, PollControl, createPoller,
                      createLiveSource, index.ts
boot/surround-boot.js no-flash surround snippet, inlined into each tool's index.html
```

The package ships **uncompiled** `.svelte`/`.ts`; the consumer's owned `apps/web/build.ts`
(`Bun.build` + `svelte/compiler`) compiles it.
peerDependencies: `svelte ^5`, `lucide-svelte`.

## Consuming (per tool)

1. `bash <repo-root>/signalworks/install.sh --dest .` (and register the tool in
   `signalworks/consumers.txt`).
2. Web app dependency: `"@fleet-tools/signalworks": "workspace:*"`, **then**
   `bun install` — Bun only materializes the workspace symlink once the dep is listed;
   installing first leaves typecheck failing with "Cannot find module".
3. Lint: add `packages/signalworks` to the tool's `.oxlintrc.json` `ignorePatterns` and
   `biome.json` `files.includes` (as `!packages/signalworks`) — the vendored copy is never
   hand-edited, and its Svelte 4 `$:` syntax trips strict `no-undef`.
4. `main.ts` — import the CSS **before** the tool's own `app.css`:
   ```ts
   import "@fleet-tools/signalworks/css";
   import "./app.css";
   ```
5. `index.html` — inline `boot/surround-boot.js` in `<head>` (before any stylesheet),
   setting `KEY` to `"<app>.surround.v1"` and any pre-rename keys in `LEGACY_KEYS`.
   Keep the `/* signalworks:boot v1 */ … /* signalworks:boot end */` markers.
6. Docker: the image build needs the workspace manifest before `bun install`:
   ```dockerfile
   COPY packages/signalworks/package.json packages/signalworks/
   ```
7. Gates: `test:theme` runs
   `bun packages/signalworks/scripts/check-contrast.mjs --signalworks packages/signalworks/css apps/web/src`;
   `design:verify` runs `bash <repo-root>/signalworks/install.sh --verify --dest .`.

## Components

```svelte
<AppHeader tool="Subscription Tracker" icon={CreditCard} appId="subscription-tracker"
           legacySurroundKeys={["subscription_tracker.surround.v1"]}>
  <nav class="sw-nav" slot="nav"><a href="#a" class="active">Accounts</a></nav>
  <PollControl slot="poll" storageKey="subscription-tracker.poll.v1" defaultMs={0}
               onTick={refresh} busy={loading} />
</AppHeader>
```

- **AppHeader** `{tool, product="Context Control", icon?, glyph?, appId,
  legacySurroundKeys=[], homeHref?}` — CC-07 chrome. `icon` (a lucide component) is the
  normal brand mark and must match the tool's Command Center card + favicon (the
  tool-glyph convention); `glyph` (serif text) is the exception; `icon` wins when both
  are given. `homeHref` defaults to `VITE_COMMAND_CENTER_URL`, else
  `location`-derived `:4730`; pass `null` to suppress the home link. Slots: `nav`
  (use `<nav class="sw-nav">` for styled links), `poll`.
- **SurroundControl** `{appId, legacyKeys=[]}` — CC-06 Moon/Contrast/Sun; key
  `${appId}.surround.v1`; migrates then removes `legacyKeys`. Rendered by AppHeader —
  only mount directly on headerless surfaces.
- **PollControl** `{storageKey, onTick, defaultMs, onRefresh?, options?, paused?,
  busy?, onIntervalChange?, liveSource?}` — CC-19 fused group, `defaultMs` required.
  Options `[0, 10000, 30000, 60000]` = Manual / Auto 10s / Auto 30s / Auto 1m. A stored
  value that parses but isn't sanctioned (e.g. a legacy `5000`) is **coerced to the
  nearest option — ties round up (5000 → 10000) — and the coerced value is persisted back
  to storage immediately**; it never silently falls back to `defaultMs`. Only an
  unparseable/absent stored value uses `defaultMs`. Countdown ticks in auto mode and
  freezes when `paused` (pause ≠ Manual). The accent sweep runs **once per poll fire**
  — auto and click; click refreshes and restarts the interval.
  `liveSource={{ url }}` opts the surface into **live (SSE) mode**: a `Live` entry joins
  the same select, the interval timer suspends while the stream is connected, and the
  countdown square becomes the connection affordance (accent dot = connected, muted dot =
  connecting, hollow `--warn-text` ring = degraded). Omit the prop and nothing changes —
  no Live option, no `EventSource`. The chosen interval is untouched and is what the
  control falls back to; `onIntervalChange` does not fire when toggling Live.
- **createPoller / DEFAULT_POLL_OPTIONS** — the underlying visibility-gated poller,
  with `nextTickAt()` and `restart()` (no immediate tick). Numeric end to end;
  live mode does not touch it.
- **createLiveSource / LIVE_FALLBACK_GRACE_MS** — the live transport controller that
  sits beside the poller. Named SSE events only: `version` (monotonic int or opaque
  string) fires **one refresh per changed value**, `heartbeat` is keepalive and never
  refreshes, anything else is ignored. Reconnect has one owner — the browser's native
  `EventSource`, tuned by the server's `retry:` field; the package adds no backoff loop.
  Not OPEN for more than the 10s grace ⇒ state `degraded` and the numeric poller resumes
  at the user's stored interval; on `open`, one immediate refresh and polling suspends.
  Enabled state persists at `<storageKey>.live` (`"1"`/absent), separate from the
  interval key. `paused`/`document.hidden` close the source exactly as they stop the
  timer. `{eventSourceFactory, clock, doc, storage}` are injectable seams so the state
  machine is unit-testable with fakes. Endpoint contract + the canonical hono shape:
  `references/polling-refresh-standard.md` § Live mode.
