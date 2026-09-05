import type { ServingAlias } from "@bench-dashboard/shared";

// Served-recipe resolver (WP-1, `plans/served-recipe-p50-prefill-and-decode.md`) -- the
// record-side half of the join whose config-side half is `llamaConfig.ts`'s `extractRecipe`.
// Pure and synchronous throughout: every function here reads only its arguments, never a
// file or the DB directly (`db/experiments.ts` is the sole caller that reaches SQLite, and
// it passes already-fetched rows/aliases in) -- "the resolver is pure and reads only the
// reconciled columns" (brief invariant).
//
// Decisions locked: "Recipe = artifact + MTP + expert placement + KV types." A record's
// recipe is `{modelPath, mtp: has(--spec-type) || has(-md), moeCpuLayers: -ncmoe ?? null,
// kvK: -ctk ?? "f16", kvV: -ctv ?? "f16"}`; the served recipe is the same tuple projected
// from the model's `cmd`. Context length and `-fa` are deliberately NOT part of the tuple
// (the user's rev-1 decision that losing the context axis is an improvement stands).

/** A record's or a served model's recipe, reduced to the five-field equality tuple the
 * plan pins ("context length and -fa deliberately excluded"). `modelPath` is always
 * ALREADY NORMALIZED on both sides by the time this type is populated -- `recipeEquals`
 * never re-normalizes. */
export interface Recipe {
  modelPath: string;
  mtp: boolean;
  moeCpuLayers: number | null;
  kvK: string;
  kvV: string;
}

/** The flags-derived half of a record's recipe (everything except `modelPath`, which comes
 * from the record's own separate `model_host_path` column, not from `sweep_server_flags`
 * -- confirmed against the real corpus, S3/S2: `server_flags` never carries `-m`). Carries
 * `specType` alongside `mtp` -- NOT part of `recipeEquals`'s tuple, but needed verbatim by
 * `resolveServedModel`'s return value (and, downstream, WP-3's export) as the MEASUREMENT's
 * own spec-type text, distinct from the served config's. */
export interface FlagRecipe {
  mtp: boolean;
  specType: string | null;
  moeCpuLayers: number | null;
  kvK: string;
  kvV: string;
}

const STALE_MODEL_PREFIX = "/srv/models/";
const CURRENT_MODEL_PREFIX = "/models/";

/** `/srv/models/…` -> `/models/…`, the ONLY rewrite this function performs (S2:
 * `/srv/models` has been a symlink to `/models` since `plans/
 * models-partition-migration.md`, 2026-08-31; pre-migration records carry the stale
 * prefix, `config.yaml` always carries the current one, and records are evidence, never
 * rewritten -- so the join normalizes at read time instead). A path that doesn't carry the
 * stale prefix (every post-migration record, and every `config.yaml` path already) passes
 * through unchanged -- never a guess at what "the rest" of a differently-shaped path
 * would normalize to. */
