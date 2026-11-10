#!/usr/bin/env node
// mcp-sync: build Claude Code, Codex and opencode MCP server config from one JSONC base file.
// Requires Node 22.18+ (runs TypeScript directly); no dependencies.

import {
  chmodSync, copyFileSync, existsSync, mkdirSync, readFileSync, realpathSync,
  renameSync, statSync, writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

type Obj = { [key: string]: unknown };
type Harness = "claude" | "codex" | "opencode";
type Kind = "add" | "change" | "remove" | "same";
type Change = { name: string; kind: Kind; note?: string };
type Plan = { harness: Harness; file: string; changes: Change[]; commit: () => void };

const HARNESSES: Harness[] = ["claude", "codex", "opencode"];
const BEGIN = "# BEGIN mcp-sync";
const END = "# END mcp-sync";
const REF = /^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$/;
const BEARER_REF = /^Bearer \$\{([A-Za-z_][A-Za-z0-9_]*)\}$/;
const SECRETISH = /key|token|secret|passw|credential|auth/i;
const KNOWN_KEYS = ["type", "command", "args", "env", "cwd", "url", "headers", "harnesses", "claude", "codex", "opencode"];

export class UserError extends Error {}

const isObj = (v: unknown): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v);

// ---------- JSONC ----------

/** Strip line and block comments plus trailing commas, leaving string contents alone. */
export function stripJsonc(text: string): string {
  let out = "";
  let i = 0;
  let inString = false;
  while (i < text.length) {
    const c = text[i];
    const next = text[i + 1];
    if (inString) {
      out += c;
      if (c === "\\") { out += next ?? ""; i += 2; continue; }
      if (c === '"') inString = false;
      i++;
      continue;
    }
    if (c === '"') { inString = true; out += c; i++; continue; }
    if (c === "/" && next === "/") {
      while (i < text.length && text[i] !== "\n") i++;
      continue;
    }
    if (c === "/" && next === "*") {
      const end = text.indexOf("*/", i + 2);
      if (end < 0) throw new Error("unterminated /* comment");
      i = end + 2;
      out += " ";
      continue;
    }
    if (c === ",") {
      let j = i + 1;
      while (j < text.length && /\s/.test(text[j])) j++;
      // Comments between a trailing comma and its closer: handled by running twice below.
      if (text[j] === "}" || text[j] === "]") { i++; continue; }
    }
    out += c;
    i++;
  }
  return out;
}

export function parseJsonc(text: string, file = "<input>"): unknown {
  try {
    // Second pass catches a trailing comma that was followed by a comment before the closer.
    return JSON.parse(stripJsonc(stripJsonc(text)));
  } catch (e) {
    throw new UserError(`${file}: ${(e as Error).message}`);
  }
}

// ---------- Base file loading and validation ----------

export function loadServers(file: string, stack: string[] = []): Record<string, Obj> {
  const abs = resolve(file);
  if (stack.includes(abs)) throw new UserError(`extends cycle: ${[...stack, abs].join(" -> ")}`);
  if (!existsSync(abs)) throw new UserError(`not found: ${abs}`);
  const doc = parseJsonc(readFileSync(abs, "utf8"), abs);
  if (!isObj(doc)) throw new UserError(`${abs}: top level must be an object`);
  for (const key of Object.keys(doc)) {
    if (key !== "extends" && key !== "mcpServers") throw new UserError(`${abs}: unknown top-level key "${key}"`);
  }
  const parents = typeof doc.extends === "string" ? [doc.extends] : Array.isArray(doc.extends) ? doc.extends : [];
  const servers: Record<string, Obj> = {};
  for (const parent of parents) {
    Object.assign(servers, loadServers(resolve(dirname(abs), String(parent)), [...stack, abs]));
  }
  const own = doc.mcpServers ?? {};
  if (!isObj(own)) throw new UserError(`${abs}: "mcpServers" must be an object`);
  for (const [name, value] of Object.entries(own)) {
    if (value === null) delete servers[name]; // null removes an inherited server
    else if (isObj(value)) servers[name] = value;
    else throw new UserError(`${abs}: server "${name}" must be an object (or null to remove it)`);
  }
  return servers;
}

function expandHome(value: unknown, home: string): unknown {
  if (typeof value === "string") return value.replaceAll("${HOME}", home);
  if (Array.isArray(value)) return value.map((v) => expandHome(v, home));
  if (isObj(value)) return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, expandHome(v, home)]));
  return value;
}

