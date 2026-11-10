import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { codexServerToml, parseJsonc, removeTables, stripJsonc, toCodex, toOpencode } from "../mcp-sync.mts";

const SCRIPT = join(dirname(fileURLToPath(import.meta.url)), "..", "mcp-sync.mts");

function sandbox() {
  const home = mkdtempSync(join(tmpdir(), "mcp-sync-test-"));
  mkdirSync(join(home, ".codex"), { recursive: true });
  const env = { ...process.env, HOME: home, CODEX_HOME: "", XDG_STATE_HOME: "" };
  delete env.CODEX_HOME;
  delete env.XDG_STATE_HOME;
  delete env.XDG_CONFIG_HOME;
  const base = (name: string, body: string) => {
    const file = join(home, name);
    writeFileSync(file, body);
    return file;
  };
  const sync = (...args: string[]) => spawnSync(process.execPath, [SCRIPT, ...args], { env, encoding: "utf8" });
  const claude = () => JSON.parse(readFileSync(join(home, ".claude.json"), "utf8"));
  const codex = () => readFileSync(join(home, ".codex", "config.toml"), "utf8");
  const opencodeFile = join(home, ".config", "opencode", "opencode.json");
  const opencode = () => JSON.parse(readFileSync(opencodeFile, "utf8"));
  return { home, base, sync, claude, codex, opencode, opencodeFile };
}

test("jsonc: comments and trailing commas, but not inside strings", () => {
  const text = `{
    // line comment
    "url": "https://example.com/a//b", /* block */
    "list": [1, 2, // trailing
    ],
    "s": "a /* not a comment */",
  }`;
  assert.deepEqual(parseJsonc(text), { url: "https://example.com/a//b", list: [1, 2], s: "a /* not a comment */" });
  assert.equal(stripJsonc('"\\"//"'), '"\\"//"');
});

test("codex translation: env forwarding, bearer token, header refs", () => {
  const warnings: string[] = [];
  const warn = (m: string) => warnings.push(m);
  const stdio = toCodex("kitt", { command: "kitt", args: ["mcp"], env: { TOKEN_X: "${TOKEN_X}", REGION: "eu" } }, warn);
  assert.deepEqual(stdio, { command: "kitt", args: ["mcp"], env_vars: ["TOKEN_X"], env: { REGION: "eu" } });

  const http = toCodex("api", {
    type: "http", url: "https://x/mcp",
    headers: { Authorization: "Bearer ${API_TOKEN}", "X-Org": "${ORG}", "X-Static": "1" },
  }, warn);
  assert.deepEqual(http, {
    url: "https://x/mcp", bearer_token_env_var: "API_TOKEN",
    http_headers: { "X-Static": "1" }, env_http_headers: { "X-Org": "ORG" },
  });

  assert.throws(() => toCodex("bad", { command: "x", env: { A: "${B}" } }, warn), /own name/);
  assert.throws(() => toCodex("bad", { command: "x", args: ["${FOO}"] }, warn), /does not expand/);
  assert.throws(() => toCodex("bad", { type: "sse", url: "https://x" }, warn), /not supported/);

  const withOverride = toCodex("o", { url: "https://x", codex: { enabled_tools: ["a"], startup_timeout_sec: 30 } }, warn);
  assert.deepEqual(withOverride, { url: "https://x", enabled_tools: ["a"], startup_timeout_sec: 30 });

  toCodex("w", { url: "https://x", timeout: 5 }, warn);
  assert.match(warnings.join(), /"timeout" has no Codex mapping/);
});

test("toml: quoting and sub-tables", () => {
  const toml = codexServerToml("a.b", { command: "x", env: { K: "v\"q" }, enabled_tools: ["t"] });
  assert.equal(toml, '[mcp_servers."a.b"]\ncommand = "x"\nenabled_tools = ["t"]\n\n[mcp_servers."a.b".env]\nK = "v\\"q"');
});

test("removeTables drops named servers, their sub-tables and header comments, nothing else", () => {
  const input = [
    'model = "x"', "", "[mcp_servers.a]", 'url = "u"', "", "# about b", "[mcp_servers.b]", 'url = "v"',
    "[mcp_servers.b.env]", 'K = "1"', "", "# about features", "[features]", "on = true", "",
  ].join("\n");
  const out = removeTables(input, new Set(["b"]));
  assert.equal(out, [
    'model = "x"', "", "[mcp_servers.a]", 'url = "u"', "", "", "# about features", "[features]", "on = true", "",
  ].join("\n"));
});