export function normalizeModelPath(p: string): string {
  return p.startsWith(STALE_MODEL_PREFIX) ? CURRENT_MODEL_PREFIX + p.slice(STALE_MODEL_PREFIX.length) : p;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** `row.sweep_server_flags` arrives here in whatever shape its caller already has it in --
 * the SQLite column is TEXT (a JSON string, `db/experiments.ts replaceExperiment`), while a
 * test fixture may pass the plain object directly. Lenient over `null`/string/object rather
 * than pushing a `JSON.parse` onto every caller; a string that fails to parse, or a parsed
 * value that isn't an object, resolves to `{}` -- the same "nothing found" outcome as if
 * the record carried no flags at all, never a thrown error from a malformed stored value. */
function parseFlags(raw: unknown): Record<string, unknown> {
  if (raw === null || raw === undefined) return {};
  if (typeof raw === "string") {
    try {
      const parsed: unknown = JSON.parse(raw);
      return isRecord(parsed) ? parsed : {};
    } catch {
      return {};
    }
  }
  return isRecord(raw) ? raw : {};
}

/** `flagsRaw` is the stored `sweep_cell.server_flags` object (keys like `-ctk`, `-ctv`,
 * `-ncmoe`, `-md`, `--spec-type`, verbatim from `record.ts`'s `sweepServerFlags` -- never
 * value-mutated on the way in). Absent-flag defaults mirror `extractRecipe`'s config-side
 * posture exactly: `-ctk`/`-ctv` absent -> `"f16"`; `-ncmoe` absent -> `null`; `-md`/
 * `--spec-type` absent -> `null`. A `-ncmoe` value that arrives as a JSON number (as every
 * real record stores it) is read directly; a stringly-typed one is never guessed at. */
export function recipeFromServerFlags(flagsRaw: unknown): FlagRecipe {
  const flags = parseFlags(flagsRaw);
  const specType = typeof flags["--spec-type"] === "string" ? (flags["--spec-type"] as string) : null;
  const draftPath = typeof flags["-md"] === "string" ? (flags["-md"] as string) : null;
  const moeCpuLayers = typeof flags["-ncmoe"] === "number" ? (flags["-ncmoe"] as number) : null;
  const kvK = typeof flags["-ctk"] === "string" ? (flags["-ctk"] as string) : "f16";
  const kvV = typeof flags["-ctv"] === "string" ? (flags["-ctv"] as string) : "f16";
  return {
    mtp: specType !== null || draftPath !== null,
    specType,
    moeCpuLayers,
    kvK,
    kvV,
  };
}

/** The served model's recipe, projected from `ServingAlias.recipe` (`llamaConfig.ts`'s
 * `extractRecipe`) into the same five-field equality tuple a record's recipe reduces to.
 * `modelPath` is normalized here too (defensive -- every real `config.yaml` path is
 * already `/models/…`, S2 census, but this keeps both sides of `recipeEquals` running
 * through the identical normalization rather than trusting one side by convention). A
 * served model with no `-m` flag at all (shouldn't happen for anything with `aliases:`,
 * but `extractRecipe` types it nullable) reduces to the empty string, matching nothing. */
export function recipeFromServedAlias(alias: ServingAlias): Recipe {
  const recipe = alias.recipe;
  const modelPath = recipe === null ? "" : normalizeModelPath(recipe.modelPath ?? "");
  const mtp = recipe !== null && (recipe.specType !== null || recipe.draftPath !== null);
  return {
    modelPath,
    mtp,
    moeCpuLayers: recipe === null ? null : recipe.moeCpuLayers,
    kvK: recipe === null ? "f16" : recipe.kvK,
    kvV: recipe === null ? "f16" : recipe.kvV,
  };
}

/** Recipe identity over the plan's pinned five-field tuple ONLY -- context length and
 * `-fa` are never compared (Decisions locked). Both sides must already carry a normalized
 * `modelPath` (`normalizeModelPath`/`recipeFromServedAlias` both guarantee this) --
 * `recipeEquals` itself never re-normalizes, so a caller building a `Recipe` by hand is
 * responsible for normalizing first. */
export function recipeEquals(a: Recipe, b: Recipe): boolean {
  return a.modelPath === b.modelPath && a.mtp === b.mtp && a.moeCpuLayers === b.moeCpuLayers && a.kvK === b.kvK && a.kvV === b.kvV;
}

/** The trailing suffix `readLegacyTimingRecord`'s dir-derived record ids commonly carry
 * (`gemma-4-12b-qat-full`) -- folded in from `db/experiments.ts`'s pre-WP-1
 * `servingTimingAlias`/`LEGACY_TIMING_ALIAS_SUFFIX` (WP-1 Key Changes: "fold into the v1
 * branch of the resolver"). Never applied to a `v2` row -- `resolveServedModel` only
 * reaches this helper from its `v1-timing` branch. */
const LEGACY_TIMING_ALIAS_SUFFIX = "-full";

function stripLegacyTimingSuffix(alias: string): string {
  return alias.endsWith(LEGACY_TIMING_ALIAS_SUFFIX) ? alias.slice(0, -LEGACY_TIMING_ALIAS_SUFFIX.length) : alias;
}

/** The exact recipe the pre-v2 timing harness could ever have measured -- it drove the
 * live router directly with no flag record of its own (Decisions locked: "v1-timing rows
 * carry no path and no flags"). A `v1-timing` row is admitted only when the SERVED recipe
 * (today, at query time) still equals this baseline; if the served model has since gained
 * MTP/offload/quantized-KV (gemma, post `mtp-serving-rollout`), the legacy row is excluded
 * outright -- never surfaced with a mismatched `recipeMatch: false`, because there is no
 * measured recipe of its own to report as having mismatched. */
function isV1TimingBaselineRecipe(recipe: Recipe): boolean {
  return !recipe.mtp && recipe.moeCpuLayers === null && recipe.kvK === "f16" && recipe.kvV === "f16";
}

/** The subset of a stored `experiments` row `resolveServedModel` needs -- named after the
 * reconciled columns it reads (`model_host_path`, `sweep_alias`, `schema_origin`,
 * `sweep_server_flags`), never `raw_config_json` (brief invariant: "reads only the
 * reconciled columns — never `raw_config_json` at request time"). */
export interface ResolvableTimingRow {
  modelHostPath: string | null;
  sweepAlias: string | null;
  schemaOrigin: string;
  sweepServerFlags: unknown;
}

export interface ResolvedServedModel {
  modelKey: string;
  aliases: string[];
  recipeMatch: boolean;
  mtp: boolean;
  moeCpuLayers: number | null;
  specType: string | null;
}

/** Resolution order (Decisions locked, "Join key: GGUF path, in the dashboard, at query
 * time"): (1) `row.modelHostPath` present -> normalized full-path join against every
 * served model's own normalized `recipe.modelPath` (basename alone is ambiguous -- S2:
 * `Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf` exists under both the MTP and the plain GGUF repo).
 * (2) else `row.schemaOrigin === "v1-timing"` -> alias-join (the pre-v2 harness's own
 * `config.alias`, stripped of any legacy `-full` directory-name suffix) admitted only
 * against a served model whose CURRENT recipe is the v1-baseline shape. (3) else `null` --
 * nothing to join on (a v2 record with no `model_host_path`, e.g. a pre-campaign probe or
 * a full-CLI record with no `sweep_cell` at all). */
export function resolveServedModel(row: ResolvableTimingRow, servedAliases: readonly ServingAlias[]): ResolvedServedModel | null {
  if (row.modelHostPath !== null) {
    const normalizedPath = normalizeModelPath(row.modelHostPath);
    const alias = servedAliases.find((a) => recipeFromServedAlias(a).modelPath === normalizedPath);
    if (alias === undefined) return null;

    const servedRecipe = recipeFromServedAlias(alias);
    const flagRecipe = recipeFromServerFlags(row.sweepServerFlags);
    const rowRecipe: Recipe = {
      modelPath: normalizedPath,
      mtp: flagRecipe.mtp,
      moeCpuLayers: flagRecipe.moeCpuLayers,
      kvK: flagRecipe.kvK,
      kvV: flagRecipe.kvV,
    };
    return {
      modelKey: alias.modelKey,
      aliases: alias.aliases,
      recipeMatch: recipeEquals(rowRecipe, servedRecipe),
      mtp: flagRecipe.mtp,
      moeCpuLayers: flagRecipe.moeCpuLayers,
      specType: flagRecipe.specType,
    };
  }

  if (row.schemaOrigin === "v1-timing") {
    if (row.sweepAlias === null) return null;
    const joinedAlias = stripLegacyTimingSuffix(row.sweepAlias);
    const alias = servedAliases.find((a) => a.aliases.includes(joinedAlias));
    if (alias === undefined) return null;

    const servedRecipe = recipeFromServedAlias(alias);
    if (!isV1TimingBaselineRecipe(servedRecipe)) return null;

    return {
      modelKey: alias.modelKey,
      aliases: alias.aliases,
      recipeMatch: true,
      mtp: false,
      moeCpuLayers: null,
      specType: null,
    };
  }

  return null;
}