function stringMap(name: string, field: string, value: unknown): Record<string, string> {
  if (!isObj(value) || Object.values(value).some((v) => typeof v !== "string")) {
    throw new UserError(`${name}: "${field}" must be an object of strings`);
  }
  return value as Record<string, string>;
}

export function validateServer(name: string, srv: Obj): void {
  if (!/^[A-Za-z0-9_-]+$/.test(name)) throw new UserError(`${name}: server names may only use letters, numbers, - and _`);
  const hasCommand = typeof srv.command === "string";
  const hasUrl = typeof srv.url === "string";
  if (hasCommand === hasUrl) throw new UserError(`${name}: set exactly one of "command" or "url"`);
  if (srv.args !== undefined && (!Array.isArray(srv.args) || srv.args.some((a) => typeof a !== "string"))) {
    throw new UserError(`${name}: "args" must be an array of strings`);
  }
  if (srv.env !== undefined) stringMap(name, "env", srv.env);
  if (srv.headers !== undefined) stringMap(name, "headers", srv.headers);
  if (srv.harnesses !== undefined) {
    if (!Array.isArray(srv.harnesses) || srv.harnesses.some((h) => !HARNESSES.includes(h as Harness))) {
      throw new UserError(`${name}: "harnesses" must be a list drawn from ${HARNESSES.join(", ")}`);
    }
  }
  for (const key of ["claude", "codex", "opencode"]) {
    if (srv[key] !== undefined && !isObj(srv[key])) throw new UserError(`${name}: "${key}" must be an object`);
  }
  lintSecrets(name, srv);
}

/** Secrets must be ${VAR} references, never literals. */
function lintSecrets(name: string, srv: Obj): void {
  const env = (srv.env ?? {}) as Record<string, string>;
  for (const [k, v] of Object.entries(env)) {
    if (SECRETISH.test(k) && !REF.test(v)) {
      throw new UserError(`${name}: env ${k} looks like a secret; use "\${${k}}" and export it in your shell`);
    }
  }
  const headers = (srv.headers ?? {}) as Record<string, string>;
  for (const [k, v] of Object.entries(headers)) {
    if (SECRETISH.test(k) && !REF.test(v) && !/^(Bearer|Basic|Token) \$\{[A-Za-z_][A-Za-z0-9_]*\}$/.test(v)) {
      throw new UserError(`${name}: header ${k} looks like a secret; use a \${VAR} reference`);
    }
  }
  if (typeof srv.url === "string" && /[?&](token|key|api[_-]?key|access[_-]?token|secret)=(?!\$\{)[^&]+/i.test(srv.url)) {
    throw new UserError(`${name}: url contains what looks like a literal credential; use a \${VAR} reference`);
  }
}

export function buildServers(file: string, home: string): Record<string, Obj> {
  const servers = loadServers(file);
  const out: Record<string, Obj> = {};
  for (const [name, srv] of Object.entries(servers)) {
    validateServer(name, srv);
    out[name] = expandHome(srv, home) as Obj;
  }
  return out;
}

const appliesTo = (srv: Obj, harness: Harness) => !Array.isArray(srv.harnesses) || srv.harnesses.includes(harness);

// ---------- Claude translation ----------

export function toClaude(srv: Obj): Obj {
  const merged: Obj = { ...srv, ...(isObj(srv.claude) ? srv.claude : {}) };
  for (const k of ["harnesses", "claude", "codex", "opencode"]) delete merged[k];
  const out: Obj = { type: merged.type ?? (typeof merged.url === "string" ? "http" : "stdio") };
  for (const k of ["command", "args", "env", "cwd", "url", "headers"]) if (k in merged) out[k] = merged[k];
  for (const [k, v] of Object.entries(merged)) if (!(k in out)) out[k] = v;
  return out;
}

// ---------- opencode translation ----------

/** opencode interpolates `{env:NAME}` in any string value, so `${NAME}` references map straight across. */
function toEnvSyntax(v: unknown): unknown {
  if (typeof v === "string") return v.replace(/\$\{([A-Za-z_][A-Za-z0-9_]*)\}/g, "{env:$1}");
  if (Array.isArray(v)) return v.map(toEnvSyntax);
  if (isObj(v)) return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, toEnvSyntax(x)]));
  return v;
}

