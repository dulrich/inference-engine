import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vite";

// Dev port PINNED to 5810 — the SERVICES.md-reserved pair for "Inference Engine
// (dashboard)" (cycle-1 material finding M3: 5810 was documented but never wired). API on
// 4810 (`apps/api/src/config.ts` default `PORT`).
export default defineConfig({
  plugins: [svelte()],
  server: {
    port: 5810,
    strictPort: true,
    proxy: {
      "/api": "http://127.0.0.1:4810",
      "/health": "http://127.0.0.1:4810",
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
