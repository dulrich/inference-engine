import js from "@eslint/js";
import { globalIgnores } from "eslint/config";
import svelte from "eslint-plugin-svelte";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  globalIgnores([
    "node_modules",
    "apps/*/dist",
    "packages/*/dist",
    "coverage",
    "data",
    "data/*.db",
    "data/*.db-*",
    // Vendored @fleet-tools/signalworks copy (signalworks/install.sh) — never
    // hand-edited here; upstream (fleet-tools/signalworks/) owns its own lint hygiene.
    "packages/signalworks",
  ]),
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...svelte.configs["flat/recommended"],
  {
    files: ["**/*.svelte"],
    languageOptions: {
      parserOptions: {
        parser: tseslint.parser,
      },
    },
    rules: {
      "@typescript-eslint/no-unused-expressions": "off",
    },
  },
  {
    files: ["apps/web/**/*.{js,ts,svelte}", "apps/web/svelte.config.js"],
    languageOptions: {
      globals: {
        ...globals.browser,
      },
    },
  },
  {
    // `scripts/**` is host-side Bun tooling (the router slots poller and its installer),
    // not part of any workspace build -- same Bun+node global set as the API.
    files: ["apps/api/**/*.ts", "packages/shared/**/*.ts", "scripts/**/*.{js,mjs,ts}", "*.config.{js,ts,mts}"],
    languageOptions: {
      globals: {
        ...globals.bun,
        ...globals.node,
      },
    },
  },
);