test("secrets must be references", () => {
  const s = sandbox();
  const base = s.base("bad.jsonc", '{ "mcpServers": { "x": { "command": "x", "env": { "API_KEY": "abc123" } } } }');
  const r = s.sync(base, "--dry-run");
  assert.equal(r.status, 2);
  assert.match(r.stderr, /looks like a secret/);
});

test("extends merges, later entries win, null removes", () => {
  const s = sandbox();
  s.base("common.jsonc", '{ "mcpServers": { "a": { "url": "https://a" }, "b": { "url": "https://b" } } }');
  const work = s.base("work.jsonc", '{ "extends": "common.jsonc", "mcpServers": { "a": { "url": "https://a2" }, "b": null } }');
  const r = s.sync(work);
  assert.equal(r.status, 0, r.stderr);
  assert.deepEqual(Object.keys(s.claude().mcpServers), ["a"]);
  assert.equal(s.claude().mcpServers.a.url, "https://a2");
});

test("end to end: claude keeps unrelated keys, profile switch removes only managed servers", () => {
  const s = sandbox();
  writeFileSync(join(s.home, ".claude.json"), JSON.stringify({
    numStartups: 7, projects: { "/p": { mcpServers: { local: { type: "http", url: "https://l" } } } },
    mcpServers: { handmade: { type: "stdio", command: "mine" } },
  }));
  const work = s.base("work.jsonc", '{ "mcpServers": { "w": { "url": "https://w" }, "both": { "url": "https://both" } } }');
  const personal = s.base("personal.jsonc", '{ "mcpServers": { "p": { "command": "p" }, "both": { "url": "https://both" } } }');

  assert.equal(s.sync(work).status, 0);
  assert.deepEqual(Object.keys(s.claude().mcpServers).sort(), ["both", "handmade", "w"]);
  assert.ok(existsSync(join(s.home, ".claude.json.mcp-sync.bak")));

  assert.equal(s.sync(personal).status, 0);
  const after = s.claude();
  assert.deepEqual(Object.keys(after.mcpServers).sort(), ["both", "handmade", "p"]);
  assert.equal(after.numStartups, 7);
  assert.equal(after.projects["/p"].mcpServers.local.url, "https://l");

  const again = s.sync(personal);
  assert.match(again.stdout, /nothing to do/);
});

test("conflicts need --adopt; --dry-run writes nothing; harness filter", () => {
  const s = sandbox();
  writeFileSync(join(s.home, ".claude.json"), JSON.stringify({ mcpServers: { x: { type: "http", url: "https://old" } } }));
  writeFileSync(join(s.home, ".codex", "config.toml"), 'model = "m"\n\n[mcp_servers.x]\nurl = "https://old"\n\n[mcp_servers.keep]\nurl = "https://keep"\n');
  const base = s.base("b.jsonc", '{ "mcpServers": { "x": { "url": "https://new" }, "only-claude": { "url": "https://c", "harnesses": ["claude"] } } }');

  assert.equal(s.sync(base).status, 2);
  assert.equal(s.sync(base, "--adopt", "--dry-run").status, 0);
  assert.equal(s.claude().mcpServers.x.url, "https://old");

  assert.equal(s.sync(base, "--adopt", "--harness", "codex").status, 0);
  const toml = s.codex();
  assert.match(toml, /\[mcp_servers\.keep\]/);
  assert.match(toml, /url = "https:\/\/new"/);
  assert.doesNotMatch(toml, /only-claude/);
  assert.equal((toml.match(/\[mcp_servers\.x\]/g) ?? []).length, 1);
  assert.equal(s.claude().mcpServers.x.url, "https://old", "claude untouched when not targeted");
});

test("${HOME} expands at build time; other ${VAR} references stay for runtime in claude", () => {
  const s = sandbox();
  const base = s.base("b.jsonc", '{ "mcpServers": { "t": { "command": "node", "args": ["${HOME}/x.js"], "env": { "TODOIST_API_KEY": "${TODOIST_API_KEY}" } } } }');
  assert.equal(s.sync(base).status, 0);
  const t = s.claude().mcpServers.t;
  assert.equal(t.args[0], `${s.home}/x.js`);
  assert.equal(t.env.TODOIST_API_KEY, "${TODOIST_API_KEY}");
  assert.match(s.codex(), /env_vars = \["TODOIST_API_KEY"\]/);
});

