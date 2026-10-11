// /api 404 vs SPA fallback with a web build present. config.webDir is read at import, so the
// app runs in a child process with APP_WEB_DIR / APP_DB_PATH pointing at temp fixtures.
import { expect, test } from "bun:test";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

test("unknown /api path is a 404 while other routes get index.html", async () => {
  const dir = await mkdtemp(join(tmpdir(), "bench-dashboard-spa-"));
  try {
    await writeFile(join(dir, "index.html"), "<html>spa-fixture</html>");
    const script = `const { app } = await import(${JSON.stringify(join(import.meta.dir, "app.ts"))});
const api = await app.request("/api/does-not-exist");
const spa = await app.request("/some/route");
console.log(JSON.stringify({ api: [api.status, await api.json()], spa: [spa.status, await spa.text()] }));`;
    const proc = Bun.spawn([process.execPath, "-e", script], {
      env: { ...process.env, APP_WEB_DIR: dir, APP_DB_PATH: join(dir, "app.db") },
      stdout: "pipe",
      stderr: "inherit",
    });
    const out = await new Response(proc.stdout).text();
    expect(await proc.exited).toBe(0);
    expect(JSON.parse(out.trim().split("\n").pop()!)).toEqual({
      api: [404, { error: "not found" }],
      spa: [200, "<html>spa-fixture</html>"],
    });
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
