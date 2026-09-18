// thread-config.mjs — is the thread the app-server actually started the one we declared?
//
// `config` silently ignores unknown keys and `thread/start` never echoes the `features` block,
// so neither is self-verifying. Both readings live here: the instruction-source trim, and the
// declared-vs-reported feature comparison against the app-server's own flag oracle. A declared
// key the oracle does not report is *unknown*, not a mismatch — an older or newer binary may
// simply not carry that flag. This is CONFIG drift detection only; `Persona.tripBoundary` is
// the runtime enforcement line.

// `config` silently ignores unknown keys, so the trim is verified at every thread/start.
export function verifyInstructionSources(sources) {
  const list = Array.isArray(sources) ? sources : [];
  if (list.length > 1) return `expected at most the persona AGENTS.md, got ${list.length} instruction sources`;
  const bad = list.filter(s => !/AGENTS\.md$/i.test(typeof s === "string" ? s : JSON.stringify(s)));
  if (bad.length) return `unexpected instruction source: ${String(bad[0]).slice(0, 200)}`;
  return null;
}

// `unified_exec` is pinned true by the model catalog's `shell_type` and reads back `true`
// however we declare it (WP-1 measurement, 2026-09-14) — exempt, so a flag we cannot move
// does not fail every thread closed.
export const FEATURE_VERIFY_EXEMPT = new Set(["unified_exec"]);

// The declared `config.features` block is honoured per thread but never echoed by
// `thread/start`, so it is read back from the app-server's own flag oracle
// (`experimentalFeature/list`) and compared. A declared key the oracle does not report is
// *unknown*, not a mismatch — an older or newer binary may simply not carry that flag.
export function featureReadings(declared, reported) {
  const decl = declared && typeof declared === "object" ? declared : {};
  const byName = new Map();
  for (const e of (Array.isArray(reported) ? reported : [])) {
    if (e && typeof e.name === "string" && !byName.has(e.name)) byName.set(e.name, e);
  }
  const features = {};
  const unknownFeatures = [];
  const mismatches = [];
  for (const [name, wantRaw] of Object.entries(decl)) {
    if (FEATURE_VERIFY_EXEMPT.has(name)) continue;
    const entry = byName.get(name);
    if (entry === undefined) { unknownFeatures.push(name); continue; }
    const got = entry.enabled === true;
    const want = wantRaw === true;
    features[name] = got;
    if (got !== want) mismatches.push({ name, want, got, entry });
  }
  return { features, unknownFeatures, mismatches };
}

// null when the thread's effective flags match what we declared, else the problem string.
export function verifyFeatures(declared, reported) {
  const { mismatches } = featureReadings(declared, reported);
  if (!mismatches.length) return null;
  return `features not honoured: ${mismatches.map(m => `${m.name}=${m.got} (declared ${m.want})`).join(", ")}`;
}
