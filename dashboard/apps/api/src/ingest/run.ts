import type { IngestSummary } from "@bench-dashboard/shared";
import { clearAllExperiments, replaceExperiment } from "../db/experiments.ts";
import { listRecordDirs, readRecordDir } from "./record.ts";

export interface RunIngestOptions {
  recordsDir: string;
  /** Drops every row this ingest owns, then replays every v2 record from scratch — the
   * brief's `--rebuild` flag. Without it, ingest is a normal idempotent upsert pass. */
  rebuild: boolean;
  /** Injectable logging seam so the CLI can print per-record detail while tests stay
   * quiet — defaults to a no-op, never `console.log` directly. */
  onRecord?: (event: { dir: string; kind: "v2" | "v1" | "invalid"; reason?: string }) => void;
}

/** Scans `recordsDir` (one level deep — `runs/bakeoff/<experiment-id>/`), classifies each
 * subdirectory (`ingest/record.ts readRecordDir`), and upserts every v2 record. v1
 * records (no `schema_version` key, or any value other than `2`) are skipped with a
 * logged count (frozen input contract E1) — never an error, never attempted. */
export async function runIngest(options: RunIngestOptions): Promise<IngestSummary> {
  if (options.rebuild) clearAllExperiments();

  const dirs = await listRecordDirs(options.recordsDir);
  let ingested = 0;
  let skippedV1 = 0;
  let skippedInvalid = 0;

  for (const dir of dirs) {
    const result = await readRecordDir(dir);
    if (result.kind === "v2") {
      replaceExperiment(result.record);
      ingested += 1;
      options.onRecord?.({ dir, kind: "v2" });
    } else if (result.kind === "v1") {
      skippedV1 += 1;
      options.onRecord?.({ dir, kind: "v1" });
    } else {
      skippedInvalid += 1;
      options.onRecord?.({ dir, kind: "invalid", reason: result.reason });
    }
  }

  return { scanned: dirs.length, ingested, skippedV1, skippedInvalid, rebuilt: options.rebuild };
}