export function toOpencode(name: string, srv: Obj, warn: (msg: string) => void): Obj {
  const fail = (msg: string): never => { throw new UserError(`${name}: ${msg}`); };
  for (const key of Object.keys(srv)) {
    if (!KNOWN_KEYS.includes(key)) warn(`${name}: "${key}" has no opencode mapping and was skipped (use an "opencode" block to set it)`);
  }
  const out: Obj = {};
  if (typeof srv.command === "string") {
    if (srv.type && srv.type !== "stdio") fail(`type "${srv.type}" is not supported by opencode`);
    out.type = "local";
    out.command = toEnvSyntax([srv.command, ...((srv.args ?? []) as string[])]);
    if (typeof srv.cwd === "string") out.cwd = srv.cwd;
    if (srv.env) out.environment = toEnvSyntax(srv.env);
  } else {
    if (srv.type && srv.type !== "http" && srv.type !== "streamable-http") fail(`type "${srv.type}" is not supported by opencode`);
    out.type = "remote";
    out.url = toEnvSyntax(srv.url);
    if (srv.headers) {
      out.headers = toEnvSyntax(srv.headers);
      // opencode tries OAuth on remote servers by default; an explicit Authorization header means don't.
      if (Object.keys(srv.headers as Obj).some((h) => h.toLowerCase() === "authorization")) out.oauth = false;
    }
  }
  return { ...out, ...(isObj(srv.opencode) ? srv.opencode : {}) };
}

// ---------- Codex translation ----------

export function toCodex(name: string, srv: Obj, warn: (msg: string) => void): Obj {
  const fail = (msg: string): never => { throw new UserError(`${name}: ${msg}`); };
  const out: Obj = {};
  const runtimeRef = (s: string) => s.includes("${");

  for (const key of Object.keys(srv)) {
    if (!KNOWN_KEYS.includes(key)) warn(`${name}: "${key}" has no Codex mapping and was skipped (use a "codex" block to set it)`);
  }

  if (typeof srv.command === "string") {
    if (srv.type && srv.type !== "stdio") fail(`type "${srv.type}" is not supported by Codex`);
    const args = (srv.args ?? []) as string[];
    if (runtimeRef(srv.command) || args.some(runtimeRef)) {
      fail('Codex does not expand ${VAR} in command or args; use ${HOME} or move the value into env');
    }
    out.command = srv.command;
    if (args.length) out.args = args;
    if (typeof srv.cwd === "string") out.cwd = srv.cwd;
    const env: Record<string, string> = {};
    const forward: string[] = [];
    for (const [k, v] of Object.entries((srv.env ?? {}) as Record<string, string>)) {
      const ref = REF.exec(v);
      if (ref && ref[1] === k) forward.push(k);
      else if (ref) fail(`env ${k}=\${${ref[1]}}: Codex can only forward a variable under its own name`);
      else if (runtimeRef(v)) fail(`env ${k}: Codex cannot interpolate inside values`);
      else env[k] = v;
    }
    if (forward.length) out.env_vars = forward;
    if (Object.keys(env).length) out.env = env;
  } else {
    if (srv.type && srv.type !== "http" && srv.type !== "streamable-http") fail(`type "${srv.type}" is not supported by Codex`);
    out.url = srv.url;
    const literal: Record<string, string> = {};
    const fromEnv: Record<string, string> = {};
    for (const [k, v] of Object.entries((srv.headers ?? {}) as Record<string, string>)) {
      const bearer = BEARER_REF.exec(v);
      const ref = REF.exec(v);
      if (k.toLowerCase() === "authorization" && bearer) out.bearer_token_env_var = bearer[1];
      else if (ref) fromEnv[k] = ref[1];
      else if (runtimeRef(v)) fail(`header ${k}: Codex cannot interpolate inside header values`);
      else literal[k] = v;
    }
    if (Object.keys(literal).length) out.http_headers = literal;
    if (Object.keys(fromEnv).length) out.env_http_headers = fromEnv;
  }
  return { ...out, ...(isObj(srv.codex) ? srv.codex : {}) };
}

// ---------- TOML output ----------

const tomlKey = (k: string) => (/^[A-Za-z0-9_-]+$/.test(k) ? k : JSON.stringify(k));

function tomlValue(v: unknown): string {
  if (typeof v === "string") return JSON.stringify(v);
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  if (Array.isArray(v)) return `[${v.map(tomlValue).join(", ")}]`;
  if (isObj(v)) return `{ ${Object.entries(v).map(([k, x]) => `${tomlKey(k)} = ${tomlValue(x)}`).join(", ")} }`;
  throw new UserError(`cannot write ${JSON.stringify(v)} as TOML`);
}

