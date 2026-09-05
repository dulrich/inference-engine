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

/** Sentinel for "the stored column decoded to something other than a JSON object" --
 * malformed JSON or a syntactically valid non-object (`[1,2]`, `"x"`, `7`). Distinct from
 * both `null` (F1: absence is a legitimate, defaulted state) and a real flags object;
 * never collapsed into `{}`, so a caller can refuse it instead of defaulting it. */
export const INVALID_SERVER_FLAGS = Symbol("invalid-server-flags");

/** Three-way decode of the stored `experiments.sweep_server_flags` TEXT column (F1): the
 * column is genuinely `null` (no flags recorded at all -- the all-defaults case), a parsed
 * JSON object, or `INVALID_SERVER_FLAGS` for anything else. Storage decode ONLY -- never
 * applies a recipe-field default itself; `recipeFromServerFlags` owns that. */
export function decodeServerFlags(raw: string | null): Record<string, unknown> | null | typeof INVALID_SERVER_FLAGS {
  if (raw === null) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return INVALID_SERVER_FLAGS;
  }
  return isRecord(parsed) ? parsed : INVALID_SERVER_FLAGS;
}

function isOptionalString(value: unknown): value is string | undefined {
  return value === undefined || typeof value === "string";
}

function isOptionalFiniteInt(value: unknown): value is number | undefined {
  return value === undefined || (typeof value === "number" && Number.isInteger(value));
}

/** The one owner of recipe defaulting/derivation (F1 remedy: "one typed recipe
 * projection"), fed by both `llamaConfig.ts extractRecipe` (command tokens, already
 * `string | null` / `number | null`) and `recipeFromServerFlags` below (stored object,
 * after its own presence/type gate). `-ctk`/`-ctv` absent (`null` in) -> `"f16"`; `mtp` is
 * `specType !== null || draftPath !== null`; `moeCpuLayers` passes through unchanged. */
export function projectRecipe(input: {
  specType: string | null;
  draftPath: string | null;
  moeCpuLayers: number | null;
  kvK: string | null;
  kvV: string | null;
}): { mtp: boolean; specType: string | null; draftPath: string | null; moeCpuLayers: number | null; kvK: string; kvV: string } {
  return {
    mtp: input.specType !== null || input.draftPath !== null,
    specType: input.specType,
    draftPath: input.draftPath,
    moeCpuLayers: input.moeCpuLayers,
    kvK: input.kvK ?? "f16",
    kvV: input.kvV ?? "f16",
  };
}

/** `flags` is the DECODED `sweep_cell.server_flags` object (`decodeServerFlags`'s non-null,
 * non-invalid result), or `null` for the genuinely-absent-column case -- NEVER the raw
 * `unknown`/string column value (F1: that decode is `decodeServerFlags`'s job, not this
 * function's). Returns `null` -- never a guessed default -- when a recipe-bearing key
 * (`-ctk`, `-ctv`, `-ncmoe`, `-md`, `--spec-type`) is PRESENT with the wrong type; an
 * absent key still keeps `projectRecipe`'s default. A `-ncmoe` value that arrives as a JSON
 * number (every real record stores it that way) is read directly; a stringly-typed or
 * non-integer one is wrong-typed, never guessed at. */
export function recipeFromServerFlags(flags: Record<string, unknown> | null): FlagRecipe | null {
  const rawSpecType: unknown = flags === null ? undefined : flags["--spec-type"];
  const rawDraftPath: unknown = flags === null ? undefined : flags["-md"];
  const rawKvK: unknown = flags === null ? undefined : flags["-ctk"];
  const rawKvV: unknown = flags === null ? undefined : flags["-ctv"];
  const rawMoeCpuLayers: unknown = flags === null ? undefined : flags["-ncmoe"];
  if (
    !isOptionalString(rawSpecType) ||
    !isOptionalString(rawDraftPath) ||
    !isOptionalString(rawKvK) ||
    !isOptionalString(rawKvV) ||
    !isOptionalFiniteInt(rawMoeCpuLayers)
  ) {
    return null;
  }
  const projected = projectRecipe({
    specType: rawSpecType ?? null,
    draftPath: rawDraftPath ?? null,
    moeCpuLayers: rawMoeCpuLayers ?? null,
    kvK: rawKvK ?? null,
    kvV: rawKvV ?? null,
  });
  return { mtp: projected.mtp, specType: projected.specType, moeCpuLayers: projected.moeCpuLayers, kvK: projected.kvK, kvV: projected.kvV };
}

/** The served model's recipe, projected from `ServingAlias.recipe` (`llamaConfig.ts`'s
 * `extractRecipe`) into the same five-field equality tuple a record's recipe reduces to.
 * `modelPath` is normalized here too (defensive -- every real `config.yaml` path is
 * already `/models/…`, S2 census, but this keeps both sides of `recipeEquals` running
 * through the identical normalization rather than trusting one side by convention). F1:
 * `null` -- never a manufactured baseline -- when the model has no recipe at all
 * (`alias.recipe === null`) or no `-m` flag (`modelPath === null`); a served model with no
 * recipe of its own must never satisfy `isV1TimingBaselineRecipe` or join by path. */
export function recipeFromServedAlias(alias: ServingAlias): Recipe | null {
  const recipe = alias.recipe;
  if (recipe === null || recipe.modelPath === null) return null;
  const projected = projectRecipe({
    specType: recipe.specType,
    draftPath: recipe.draftPath,
    moeCpuLayers: recipe.moeCpuLayers,
    kvK: recipe.kvK,
    kvV: recipe.kvV,
  });
  return {
    modelPath: normalizeModelPath(recipe.modelPath),
    mtp: projected.mtp,
    moeCpuLayers: projected.moeCpuLayers,
    kvK: projected.kvK,
    kvV: projected.kvV,
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
  sweepServerFlags: string | null;
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
    const alias = servedAliases.find((a) => recipeFromServedAlias(a)?.modelPath === normalizedPath);
    if (alias === undefined) return null;

    const servedRecipe = recipeFromServedAlias(alias);
    if (servedRecipe === null) return null; // no served recipe -> no join (F1)

    const decodedFlags = decodeServerFlags(row.sweepServerFlags);
    if (decodedFlags === INVALID_SERVER_FLAGS) return null; // invalid evidence -> unresolved (F1)
    const flagRecipe = recipeFromServerFlags(decodedFlags);
    if (flagRecipe === null) return null; // present-but-wrong-typed key -> unresolved (F1)

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
    if (servedRecipe === null) return null; // no served recipe -> not the v1 baseline (F1)
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
