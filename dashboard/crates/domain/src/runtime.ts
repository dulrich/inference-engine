type Node =
  | { k: "str"; min: number | null; minMsg: string | null; re: RegExp | null; reMsg: string | null }
  | { k: "num"; int: boolean; gt: number | null; gte: number | null; lte: number | null }
  | { k: "bool" }
  | { k: "null" }
  | { k: "unknown" }
  | { k: "lit"; value: string | number | boolean }
  | { k: "enum"; values: readonly string[] }
  | { k: "array"; item: Node }
  | { k: "object"; fields: readonly Field[]; passthrough: boolean }
  | { k: "record"; key: Node; value: Node }
  | { k: "nullable"; inner: Node }
  | { k: "optional"; inner: Node }
  | { k: "default"; inner: Node; make: () => unknown }
  | { k: "union"; options: readonly Node[] }
  | { k: "disc"; key: string; options: readonly Node[] };
type Field = { name: string; node: Node };
type Issue = { code: string; path: (string | number)[]; message: string; [key: string]: unknown };
class ContractError extends Error {
  readonly issues: Issue[];
  constructor(issues: Issue[]) {
    super(JSON.stringify(issues, null, 2));
    this.name = "ContractError";
    this.issues = issues;
  }
}
const FAIL = Symbol("fail");
type R = typeof FAIL | { d: boolean; v: unknown };
type Obj = Record<string, unknown>;
const isObject = (v: unknown): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v);
const received = (v: unknown): string =>
  v === null ? "null" : Array.isArray(v) ? "array" : typeof v === "number" && Number.isNaN(v) ? "nan" : typeof v;
