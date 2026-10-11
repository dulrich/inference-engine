import { expect, test } from "bun:test";
import { lintSource } from "./svelte-template-lint.ts";

const rules = (src: string) => lintSource(src, "x.svelte").map((f) => `${f.line}:${f.col} ${f.rule}`);

test("flags {@html}, keyless {#each} and {@debug} with positions", () => {
  const src = `<script lang="ts">let items: { id: number }[] = []; let h = "";</script>
<p>{@html h}</p>
{#each items as item}<i>{item.id}</i>{/each}
{#each items as item (item.id)}<i>{item.id}</i>{/each}
{@debug items}`;
  expect(rules(src)).toEqual(["2:4 no-at-html-tags", "3:1 require-each-key", "5:1 no-at-debug-tags"]);
});

test("honours the fleet suppression on the element wrapping {@html} (initiative_tracker placements)", () => {
  // Verbatim shapes from initiative_tracker/apps/web/src/InitiativeDetail.svelte:678-679,704-705,729-730.
  const src = `<script lang="ts">let feedback = { content: "" }; let initiativeMdDoc = feedback; let readme = feedback; const renderMarkdown = (s: string) => s;</script>
<div>
        <!-- eslint-disable-next-line svelte/no-at-html-tags -- user's own local file, rendered read-only by design -->
        <article class="markdown">{@html renderMarkdown(feedback.content)}</article>
        <!-- eslint-disable-next-line svelte/no-at-html-tags -- user's own local file, rendered read-only by design -->
        <article class="markdown">{@html renderMarkdown(initiativeMdDoc.content)}</article>
          <!-- eslint-disable-next-line svelte/no-at-html-tags -- user's own local file, rendered read-only by design -->
          <article class="markdown">{@html renderMarkdown(readme.content)}</article>
</div>`;
  expect(rules(src)).toEqual([]);
});

test("a suppression only covers the next line and the named rule", () => {
  const src = `<script lang="ts">let h = ""; let xs: number[] = [];</script>
<!-- eslint-disable-next-line svelte/require-each-key -- static list -->
<p>{@html h}</p>
<!-- eslint-disable-next-line svelte/no-at-html-tags -- trusted -->

<p>{@html h}</p>
<!-- eslint-disable-next-line svelte/require-each-key -- static list -->
{#each xs as x}{x}{/each}`;
  expect(rules(src)).toEqual(["3:4 no-at-html-tags", "6:4 no-at-html-tags"]);
});

test("a suppression without a reason is itself a finding and suppresses nothing", () => {
  const src = `<script lang="ts">let h = "";</script>
<!-- eslint-disable-next-line svelte/no-at-html-tags -->
<p>{@html h}</p>`;
  expect(rules(src)).toEqual(["2:1 suppression-needs-reason", "3:4 no-at-html-tags"]);
});

test("a parse error is reported as a finding, not a crash", () => {
  expect(rules(`<p>{#if x}</p>`)[0]).toMatch(/^1:\d+ parse-error$/);
});
