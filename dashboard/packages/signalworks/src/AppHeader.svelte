<script lang="ts">
  // Every tool ships this header (CC-07): sticky, 72px, --header-bg with 16px
  // backdrop blur, bottom --line. Left: brand mark + product-up/tool-down.
  // Right, in order: nav slot · poll slot · SurroundControl · home link.
  // Content sits on the 1480 rail (--w-rail).
  import { House } from "lucide-svelte";
  import type { ComponentType } from "svelte";
  import SurroundControl from "./SurroundControl.svelte";

  /** Tool name — the small-caps lower line (e.g. "Subscription Tracker"). */
  export let tool: string;
  /** Product name — the upper line. */
  export let product = "Context Control";
  /**
   * Brand mark, preferably a lucide component matching the tool's Command Center
   * card and favicon (the fleet tool-glyph convention in references/visual-language.md).
   */
  export let icon: ComponentType | undefined = undefined;
  /** Serif text glyph — the exception, used only when no lucide icon fits. */
  export let glyph: string | undefined = undefined;
  /** App id for the surround store; key is `${appId}.surround.v1`. */
  export let appId: string;
  /** Pre-rename surround keys, migrated then removed by SurroundControl. */
  export let legacySurroundKeys: readonly string[] = [];
  /**
   * Command Center link. Default: VITE_COMMAND_CENTER_URL, else the current host
   * on :4730. Pass null to suppress the link entirely.
   */
  export let homeHref: string | null | undefined = undefined;

  function defaultHomeHref(): string {
    const env = (import.meta as { env?: Record<string, string | undefined> }).env;
    const fromEnv = env?.VITE_COMMAND_CENTER_URL;
    if (fromEnv) return fromEnv;
    return `${location.protocol}//${location.hostname}:4730/`;
  }

  $: resolvedHomeHref = homeHref === undefined ? defaultHomeHref() : homeHref;
</script>

<header class="sw-header">
  <span class="brand">
    <span class="mark" class:serif={!icon}>
      {#if icon}
        <svelte:component this={icon} size={18} aria-hidden="true" />
      {:else}
        {glyph}
      {/if}
    </span>
    <span class="names">
      <strong>{product}</strong>
      <small>{tool}</small>
    </span>
  </span>
  <span class="controls">
    <slot name="nav" />
    <slot name="poll" />
    <SurroundControl {appId} legacyKeys={legacySurroundKeys} />
    {#if resolvedHomeHref !== null}
      <a class="home" href={resolvedHomeHref} title="Command Center" aria-label="Command Center">
        <House size={14} aria-hidden="true" />
      </a>
    {/if}
  </span>
</header>

<style>
  .sw-header {
    position: sticky;
    top: 0;
    z-index: 50;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    height: 72px;
    padding: 0 max(24px, calc((100vw - var(--w-rail)) / 2));
    background: var(--header-bg);
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
    border-bottom: 1px solid var(--line);
  }

  .brand {
    display: flex;
    align-items: center;
    gap: 11px;
    min-width: 0;
  }

  /* 37px circle, border tinted toward the accent, glyph in --accent-text. */
  .mark {
    display: grid;
    place-items: center;
    width: 37px;
    height: 37px;
    border-radius: 50%;
    border: 1px solid var(--focus-border);
    color: var(--accent-text);
    flex: none;
  }

  .mark.serif {
    font: 700 20px var(--font-display);
  }

  .names {
    display: flex;
    flex-direction: column;
  }

  .names strong {
    font-size: 0.86rem;
    color: var(--heading);
  }

  .names small {
    font-size: 0.62rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--muted);
    margin-top: 2px;
  }

  .controls {
    display: flex;
    align-items: center;
    gap: 12px;
    flex: none;
  }

  /* 34×34 = the shared 32px control height plus its 1px frame all round,
     flush with the other header controls' 34px line. */
  /* Geometry declared exhaustively (incl. min-* and padding) so consumer-side
     bare `a` rules can never fill an undeclared property. */
  .home {
    display: grid;
    place-items: center;
    width: 34px;
    height: 34px;
    min-width: 34px;
    min-height: 34px;
    padding: 0;
    border: 1px solid var(--line);
    border-radius: var(--r-control);
    background: var(--panel-2);
    color: var(--text);
  }

  .home:focus-visible {
    outline: none;
    box-shadow: 0 0 0 3px var(--focus-shadow);
  }

  /* Narrow-header wrap (CC-20). Breakpoint is one fleet value, 640px, kept a
     literal — media queries can't read custom properties. Canonical definition
     + rationale live in signalworks/css/base.css above the .sw-nav rule;
     mirrored verbatim here for the same breakpoint. Two rows — brand then
     controls — height: auto, 34px control heights unchanged (component-owned),
     16px gutters. .controls loses its flex: none so the nav slot and the
     select inside PollControl can shrink; the fixed-size squares (countdown,
     refresh, SurroundControl, home) hold their own flex: none/min-width and
     never shrink (existing shrink-order contract in PollControl.svelte). */
  @media (max-width: 640px) {
    .sw-header {
      flex-wrap: wrap;
      height: auto;
      padding: 12px 16px;
    }

    .brand,
    .controls {
      flex: 1 1 100%;
    }

    /* The controls row holds the nav slot plus the control squares; below the
       breakpoint it wraps its blocks so the row can never widen the document
       (the CC-20 acceptance: no horizontal document scroll at 320px). A nav
       wider than the viewport stays on its own wrapped line and scrolls via
       base.css's `.sw-nav { overflow-x: auto }` instead of growing the row. */
    .controls {
      flex-wrap: wrap;
      row-gap: 8px;
    }

    .controls :global(.sw-nav) {
      max-width: 100%;
    }

    .names {
      min-width: 0;
      overflow: hidden;
    }

    .names strong,
    .names small {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
  }
</style>
