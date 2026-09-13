import { describe, expect, test } from "bun:test";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { z } from "zod";
import { DEFAULT_TAIL_WINDOW_BYTES, SLOTS_TAIL_WINDOW_BYTES, parseJsonlLines, readTailLines } from "./tailReader.ts";

// The bounded-read contract. The point of this file is the case the fleet's 256m caps make
// non-negotiable: a file MUCH larger than the window must be read through the window only,
// yielding whole lines, with the straddled first line dropped and the last line intact.

const WINDOW = 1024 * 1024;

function temporaryDir(): string {
  return mkdtempSync(join(tmpdir(), "bench-dashboard-tail-"));
}

/** A ~3 MiB jsonl whose every line is self-identifying, so "which lines came back" is an
 * exact assertion rather than a count. */
function writeOversizedFixture(dir: string): { path: string; lineCount: number; size: number } {
  const filler = "x".repeat(900);
  const lines: string[] = [];
  for (let index = 0; index < 3600; index += 1) {
    lines.push(JSON.stringify({ n: index, filler }));
  }
  const body = `${lines.join("\n")}\n`;
  const path = join(dir, "big.jsonl");
  writeFileSync(path, body, "utf8");
  return { path, lineCount: lines.length, size: Buffer.byteLength(body) };
}

describe("readTailLines", () => {
  test("reads a file larger than the window through the window only, whole lines both ends", async () => {
    const { path, lineCount, size } = writeOversizedFixture(temporaryDir());
    expect(size).toBeGreaterThan(3 * WINDOW - 1);

    const result = await readTailLines(path, WINDOW);
    expect(result.size).toBe(size);
    expect(result.windowBytes).toBe(WINDOW);
    expect(result.truncated).toBe(true);

    // Every returned line is whole, parseable JSON — never a fragment.
    for (const line of result.lines) {
      expect(() => JSON.parse(line) as unknown).not.toThrow();
    }
    // The last line of the FILE is the last line returned, intact.
    const last = JSON.parse(result.lines[result.lines.length - 1] as string) as { n: number };
    expect(last.n).toBe(lineCount - 1);
    // Only the tail came back: a window of 1 MiB over a 3 MiB file cannot hold line 0.
    const first = JSON.parse(result.lines[0] as string) as { n: number };
    expect(first.n).toBeGreaterThan(0);
    // …and the line that straddled the window offset was dropped, not returned partial:
    // exactly one line is missing from the front, and the tail is otherwise contiguous.
    expect(result.lines.length).toBeLessThan(lineCount);
    const numbers = result.lines.map((line) => (JSON.parse(line) as { n: number }).n);
    for (let i = 1; i < numbers.length; i += 1) {
      expect(numbers[i]).toBe((numbers[i - 1] as number) + 1);
    }
  });

  test("a file smaller than the window is returned whole", async () => {
    const dir = temporaryDir();
    const path = join(dir, "small.jsonl");
    writeFileSync(path, '{"n":0}\n{"n":1}\n{"n":2}\n', "utf8");
    const result = await readTailLines(path, WINDOW);
    expect(result.truncated).toBe(false);
    expect(result.lines).toEqual(['{"n":0}', '{"n":1}', '{"n":2}']);
  });

  test("an empty file yields no lines and does not throw", async () => {
    const dir = temporaryDir();
    const path = join(dir, "empty.jsonl");
    writeFileSync(path, "", "utf8");
    const result = await readTailLines(path, WINDOW);
    expect(result).toEqual({ lines: [], size: 0, windowBytes: WINDOW, truncated: false });
    // The documented window defaults, asserted here so a silent widening trips a gate.
    expect(DEFAULT_TAIL_WINDOW_BYTES).toBe(1024 * 1024);
    expect(SLOTS_TAIL_WINDOW_BYTES).toBe(256 * 1024);
  });
});

describe("parseJsonlLines", () => {
  const schema = z.object({ n: z.number() });

  test("skips and counts malformed lines instead of throwing", () => {
    const { records, skipped } = parseJsonlLines(
      ['{"n":1}', '{"n":', '{"n":"two"}', '{"n":3}'],
      schema,
    );
    expect(records).toEqual([{ n: 1 }, { n: 3 }]);
    expect(skipped).toBe(2);
  });
});
