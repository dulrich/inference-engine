<script lang="ts">
  // The one uPlot mount/destroy wrapper in this SPA (plan WP-2: "wrap it in one Svelte
  // component") -- every chart view (Long-context's position curve + real-token bins)
  // builds its own `uPlot.Options` via a pure, testable module (`lib/longcontext/*`) and
  // hands it here through `buildOptions`, following `fleet-tools/model_scoreboard`'s
  // own `lib/analysis/TrendChart.svelte` precedent: colors are read from the surrounding
  // document's CSS custom properties AT DRAW TIME (never hardcoded), and a
  // MutationObserver on `data-surround` rebuilds the whole chart on a surround flip --
  // uPlot has no "restyle these series in place" path, so a full rebuild is the correct,
  // simplest response (same call TrendChart makes).
  import uPlot from "uplot";
  import "uplot/dist/uPlot.min.css";

  let {
    buildOptions,
    data,
    ariaLabel,
    height = 220,
  }: {
    /** Receives a CSS custom-property reader plus the mount's current pixel width/height
     * and returns a fresh `uPlot.Options` -- called on every (re)build, so it always sees
     * the surround's current token values. */
    buildOptions: (token: (name: string) => string, width: number, height: number) => uPlot.Options;
    data: uPlot.AlignedData;
    /** Accessible description of what the chart plots -- the mount has no text content of
     * its own (uPlot renders to canvas). */
    ariaLabel: string;
    height?: number;
  } = $props();

  let el: HTMLDivElement | undefined = $state();
  let chart: uPlot | undefined;

  function token(name: string): string {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function build() {
    chart?.destroy();
    chart = undefined;
    if (!el) return;
    const width = el.clientWidth || 640;
    chart = new uPlot(buildOptions(token, width, height), data, el);
  }

  $effect(() => {
    data;
    buildOptions;
    build();
    return () => {
      chart?.destroy();
      chart = undefined;
    };
  });

  // Recolor on surround change (light/med/dark): a full rebuild re-reads every token this
  // chart's own buildOptions call touches.
  $effect(() => {
    const observer = new MutationObserver(() => build());
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-surround"] });
    return () => observer.disconnect();
  });

  // uPlot sizes to an explicit pixel width, so a container resize needs an explicit rebuild.
  $effect(() => {
    const onResize = () => build();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  });
</script>

<div class="uplot-mount" role="img" aria-label={ariaLabel} bind:this={el}></div>

<style>
  .uplot-mount {
    /* uPlot's own canvas/legend chrome inherits ambient type; the panel background shows
       through (no --panel/--bg literal here -- this is layout only). */
    width: 100%;
  }
</style>