function emitTable(path: string[], obj: Obj, out: string[]): void {
  out.push(`[${path.map(tomlKey).join(".")}]`);
  const subtables: [string, Obj][] = [];
  for (const [k, v] of Object.entries(obj)) {
    if (isObj(v)) { if (Object.keys(v).length) subtables.push([k, v]); }
    else out.push(`${tomlKey(k)} = ${tomlValue(v)}`);
  }
  for (const [k, v] of subtables) { out.push(""); emitTable([...path, k], v, out); }
}

export function codexServerToml(name: string, table: Obj): string {
  const lines: string[] = [];
  emitTable(["mcp_servers", name], table, lines);
  return lines.join("\n");
}

// ---------- Codex config editing ----------

const blockRe = new RegExp(`^${BEGIN}[^\\n]*\\n[\\s\\S]*?^${END}[^\\n]*(\\n|$)`, "m");
const headerRe = /^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$/;

/** Split a TOML dotted path like `mcp_servers."a.b".env` into segments. */
function tomlPath(raw: string): string[] {
  const parts: string[] = [];
  const re = /\s*(?:"((?:[^"\\]|\\.)*)"|([A-Za-z0-9_-]+))\s*(?:\.|$)/y;
  let m: RegExpExecArray | null;
  let pos = 0;
  while (pos < raw.length && (m = (re.lastIndex = pos, re.exec(raw)))) {
    parts.push(m[1] !== undefined ? JSON.parse(`"${m[1]}"`) : m[2]);
    pos = re.lastIndex;
  }
  return parts;
}

function mcpServerOf(line: string): string | undefined {
  const m = headerRe.exec(line);
  if (!m) return undefined;
  const p = tomlPath(m[1]);
  return p[0] === "mcp_servers" && p.length > 1 ? p[1] : undefined;
}

/** Map server name -> its TOML text, for a managed block or any chunk of config. */
function serverChunks(text: string): Map<string, string> {
  const chunks = new Map<string, string[]>();
  let current: string | undefined;
  for (const line of text.split("\n")) {
    if (line.startsWith(BEGIN)) continue;
    if (line.startsWith(END)) { current = undefined; continue; }
    if (headerRe.test(line)) current = mcpServerOf(line);
    if (current !== undefined) (chunks.get(current) ?? chunks.set(current, []).get(current)!).push(line);
  }
  return new Map([...chunks].map(([k, v]) => [k, v.join("\n").trim()]));
}

/** Remove the tables for `names` (and their sub-tables), keeping neighbouring comments. */
export function removeTables(text: string, names: Set<string>): string {
  const out: string[] = [];
  let skipping = false;
  let trail: string[] = [];
  for (const line of text.split("\n")) {
    if (headerRe.test(line)) {
      const server = mcpServerOf(line);
      const drop = server !== undefined && names.has(server);
      // Comments directly above a removed table describe it, so they go with it.
      if (drop) while (out.length && out[out.length - 1].trim().startsWith("#")) out.pop();
      if (!drop) out.push(...trail);
      trail = [];
      skipping = drop;
      if (!drop) out.push(line);
      continue;
    }
    if (!skipping) { out.push(line); continue; }
    if (line.trim() === "" || line.trim().startsWith("#")) trail.push(line);
    else trail = []; // key/value line belongs to the removed table
  }
  return out.join("\n");
}

/** --prune: also remove servers that are not in the profile. Names in `keep` are spared. */
type Prune = { on: boolean; keep: Set<string> };