test("a bare profile name resolves to mcp/<name>.jsonc in the repo, from any directory", () => {
  const s = sandbox();
  const r = spawnSync(process.execPath, [SCRIPT, "work", "--dry-run"], {
    cwd: s.home, encoding: "utf8", env: { ...process.env, HOME: s.home },
  });
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.stdout, /\+ zendesk-graphql/);
  const missing = s.sync("nope", "--dry-run");
  assert.equal(missing.status, 2);
  assert.match(missing.stderr, /not found/);
});

test("--prune removes servers outside the profile in both harnesses; --keep spares names", () => {
  const s = sandbox();
  writeFileSync(join(s.home, ".claude.json"), JSON.stringify({
    numStartups: 3, mcpServers: { old: { type: "http", url: "https://old" }, spare: { type: "http", url: "https://spare" } },
  }));
  writeFileSync(join(s.home, ".codex", "config.toml"),
    'model = "m"\n\n[mcp_servers.old]\nurl = "https://old"\n\n[mcp_servers.spare]\nurl = "https://spare"\n');
  const base = s.base("b.jsonc", '{ "mcpServers": { "new": { "url": "https://new" } } }');

  assert.equal(s.sync(base).status, 0);
  assert.deepEqual(Object.keys(s.claude().mcpServers).sort(), ["new", "old", "spare"], "no prune without the flag");

  const dry = s.sync(base, "--prune", "--keep", "spare", "--dry-run");
  assert.match(dry.stdout, /- old \(prune\)/);
  assert.doesNotMatch(dry.stdout, /spare/);
  assert.deepEqual(Object.keys(s.claude().mcpServers).sort(), ["new", "old", "spare"], "dry run writes nothing");

  assert.equal(s.sync(base, "--prune", "--keep", "spare").status, 0);
  assert.deepEqual(Object.keys(s.claude().mcpServers).sort(), ["new", "spare"]);
  assert.equal(s.claude().numStartups, 3);
  const toml = s.codex();
  assert.doesNotMatch(toml, /mcp_servers\.old/);
  assert.match(toml, /\[mcp_servers\.spare\]/);
  assert.match(toml, /\[mcp_servers\.new\]/);

  assert.equal(s.sync(base, "--keep", "x").status, 2);
});

test("opencode translation: local/remote shapes, {env:} syntax, oauth off with Authorization, overrides", () => {
  const warnings: string[] = [];
  const warn = (m: string) => warnings.push(m);
  assert.deepEqual(
    toOpencode("fs", { command: "npx", args: ["-y", "pkg", "--dir=${DIR}"], env: { TOKEN_X: "${TOKEN_X}", REGION: "eu" }, cwd: "/w" }, warn),
    { type: "local", command: ["npx", "-y", "pkg", "--dir={env:DIR}"], cwd: "/w", environment: { TOKEN_X: "{env:TOKEN_X}", REGION: "eu" } });
  assert.deepEqual(toOpencode("plain", { type: "http", url: "https://x/mcp" }, warn), { type: "remote", url: "https://x/mcp" });
  assert.deepEqual(
    toOpencode("api", { type: "http", url: "https://x/${REGION}/mcp", headers: { Authorization: "Bearer ${API_TOKEN}", "X-Org": "1" } }, warn),
    { type: "remote", url: "https://x/{env:REGION}/mcp", headers: { Authorization: "Bearer {env:API_TOKEN}", "X-Org": "1" }, oauth: false });
  assert.deepEqual(toOpencode("o", { url: "https://x", opencode: { disabled: true, oauth: false } }, warn),
    { type: "remote", url: "https://x", disabled: true, oauth: false });
  assert.throws(() => toOpencode("bad", { type: "sse", url: "https://x" }, warn), /not supported/);
  toOpencode("w", { url: "https://x", mystery: 1 }, warn);
  assert.equal(warnings.length, 1);
});