function run(n: Node, v: unknown, path: (string | number)[], issues: Issue[]): R {
  const push = (code: string, message: string, extra: Obj = {}, at: (string | number)[] = path) => {
    issues.push({ code, ...extra, path: at, message });
  };
  const typeError = (expected: string, got = received(v)): R => {
    push("invalid_type", v === undefined ? "Required" : `Expected ${expected}, received ${got}`, { expected, received: got });
    return FAIL;
  };
  switch (n.k) {
    case "str": {
      if (typeof v !== "string") return typeError("string");
      let d = false;
      if (n.min !== null && v.length < n.min) {
        push("too_small", n.minMsg ?? `String must contain at least ${n.min} character(s)`, { minimum: n.min, type: "string", inclusive: true, exact: false });
        d = true;
      }
      if (n.re !== null && !n.re.test(v)) {
        push("invalid_string", n.reMsg ?? "Invalid", { validation: "regex" });
        d = true;
      }
      return { d, v };
    }
    case "num": {
      const x = v;
      if (typeof x !== "number" || Number.isNaN(x)) return typeError("number");
      let d = false;
      if (n.int && !Number.isInteger(x)) {
        push("invalid_type", "Expected integer, received float", { expected: "integer", received: "float" });
        d = true;
      }
      if (n.gt !== null && !(x > n.gt)) {
        push("too_small", `Number must be greater than ${n.gt}`, { minimum: n.gt, type: "number", inclusive: false, exact: false });
        d = true;
      }
      if (n.gte !== null && !(x >= n.gte)) {
        push("too_small", `Number must be greater than or equal to ${n.gte}`, { minimum: n.gte, type: "number", inclusive: true, exact: false });
        d = true;
      }
      if (n.lte !== null && !(x <= n.lte)) {
        push("too_big", `Number must be less than or equal to ${n.lte}`, { maximum: n.lte, type: "number", inclusive: true, exact: false });
        d = true;
      }
      return { d, v: x };
    }
    case "bool":
      return typeof v === "boolean" ? { d: false, v } : typeError("boolean");
    case "null":
      return v === null ? { d: false, v } : typeError("null");
    case "unknown":
      return { d: false, v };
    case "lit":
      if (v === n.value) return { d: false, v };
      push("invalid_literal", `Invalid literal value, expected ${JSON.stringify(n.value)}`, { received: v, expected: n.value });
      return FAIL;
    case "enum": {
      const options = n.values.map((x) => `'${x}'`).join(" | ");
      if (typeof v !== "string") return typeError(options);
      if (n.values.includes(v)) return { d: false, v };
      push("invalid_enum_value", `Invalid enum value. Expected ${options}, received '${v}'`, { received: v, options: [...n.values] });
      return FAIL;
    }
    case "array": {
      if (!Array.isArray(v)) return typeError("array");
      let d = false;
      let failed = false;
      const out = v.map((item, i) => {
        const r = run(n.item, item, [...path, i], issues);
        if (r === FAIL) {
          failed = true;
          return undefined;
        }
        d ||= r.d;
        return r.v;
      });
      return failed ? FAIL : { d, v: out };
    }
    case "object": {
      if (!isObject(v)) return typeError("object");
      const out: Obj = {};
      let d = false;
      let failed = false;
      for (const f of n.fields) {
        const r = run(f.node, Object.hasOwn(v, f.name) ? v[f.name] : undefined, [...path, f.name], issues);
        if (r === FAIL) failed = true;
        else {
          d ||= r.d;
          // zod's `alwaysSet`: a key present as `undefined` keeps its slot.
          if (r.v !== undefined || Object.hasOwn(v, f.name)) out[f.name] = r.v;
        }
      }
      // zod v3 `.passthrough()`: keys outside the shape follow it, values verbatim.
      if (n.passthrough) {
        for (const key of Object.keys(v)) {
          if (key !== "__proto__" && !n.fields.some((f) => f.name === key) && v[key] !== undefined) out[key] = v[key];
        }
      }
      return failed ? FAIL : { d, v: out };
    }
    case "record": {
      if (!isObject(v)) return typeError("object");
      const out: Obj = {};
      let d = false;
      let failed = false;
      for (const [key, value] of Object.entries(v)) {
        const k = run(n.key, key, [...path, key], issues);
        const r = run(n.value, value, [...path, key], issues);
        if (k === FAIL || r === FAIL) failed = true;
        else {
          d ||= k.d || r.d;
          if (key !== "__proto__") out[k.v as string] = r.v;
        }
      }
      return failed ? FAIL : { d, v: out };
    }
    case "nullable":
      return v === null ? { d: false, v: null } : run(n.inner, v, path, issues);
    case "optional":
      return v === undefined ? { d: false, v: undefined } : run(n.inner, v, path, issues);
    case "default":
      return run(n.inner, v === undefined ? n.make() : v, path, issues);
    case "union": {
      let dirty: { r: R; issues: Issue[] } | null = null;
      const errors: Issue[][] = [];
      for (const option of n.options) {
        const scratch: Issue[] = [];
        const r = run(option, v, path, scratch);
        if (r !== FAIL && !r.d) return r;
        if (r !== FAIL && dirty === null) dirty = { r, issues: scratch };
        if (scratch.length) errors.push(scratch);
      }
      if (dirty !== null) {
        issues.push(...dirty.issues);
        return dirty.r;
      }
      push("invalid_union", "Invalid input", { unionErrors: errors.map((e) => ({ issues: e, name: "ZodError" })) });
      return FAIL;
    }
    case "disc": {
      if (!isObject(v)) return typeError("object");
      const tags = n.options.map((o) => {
        const f = o.k === "object" ? o.fields.find((x) => x.name === n.key) : undefined;
        return f !== undefined && f.node.k === "lit" ? f.node.value : undefined;
      });
      const at = tags.findIndex((t) => t !== undefined && t === v[n.key]);
      const option = n.options[at];
      if (option === undefined) {
        const listed = tags.map((t) => (typeof t === "string" ? `'${t}'` : String(t))).join(" | ");
        push("invalid_union_discriminator", `Invalid discriminator value. Expected ${listed}`, { options: tags }, [...path, n.key]);
        return FAIL;
      }
      return run(option, v, path, issues);
    }
  }
}
type SafeParseResult<T> = { success: true; data: T; error?: undefined } | { success: false; error: ContractError; data?: undefined };
type Validator<T> = {
  parse(input: unknown): T;
  safeParse(input: unknown): SafeParseResult<T>;
  array(): Validator<T[]>;
  nullable(): Validator<T | null>;
  optional(): Validator<T | undefined>;
};
function validator<T>(node: Node): Validator<T> {
  const check = (input: unknown): SafeParseResult<T> => {
    const issues: Issue[] = [];
    const r = run(node, input, [], issues);
    if (r === FAIL || r.d || issues.length) return { success: false, error: new ContractError(issues) };
    return { success: true, data: r.v as T };
  };
  return {
    parse(input) {
      const r = check(input);
      if (!r.success) throw r.error;
      return r.data;
    },
    safeParse: check,
    array: () => validator<T[]>({ k: "array", item: node }),
    nullable: () => validator<T | null>({ k: "nullable", inner: node }),
    optional: () => validator<T | undefined>({ k: "optional", inner: node }),
  };
}
// Used by the generated exports that follow this runtime in the emitted module.
// oxlint-disable-next-line no-unused-vars
function enumValidator<T, O>(node: Node & { k: "enum" }): Validator<T> & { readonly options: O } {
  return { ...validator<T>(node), options: [...node.values] as O };
}