function planCodex(file: string, desired: Record<string, Obj>, source: string, adopt: boolean, dry: boolean, prune: Prune): Plan {
  const original = existsSync(file) ? readFileSync(file, "utf8") : "";
  const block = blockRe.exec(original);
  // Apps (e.g. the Codex desktop app) sometimes insert their own tables next to ours, inside the markers.
  // Only names recorded in the BEGIN line are ours; anything else in the block is foreign and is moved out.
  // A block written before names were recorded is trusted only for servers this profile still wants.
  const header = block ? block[0].split("\n", 1)[0] : "";
  const listed = new Set((/names=([^\s;]*)/.exec(header)?.[1] ?? "").split(",").filter(Boolean));
  const legacy = Boolean(block) && !/names=/.test(header);
  const inBlock = block ? serverChunks(block[0]) : new Map<string, string>();
  const isOurs = (n: string) => listed.has(n) || (legacy && n in desired);
  const previous = new Map([...inBlock].filter(([n]) => isOurs(n)));
  const foreign = new Map([...inBlock].filter(([n]) => !isOurs(n)));
  const outside = block ? original.replace(blockRe, "") : original;
  const unmanaged = new Set([...serverChunks(outside).keys(), ...foreign.keys()]);

  const wanted = new Map(Object.entries(desired).map(([n, t]) => [n, codexServerToml(n, t)]));
  const clash = [...wanted.keys()].filter((n) => unmanaged.has(n));
  if (clash.length && !adopt) {
    throw new UserError(`codex: ${clash.join(", ")} already defined outside the managed block in ${file}; rerun with --adopt to take them over`);
  }

  const changes: Change[] = [];
  for (const [name, text] of wanted) {
    if (clash.includes(name)) changes.push({ name, kind: "change", note: "adopt" });
    else if (!previous.has(name)) changes.push({ name, kind: "add" });
    else changes.push({ name, kind: previous.get(name) === text.trim() ? "same" : "change" });
  }
  for (const name of previous.keys()) if (!wanted.has(name)) changes.push({ name, kind: "remove" });
  const pruned = prune.on ? [...unmanaged].filter((n) => !wanted.has(n) && !prune.keep.has(n)) : [];
  for (const name of pruned) changes.push({ name, kind: "remove", note: "prune" });

  const drop = new Set([...clash, ...pruned]);
  const kept = [...foreign].filter(([n]) => !drop.has(n));
  for (const [name] of kept) changes.push({ name, kind: "change", note: "moved out of the managed block" });
  const base = drop.size ? removeTables(outside, drop) : outside;
  const rendered = wanted.size
    ? `${BEGIN} (from ${source}) names=${[...wanted.keys()].join(",")}; generated, edits here are overwritten\n${[...wanted.values()].join("\n\n")}\n${END}\n`
    : "";
  let next: string;
  if (block && !drop.size && !foreign.size) next = original.replace(blockRe, () => rendered);
  else {
    next = base.replace(/\n*$/, "\n")
      + (kept.length ? `\n${kept.map(([, text]) => text).join("\n\n")}\n` : "")
      + (rendered ? `\n${rendered}` : "");
  }
  if (!original && !rendered) next = "";

  return {
    harness: "codex", file, changes,
    commit: () => { if (!dry && next !== original) safeWrite(file, next); },
  };
}

// ---------- Claude config editing ----------

const stable = (v: unknown): string =>
  JSON.stringify(v, (_k, x) => (isObj(x) ? Object.fromEntries(Object.entries(x).sort(([a], [b]) => a.localeCompare(b))) : x));

/** Where a JSON harness keeps its server map, and how to put an updated map back. */
type JsonLayout = {
  harness: Harness;
  parse: (text: string) => Obj;
  get: (cfg: Obj) => Record<string, unknown>;
  set: (cfg: Obj, servers: Record<string, unknown>) => Obj;
};

const claudeLayout: JsonLayout = {
  harness: "claude",
  parse: (text) => JSON.parse(text) as Obj,
  get: (cfg) => (isObj(cfg.mcpServers) ? { ...cfg.mcpServers } : {}),
  set: (cfg, servers) => ({ ...cfg, mcpServers: servers }),
};

/** opencode keeps servers under `mcp.servers`; `mcp.timeout` and everything else is preserved. */
function opencodeLayout(warn: (msg: string) => void): JsonLayout {
  return {
    harness: "opencode",
    parse: (text) => {
      if (stripJsonc(text) !== text) warn("opencode config has comments or trailing commas; they will not survive the rewrite (a .mcp-sync.bak copy is kept)");
      const doc = parseJsonc(text);
      return isObj(doc) ? doc : {};
    },
    get: (cfg) => {
      const mcp = isObj(cfg.mcp) ? cfg.mcp : {};
      const legacy = Object.keys(mcp).filter((k) => k !== "servers" && k !== "timeout");
      if (legacy.length) warn(`opencode: deprecated flat mcp entries (${legacy.join(", ")}) are left alone; mcp-sync manages mcp.servers`);
      return isObj(mcp.servers) ? { ...mcp.servers } : {};
    },
    set: (cfg, servers) => ({ ...cfg, mcp: { ...(isObj(cfg.mcp) ? cfg.mcp : {}), servers } }),
  };
}

