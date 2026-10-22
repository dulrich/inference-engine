// test-tmpfs.ts — bun test preload. Measured 2026-09-05: file-backed bun:sqlite test suites
// (synchronous=FULL) issue ~10k fsyncs per file; on ext4 /tmp that is ~4 s under I/O
// contention from sibling sessions, on tmpfs ~0.3 s (same syscalls, ~2 µs each). Bun
// reads TMPDIR at each os.tmpdir() call, so setting it here redirects every
// mkdtempSync(join(tmpdir(), ...)) in the run. A pre-set TMPDIR is always respected.
import { existsSync } from "node:fs";
const applied = !process.env.TMPDIR && existsSync("/dev/shm");
if (applied) process.env.TMPDIR = "/dev/shm";
process.env.TEST_TMPFS_APPLIED = applied ? "1" : "0";