test("opencode end to end: only mcp.servers changes, profile switch and prune are tracked", () => {
  const s = sandbox();
  mkdirSync(dirname(s.opencodeFile), { recursive: true });
  writeFileSync(s.opencodeFile, JSON.stringify({
    $schema: "https://opencode.ai/config.json", model: "m", mcp: { timeout: { startup: 1000 }, servers: { hand: { type: "remote", url: "https://hand" } } },
  }));
  const a = s.base("a.jsonc", '{ "mcpServers": { "one": { "url": "https://one" }, "two": { "command": "x", "args": ["${HOME}/y"] }, "cx": { "url": "https://cx", "harnesses": ["codex"] } } }');
  const b = s.base("b.jsonc", '{ "mcpServers": { "one": { "url": "https://one" } } }');

  assert.equal(s.sync(a).status, 0);
  let cfg = s.opencode();
  assert.equal(cfg.model, "m");
  assert.deepEqual(cfg.mcp.timeout, { startup: 1000 });
  assert.deepEqual(Object.keys(cfg.mcp.servers).sort(), ["hand", "one", "two"], "harnesses tag keeps cx out");
  assert.deepEqual(cfg.mcp.servers.two.command, ["x", s.home + "/y"]);

  assert.equal(s.sync(b).status, 0);
  assert.deepEqual(Object.keys(s.opencode().mcp.servers).sort(), ["hand", "one"], "only managed servers are removed");

  assert.equal(s.sync(b, "--prune").status, 0);
  assert.deepEqual(Object.keys(s.opencode().mcp.servers), ["one"]);

  const after = s.opencode();
  after.mcp.servers.hand = { type: "remote", url: "https://hand" };
  writeFileSync(s.opencodeFile, JSON.stringify(after));
  const conflict = s.base("c.jsonc",'{ "mcpServers": { "hand": { "url": "https://other" } } }');
  assert.equal(s.sync(conflict, "--harness", "opencode").status, 2);
  assert.equal(s.sync(conflict, "--harness", "opencode", "--adopt").status, 0);
  assert.equal(s.opencode().mcp.servers.hand.url, "https://other");
});

test("codex: a table an app inserted inside the managed block is kept, not deleted", () => {
  const s = sandbox();
  const base = s.base("b.jsonc", '{ "mcpServers": { "one": { "url": "https://one" } } }');
  assert.equal(s.sync(base, "--harness", "codex").status, 0);
  assert.match(s.codex(), /# BEGIN mcp-sync \(from b\.jsonc\) names=one;/);

  // The app writes its own table (with a sub-table) just before our END marker.
  const withApp = s.codex().replace("# END mcp-sync", '[mcp_servers.app_tool]\ncommand = "/app/bin/tool"\n\n[mcp_servers.app_tool.env]\nA = "1"\n# END mcp-sync');
  writeFileSync(join(s.home, ".codex", "config.toml"), withApp);

  const dry = s.sync(base, "--harness", "codex", "--dry-run");
  assert.match(dry.stdout, /~ app_tool \(moved out of the managed block\)/);
  assert.doesNotMatch(dry.stdout, /- app_tool/);

  assert.equal(s.sync(base, "--harness", "codex").status, 0);
  const toml = s.codex();
  const block = toml.slice(toml.indexOf("# BEGIN mcp-sync"), toml.indexOf("# END mcp-sync"));
  assert.doesNotMatch(block, /app_tool/);
  assert.match(toml, /\[mcp_servers\.app_tool\]\ncommand = "\/app\/bin\/tool"/);
  assert.match(toml, /\[mcp_servers\.app_tool\.env\]\nA = "1"/);

  assert.equal(s.sync(base, "--harness", "codex").status, 0);
  assert.equal(s.codex(), toml, "second run is a no-op");
  assert.match(s.sync(base, "--harness", "codex", "--prune").stdout, /- app_tool \(prune\)/);
});

test("codex: a block from before names were recorded is not trusted to delete unknown tables", () => {
  const s = sandbox();
  writeFileSync(join(s.home, ".codex", "config.toml"),
    'model = "m"\n\n# BEGIN mcp-sync (from old.jsonc); generated, edits here are overwritten\n[mcp_servers.one]\nurl = "https://one"\n\n[mcp_servers.stale]\nurl = "https://stale"\n# END mcp-sync\n');
  const base = s.base("b.jsonc", '{ "mcpServers": { "one": { "url": "https://one" } } }');
  assert.equal(s.sync(base, "--harness", "codex").status, 0, "no --adopt needed for our own legacy entries");
  assert.match(s.codex(), /\[mcp_servers\.stale\]/);
  assert.match(s.codex(), /names=one;/);
});