function planJson(layout: JsonLayout, file: string, desired: Record<string, Obj>, managed: string[], adopt: boolean, dry: boolean,
  prune: Prune, onManaged: (names: string[]) => void): Plan {
  const harness = layout.harness;
  const cfg: Obj = existsSync(file) ? layout.parse(readFileSync(file, "utf8")) : {};
  const current = layout.get(cfg);

  const clash = Object.keys(desired).filter((n) => n in current && !managed.includes(n));
  const different = clash.filter((n) => stable(current[n]) !== stable(desired[n]));
  if (different.length && !adopt) {
    throw new UserError(`${harness}: ${different.join(", ")} already defined in ${file} and not managed by mcp-sync; rerun with --adopt to overwrite them`);
  }

  const changes: Change[] = [];
  for (const [name, entry] of Object.entries(desired)) {
    if (!(name in current)) changes.push({ name, kind: "add" });
    else if (stable(current[name]) === stable(entry)) changes.push({ name, kind: "same" });
    else changes.push({ name, kind: "change", note: managed.includes(name) ? undefined : "adopt" });
  }
  for (const name of managed) if (!(name in desired) && name in current) changes.push({ name, kind: "remove" });
  if (prune.on) {
    for (const name of Object.keys(current)) {
      if (name in desired || managed.includes(name) || prune.keep.has(name)) continue;
      changes.push({ name, kind: "remove", note: "prune" });
    }
  }

  for (const c of changes) {
    if (c.kind === "remove") delete current[c.name];
    else current[c.name] = desired[c.name];
  }
  const next = layout.set(cfg, current);

  return {
    harness, file, changes,
    commit: () => {
      if (dry) return;
      if (changes.some((c) => c.kind !== "same")) {
        safeWrite(file, JSON.stringify(next, null, 2) + "\n");
      }
      onManaged(Object.keys(desired));
    },
  };
}

// ---------- Files ----------

function safeWrite(file: string, content: string): void {
  mkdirSync(dirname(file), { recursive: true });
  if (existsSync(file)) copyFileSync(file, `${file}.mcp-sync.bak`);
  const tmp = join(dirname(file), `.${basename(file)}.mcp-sync.tmp`);
  writeFileSync(tmp, content);
  if (existsSync(file)) chmodSync(tmp, statSync(file).mode & 0o777);
  renameSync(tmp, file);
}

function statePath(home: string): string {
  return join(process.env.XDG_STATE_HOME ?? join(home, ".local", "state"), "mcp-sync", "state.json");
}

/** Server names mcp-sync manages in the JSON harnesses (Codex tracks its own via the marker block). */
type State = { claude: string[]; opencode: string[] };

function readState(home: string): State {
  try {
    const s = JSON.parse(readFileSync(statePath(home), "utf8")) as Obj;
    return {
      claude: Array.isArray(s.claude) ? (s.claude as string[]) : [],
      opencode: Array.isArray(s.opencode) ? (s.opencode as string[]) : [],
    };
  } catch {
    return { claude: [], opencode: [] };
  }
}

/** ~/.config/opencode/opencode.json, or opencode.jsonc if that is the one you have. */
function opencodeConfigPath(home: string): string {
  const dir = join(process.env.XDG_CONFIG_HOME ?? join(home, ".config"), "opencode");
  const jsonc = join(dir, "opencode.jsonc");
  return existsSync(jsonc) && !existsSync(join(dir, "opencode.json")) ? jsonc : join(dir, "opencode.json");
}

// ---------- CLI ----------

/** A bare name like "work" means mcp/work.jsonc next to this script; anything else is a path. */
export function resolveBase(arg: string): string {
  if (/^[A-Za-z0-9_-]+$/.test(arg)) {
    const bundled = join(dirname(realpathSync(fileURLToPath(import.meta.url))), "mcp", `${arg}.jsonc`);
    if (existsSync(bundled)) return bundled;
  }
  return resolve(arg);
}

