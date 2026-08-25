import type { IngestSummary } from "@bench-dashboard/shared";
import { db } from "../db/connection.ts";
import { clearAllExperiments, replaceExperiment } from "../db/experiments.ts";
import { listRecordDirs, readLegacyTimingRecord, readRecordDir } from "./record.ts";

export interface RunIngestOptions {
  recordsDir: string;
  /** Drops every row this ingest owns, then replays every v2 record from scratch — the
   * brief's `--rebuild` flag. Without it, ingest is a normal idempotent upsert pass. */
  rebuild: boolean;
  /** Injectable logging seam so the CLI can print per-record detail while tests stay
   * quiet — defaults to a no-op, never `console.log` directly. */
  onRecord?: (event: { dir: string; kind: "v2" | "v1" | "v1-timing" | "invalid"; reason?: string }) => void;
}

/** Sets the WP-4 (`plans/bakeoff-timing-records-and-estimates.md`) provenance marker on a
 * just-ingested legacy-timing experiment row. `replaceExperiment` (`db/experiments.ts`,
 * owned by WP-3, out of this WP's file scope) writes every row through an `INSERT` that
 * predates `schema_origin` and does not name it, so it always lands on that column's DDL
 * `DEFAULT` (`'v2'`, `db/schema.ts`) regardless of the record's actual provenance — this
 * one explicit follow-up `UPDATE`, scoped to this ingest module and never called for a real
 * v2 record, is the sole place `'v1-timing'` is ever written. Two-phase, not atomic with
 * the insert (a crash between the two would leave the row misreporting `'v2'`), but ingest
 * is idempotent — a re-run always corrects it, same as any other ingest interruption. */
function markLegacyTimingProvenance(experimentId: string): void {
  db.query(`UPDATE experiments SET schema_origin = ? WHERE id = ?`).run("v1-timing", experimentId);
}

/** Scans `recordsDir` (one level deep — `runs/bakeoff/<experiment-id>/`), classifies each
 * subdirectory (`ingest/record.ts readRecordDir`), and upserts every v2 record. v1
 * records (no `schema_version` key, or any value other than `2`) are skipped with a
 * logged count (frozen input contract E1) — never an error, never attempted — UNLESS the
 * allowlisted WP-4 timing-only side door applies: a v1 dir whose `samples.jsonl` contains
 * `timing_round` rows (`ingest/record.ts readLegacyTimingRecord`) has those rows (and only
 * those) ingested under explicit `'v1-timing'` provenance. This is a strict allowlist, not
 * a relaxation of the v2 gate — a v1 dir without `timing_round` rows is still fully
 * skipped, counted in `skippedV1` exactly as before. */
export async function runIngest(options: RunIngestOptions): Promise<IngestSummary> {
  if (options.rebuild) clearAllExperiments();

  const dirs = await listRecordDirs(options.recordsDir);
  let ingested = 0;
  let skippedV1 = 0;
  let skippedInvalid = 0;
  let legacyTimingIngested = 0;

  for (const dir of dirs) {
    const result = await readRecordDir(dir);
    if (result.kind === "v2") {
      replaceExperiment(result.record);
      ingested += 1;
      options.onRecord?.({ dir, kind: "v2" });
    } else if (result.kind === "v1") {
      const legacy = await readLegacyTimingRecord(dir);
      if (legacy.kind === "v1-timing") {
        replaceExperiment(legacy.record);
        markLegacyTimingProvenance(legacy.record.id);
        legacyTimingIngested += 1;
        options.onRecord?.({ dir, kind: "v1-timing" });
      } else if (legacy.kind === "v1") {
        skippedV1 += 1;
        options.onRecord?.({ dir, kind: "v1" });
      } else {
        skippedInvalid += 1;
        options.onRecord?.({ dir, kind: "invalid", reason: legacy.reason });
      }
    } else {
      skippedInvalid += 1;
      options.onRecord?.({ dir, kind: "invalid", reason: result.reason });
    }
  }

  return {
    scanned: dirs.length,
    ingested,
    skippedV1,
    skippedInvalid,
    legacyTimingIngested,
    rebuilt: options.rebuild,
  };
}
