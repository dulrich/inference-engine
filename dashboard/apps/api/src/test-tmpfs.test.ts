// test-tmpfs.test.ts — verifies the bunfig `[test] preload` in ./test-tmpfs.ts. Conditioned
// on TEST_TMPFS_APPLIED (set by the preload itself), never re-derived from host state
// (existsSync("/dev/shm"), etc.) -- exactly one of the two cases below runs per invocation.
//
// Manual opt-out check (not run by CI, verified once at authoring time):
//   bun test apps/api/src/test-tmpfs.test.ts             -> case (i) runs
//   TMPDIR=/tmp bun test apps/api/src/test-tmpfs.test.ts -> case (ii) runs
import { describe, expect, test } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const applied = process.env.TEST_TMPFS_APPLIED === "1";

describe("test-tmpfs preload", () => {
  test.if(applied)("redirects tmpdir() to /dev/shm when TMPDIR was unset and /dev/shm exists", () => {
    expect(tmpdir().startsWith("/dev/shm")).toBe(true);
    const dir = mkdtempSync(join(tmpdir(), "tmpfs-"));
    try {
      expect(dir.startsWith("/dev/shm")).toBe(true);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  test.if(!applied)("leaves a pre-set (or absent) TMPDIR untouched -- opt-out", () => {
    // TEST_TMPFS_APPLIED === "0" means the preload made no change: TMPDIR is exactly
    // what it was before this process started (pre-set by the caller, or unset with
    // no /dev/shm present). Assert the preload did not overwrite it, without
    // re-deriving which of those two host states produced it.
    if (process.env.TMPDIR) {
      expect(tmpdir()).toBe(process.env.TMPDIR);
    } else {
      expect(tmpdir()).not.toBe("/dev/shm");
    }
  });
});
