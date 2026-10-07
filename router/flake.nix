{
  description = "inference-engine router: ai-gateway 782cd51 + router/patches series as a Nix derivation + image (parity-gated against router/Dockerfile by nix-parity.sh)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      lib = pkgs.lib;

      router = pkgs.buildNpmPackage {
        pname = "inference-engine-router";
        version = "1.0.18-782cd51";
        nodejs = pkgs.nodejs_24;

        src = pkgs.fetchFromGitHub {
          owner = "The-NeXT-AI";
          repo = "ai-gateway";
          rev = "782cd51941e5da198ea6543feb7dc9e6e8ee3915";
          hash = "sha256-AZ7JnpS2d2LLgvgbC4M0JU8/zfJ0/j5I7WhzT4YSRjk=";
        };

        # The vendored series, in order: every router/patches/*.patch, sorted by name
        # (the NNNN- prefix). This repo's files are read directly, never copied.
        # buildNpmPackage forwards `patches` to fetchNpmDeps, so lockfile-only bumps
        # (0005/0008/0011/0012) move npmDepsHash. router/nix-parity.sh fails if this
        # list ever differs from the Dockerfile's `git apply` sequence.
        patches = map (f: ./patches + "/${f}") (lib.sort lib.lessThan
          (lib.filter (f: lib.hasSuffix ".patch" f) (lib.attrNames (builtins.readDir ./patches))));

        npmDepsHash = "sha256-vCIZ49HvLgx2xA5SkyZzxnngNOMYuAR2QqbXPQqOCgE=";
        npmBuildScript = "build";

        # Same build-stage gate as router/Dockerfile (222/222 at 0013).
        doCheck = true;
        checkPhase = ''
          runHook preCheck
          npx vitest run \
            src/adapters/builtins/common.test.ts \
            src/gateway/streaming-conversion.test.ts \
            src/gateway/routes.test.ts \
            src/gateway/request-telemetry.test.ts \
            src/adapters/builtins/target/openai-chat-compat/effort-kwargs.test.ts \
            src/adapters/builtins/target/openai-chat-compat/max-tokens-clamp.test.ts
          # vitest writes timing results into node_modules/.vite — the only
          # nondeterministic byte in the output (measured with --rebuild).
          rm -rf node_modules/.vite
          runHook postCheck
        '';

        # Runtime on nodejs-slim: drops the bundled npm the Dockerfile deletes by hand
        # (it carried 1 CRITICAL + 7 HIGH in the security scan).
        postFixup = ''
          for f in $(grep -rlI '${pkgs.nodejs_24}' $out); do
            substituteInPlace "$f" --replace-fail '${pkgs.nodejs_24}' '${pkgs.nodejs-slim_24}'
          done
        '';
        disallowedReferences = [ pkgs.nodejs_24 ];
      };

      image = pkgs.dockerTools.buildLayeredImage {
        name = "inference-engine-router-nix";
        tag = "nix";
        contents = [ pkgs.cacert ];
        config = {
          Entrypoint = [ "${router}/bin/next-ai-gateway" ];
          ExposedPorts."4813/tcp" = { };
          Env = [
            "GATEWAY_CONFIG_PATH=/app/gateway.config.json"
            "NODE_ENV=production"
            "ROUTER_TELEMETRY_ENABLED=true"
            "ROUTER_TELEMETRY_DIR=/var/log/inference-router"
            "ROUTER_TELEMETRY_MAX_BYTES=16777216"
            "ROUTER_TELEMETRY_KEEP_FILES=14"
            "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
          ];
        };
      };
    in
    {
      packages.${system} = { inherit router image; default = router; };
      checks.${system} = { inherit router; };
    };
}
