import { app } from "./app.ts";
import { config } from "./config.ts";

const server = Bun.serve({ port: config.port, fetch: app.fetch });
console.log(`Bench Dashboard API on http://127.0.0.1:${server.port} (loopback only)`);

export { app, server };
