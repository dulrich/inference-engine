<script lang="ts">
  // Moon / Contrast / Sun segmented control, docked in the header (CC-06) —
  // never a floating overlay. Drives data-surround on <html>; the no-flash boot
  // (boot/surround-boot.js) resolves stored → prefers-color-scheme → Med before
  // first paint, so this component only reads and writes.
  import { onMount } from "svelte";

  type Surround = "dark" | "med" | "light";

  /** App id; the storage key is `${appId}.surround.v1`. */
  export let appId: string;
  /** Pre-rename storage keys: migrated into the current key on mount, then removed. */
  export let legacyKeys: readonly string[] = [];

  $: key = `${appId}.surround.v1`;

  let current: Surround =
    typeof document !== "undefined" &&
    (["dark", "med", "light"] as const).includes(
      document.documentElement.dataset.surround as Surround,
    )
      ? (document.documentElement.dataset.surround as Surround)
      : "med";

  function set(s: Surround) {
    current = s;
    document.documentElement.dataset.surround = s;
    try {
      localStorage.setItem(key, s);
      for (const lk of legacyKeys) localStorage.removeItem(lk);
    } catch {
      // Storage unavailable: the choice still applies for this page view.
    }
  }

  onMount(() => {
    try {
      if (localStorage.getItem(key) === null) {
        for (const lk of legacyKeys) {
          const v = localStorage.getItem(lk);
          if (v === "dark" || v === "med" || v === "light") {
            localStorage.setItem(key, v);
            break;
          }
        }
      }
      for (const lk of legacyKeys) localStorage.removeItem(lk);
    } catch {
      // Storage unavailable: nothing to migrate.
    }
  });
</script>

<!-- Inline lucide shapes rather than lucide-svelte components: the active
     segment needs the *filled* glyph variant (fill:currentColor) per CC-06,
     and Contrast always renders its half-fill. -->
<span class="surround" role="group" aria-label="Surround">
  <button
    type="button"
    class="seg"
    class:active={current === "dark"}
    aria-pressed={current === "dark"}
    title="Dark"
    on:click={() => set("dark")}
  >
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill={current === "dark" ? "currentColor" : "none"}
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z" />
    </svg>
  </button>
  <button
    type="button"
    class="seg"
    class:active={current === "med"}
    aria-pressed={current === "med"}
    title="Med"
    on:click={() => set("med")}
  >
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="10" />
      <path d="M12 18a6 6 0 0 0 0-12v12z" fill="currentColor" stroke="none" />
    </svg>
  </button>
  <button
    type="button"
    class="seg"
    class:active={current === "light"}
    aria-pressed={current === "light"}
    title="Light"
    on:click={() => set("light")}
  >
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="4" fill={current === "light" ? "currentColor" : "none"} />
      <path
        d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"
      />
    </svg>
  </button>
</span>

<style>
  /* Frame on --panel; flush segments, square corners, NO inner separators —
     only the container corners round (CC-06). */
  .surround {
    display: inline-flex;
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: var(--r-control);
    background: var(--panel);
  }

  /* Geometry is declared exhaustively (incl. min-* and radius) so consumer-side
     bare element selectors (e.g. `button { border-radius; min-height }`) can
     never fill an undeclared property — scoped rules only win where they
     actually declare. */
  .seg {
    display: grid;
    place-items: center;
    width: 32px;
    height: 32px;
    min-width: 32px;
    min-height: 32px;
    padding: 0;
    border: 0;
    border-radius: 0;
    background: transparent;
    color: var(--muted);
    cursor: pointer;
  }

  /* Active = --panel-2 block + filled glyph in --accent-text. Never the accent
     as a background, never --inset. */
  .seg.active {
    background: var(--panel-2);
    color: var(--accent-text);
  }

  .seg:focus-visible {
    outline: none;
    box-shadow: inset 0 0 0 2px var(--focus-border);
  }
</style>
