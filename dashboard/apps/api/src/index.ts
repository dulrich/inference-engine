import { app } from "./app.ts";
import { config } from "./config.ts";

const server = Bun.serve({ port: config.port, fetch: app.fetch });
// Bind scope is decided by the compose publish (loopback by default, one explicit
// trusted-LAN interface via INFERENCE_DASHBOARD_BIND_ADDRESS) -- the process itself
// listens on all container interfaces, so don't claim "loopback only" here.
console.log(`Bench Dashboard API listening on port ${server.port}`);

export { app, server };
