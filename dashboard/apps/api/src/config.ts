import { join } from "node:path";

// Fleet scaffold convention (`config.ts`, model_scoreboard exemplar): a single object,
// computed once at module load from `process.env`, imported everywhere else. No per-field
// `Bun.env` reads scattered through the codebase.
//
// Ports: 4810 (this dashboard API) / 5810 (Vite dev, `apps/web/vite.config.ts`) are the
// SERVICES.md-reserved pair for "Inference Engine (dashboard)" — root plan Public
// Interfaces. Never 4830/5830 (those are model_scoreboard's).

export const config = {
  port: Number(process.env.PORT ?? 4810),

  // dashboard/data/app.db — gitignored (dashboard/.gitignore), derived from llama/bakeoff/records/
  // (Decisions locked: "the DB is derived, records are source of truth").
  dbPath: process.env.APP_DB_PATH ?? join(import.meta.dir, "../../../data/app.db"),

  webDir: process.env.APP_WEB_DIR ?? join(import.meta.dir, "../../web/dist"),

  // The ingest CLI's default scan root: `../llama/bakeoff/records` relative to `dashboard/`
  // (brief's File scope pin), i.e. `<repo-root>/llama/bakeoff/records` from this file's real
  // location four path segments down (`dashboard/apps/api/src`).
  recordsDir: process.env.RECORDS_DIR ?? join(import.meta.dir, "../../../../llama/bakeoff/records"),

  // `GET /api/serving`'s read-only source (cycle-1 blocking finding B1). Defaults to the
  // real repo file for local dev convenience — it IS present in this worktree — but every
  // test sets this explicitly to a fixture path (brief's hard constraint: tests never
  // depend on the real file being mounted).
  llamaConfigPath: process.env.LLAMA_CONFIG_PATH ?? join(import.meta.dir, "../../../../llama/config.yaml"),

  // `GET /api/queue`'s read-only source: `llama/bakeoff/queues/*.json` + sibling
  // `*.state.json` files (WP-A). Unlike `recordsDir`/`llamaConfigPath` above, this
  // defaults to the CONTAINER mount path (`fleet-tools/compose.fleet.yml`'s
  // `…/llama/bakeoff/queues:/queues:ro` + `QUEUES_DIR=/queues`, WP-D), not a repo-relative
  // dev-convenience path — every test sets this explicitly to a fixture path.
  queuesDir: process.env.QUEUES_DIR ?? "/queues",

  // Control plane (WP-4, `plans/bakeoff-queue-control-plane.md`) — both optional, and
  // BOTH left `undefined` (never defaulted, unlike the paths above) when unset: absence
  // is the pinned "control features off" signal (`control: null` on `GET /api/queue`,
  // `POST /api/control/*` 503). `CONTROL_REQUESTS_DIR` — rw drop-box, mounted
  // `…/control/requests:/control/requests` (WP-6). `CONTROL_HOST_DIR` — ro host-written
  // dir containing `status.json`, mounted `…/control/host:/control/host:ro` (WP-6).
  controlRequestsDir: process.env.CONTROL_REQUESTS_DIR,
  controlHostDir: process.env.CONTROL_HOST_DIR,

  // Router status plane (WP-3, `plans/router-observability-and-tool-search-conformance.md`)
  // — both optional and BOTH left `undefined` when unset, exactly like the `CONTROL_*` pair
  // above: absence is the "router status off" signal, and every `/api/router/*` route then
  // answers 200 with `{available: false, reason: "disabled"}` rather than 500.
  //
  // `ROUTER_TELEMETRY_DIR` — the router's own jsonl sink dir (patch 0006), mounted
  // `…/.local/state/inference-router/logs:/var/log/inference-router:ro`. Read by BOTH
  // `/api/router/telemetry` (the `router-*.jsonl` sink) and `/api/router/slots`, because
  // the host slots poller writes `slots-*.jsonl` into that same directory.
  // `CODEX_SIDECAR_LOG_DIR` — the sidecar's jsonl dir, mounted
  // `…/.local/state/codex-sidecar/logs:/var/log/codex-sidecar:ro`.
  //
  // Read-only on both mounts: this API writes neither feed, and every read goes through
  // the bounded byte-window tail reader (`apps/api/src/router/tailReader.ts`).
  routerTelemetryDir: process.env.ROUTER_TELEMETRY_DIR,
  codexSidecarLogDir: process.env.CODEX_SIDECAR_LOG_DIR,
};