const USAGE = `Usage: mcp-sync <profile|base.jsonc> [options]

Build Claude Code, Codex and opencode MCP config from one base file. A bare
profile name such as "work" means mcp/work.jsonc in this repo; otherwise pass
a path.

Options:
  --harness <list>   Comma-separated: claude, codex, opencode (default: all)
  --dry-run          Show what would change; write nothing
  --adopt            Take over servers that already exist outside mcp-sync
  --prune            Also remove user-level servers that are not in the profile
  --keep <list>      With --prune, comma-separated server names to leave alone
  -h, --help         Show this help

Writes ~/.claude.json (mcpServers only), ~/.codex/config.toml (managed block)
and ~/.config/opencode/opencode.json (mcp.servers only). Originals are saved next to them as *.mcp-sync.bak.`;

export function run(argv: string[], out: (s: string) => void = console.log, err: (s: string) => void = console.error): number {
  try {
    const { values, positionals } = parseArgs({
      args: argv, allowPositionals: true,
      options: { harness: { type: "string" }, "dry-run": { type: "boolean" }, adopt: { type: "boolean" }, prune: { type: "boolean" }, keep: { type: "string" }, help: { type: "boolean", short: "h" } },
    });
    if (values.help) { out(USAGE); return 0; }
    if (positionals.length !== 1) throw new UserError(`expected one base file\n\n${USAGE}`);

    const targets = (values.harness ?? HARNESSES.join(",")).split(",").map((s) => s.trim()) as Harness[];
    for (const t of targets) if (!HARNESSES.includes(t)) throw new UserError(`unknown harness "${t}"`);

    const home = homedir();
    const dry = Boolean(values["dry-run"]);
    const adopt = Boolean(values.adopt);
    if (values.keep && !values.prune) throw new UserError("--keep only makes sense with --prune");
    const prune: Prune = { on: Boolean(values.prune), keep: new Set((values.keep ?? "").split(",").map((s) => s.trim()).filter(Boolean)) };
    const baseFile = resolveBase(positionals[0]);
    const servers = buildServers(baseFile, home);
    const state = readState(home);
    const plans: Plan[] = [];

    if (targets.includes("claude")) {
      const desired = Object.fromEntries(
        Object.entries(servers).filter(([, s]) => appliesTo(s, "claude")).map(([n, s]) => [n, toClaude(s)]));
      plans.push(planJson(claudeLayout, join(home, ".claude.json"), desired, state.claude, adopt, dry, prune, (names) => { state.claude = names; }));
    }
    if (targets.includes("opencode")) {
      const warn = (m: string) => err(`warning: ${m}`);
      const desired = Object.fromEntries(
        Object.entries(servers).filter(([, s]) => appliesTo(s, "opencode")).map(([n, s]) => [n, toOpencode(n, s, warn)]));
      plans.push(planJson(opencodeLayout(warn), opencodeConfigPath(home), desired, state.opencode, adopt, dry, prune, (names) => { state.opencode = names; }));
    }
    if (targets.includes("codex")) {
      const desired = Object.fromEntries(
        Object.entries(servers).filter(([, s]) => appliesTo(s, "codex")).map(([n, s]) => [n, toCodex(n, s, (m) => err(`warning: ${m}`))]));
      const codexHome = process.env.CODEX_HOME ?? join(home, ".codex");
      plans.push(planCodex(join(codexHome, "config.toml"), desired, basename(baseFile), adopt, dry, prune));
    }

    const sign = { add: "+", change: "~", remove: "-", same: "=" } as const;
    for (const plan of plans) {
      const shown = plan.changes.filter((c) => c.kind !== "same");
      const same = plan.changes.length - shown.length;
      out(`${plan.harness}: ${plan.file.replace(home, "~")}`);
      for (const c of shown) out(`  ${sign[c.kind]} ${c.name}${c.note ? ` (${c.note})` : ""}`);
      out(`  ${same} unchanged${shown.length === 0 ? "; nothing to do" : ""}`);
    }
    for (const plan of plans) plan.commit();
    if (!dry && plans.some((p) => p.harness === "claude" || p.harness === "opencode")) {
      mkdirSync(dirname(statePath(home)), { recursive: true });
      writeFileSync(statePath(home), JSON.stringify(state, null, 2) + "\n");
    }
    if (dry) out("dry run: nothing written");
    return 0;
  } catch (e) {
    if (e instanceof UserError || (e as { code?: string }).code?.startsWith("ERR_PARSE_ARGS")) {
      err(`mcp-sync: ${(e as Error).message}`);
      return 2;
    }
    throw e;
  }
}

const entry = process.argv[1];
if (entry && import.meta.url === pathToFileURL(realpathSync(entry)).href) {
  process.exitCode = run(process.argv.slice(2));
}
