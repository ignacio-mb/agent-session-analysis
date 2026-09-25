// Named, throwaway Metabase instances on Docker, for testing the robot data engineer (~/dev/mba).
// Run from this directory: `bun server.ts`, then open http://localhost:4000. No dependencies:
// Bun serves the page and talks to the Docker Engine API over its unix socket and to Postgres with Bun.SQL.
import { SQL } from "bun";
import { existsSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { connect } from "node:net";
import { homedir } from "node:os";

const APP_PORT = Number(process.env.PORT) || 4000;
const FIRST_PORT = Number(process.env.FIRST_PORT) || 3200;
const DEFAULT_IMAGE = "metabase/metabase-dev:transform-tests-ee"; // what `rde init` runs
// The admin every new instance gets: yours, from .env (see .env.example).
const ADMIN = {
  email: process.env.LAB_ADMIN_EMAIL || "yourname@metabase.com",
  password: process.env.LAB_ADMIN_PASSWORD || "metabot1",
};
const LICENSE = process.env.MB_PREMIUM_EMBEDDING_TOKEN || process.env.RDE_LICENSE_TOKEN || "";
const STATE_FILE = `${import.meta.dir}/state.json`;
// A hostname label (the instance lives at <name>.localhost), and no ".", so `mbo-<name>.postgres` never names an instance.
const NAME = /^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$/;
const LABEL = { name: "mbo.name", port: "mbo.port", image: "mbo.image", db: "mbo.db", databases: "mbo.databases" };
const READY_TIMEOUT_MS = 15 * 60_000;

// Every instance gets its own Postgres, on its port + 10000, holding the databases below, each connected in Metabase.
// Metabase reads through a read-only role and writes (transforms, uploads, actions) through the writable connection.
// The image is built from db/: the Sample Database image `rde init` uses, with dba.stackexchange.com baked in.
const PG = {
  portOffset: 10_000,
  owner: "metabase",
  reader: "metabase_readonly",
  password: "metasample123",
};
const DATABASES = [
  { dbname: "sample", name: "Sample Database", description: null },
  {
    dbname: "stackexchange",
    name: "DBA Stack Exchange",
    description:
      "dba.stackexchange.com, the Database Administrators Q&A site: its users, questions and answers, tags, comments, " +
      "votes, badges and edit history from January 2011 to March 2024. Real data from Stack Exchange's data dump of " +
      "2024-04-06, licensed CC BY-SA.",
  },
];
type Database = (typeof DATABASES)[number];
const readOnlyRole = (dbname: string) => `
  DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${PG.reader}') THEN
      CREATE ROLE ${PG.reader} LOGIN PASSWORD '${PG.password}';
    END IF;
  END $$;
  GRANT CONNECT ON DATABASE ${dbname} TO ${PG.reader};
  GRANT USAGE ON SCHEMA public TO ${PG.reader};
  GRANT SELECT ON ALL TABLES IN SCHEMA public TO ${PG.reader};
  -- what the owner creates later through the writable connection stays readable, new schemas included
  ALTER DEFAULT PRIVILEGES FOR ROLE ${PG.owner} GRANT SELECT ON TABLES TO ${PG.reader};
  ALTER DEFAULT PRIVILEGES FOR ROLE ${PG.owner} GRANT USAGE ON SCHEMAS TO ${PG.reader};
`;

// The build context, the files in db/, and the image tag it gives: its content, so changing db/ builds a new image.
const DB_DIR = `${import.meta.dir}/db`;
const DB_CONTEXT = Object.fromEntries(
  readdirSync(DB_DIR, { withFileTypes: true })
    .filter((entry) => entry.isFile() && !entry.name.startsWith("."))
    .map((entry) => entry.name)
    .sort()
    .map((file) => [file, readFileSync(`${DB_DIR}/${file}`)]),
);
const contextHash = new Bun.CryptoHasher("sha256");
for (const [file, bytes] of Object.entries(DB_CONTEXT)) contextHash.update(file).update(bytes);
const PG_IMAGE = `mbo-postgres:${contextHash.digest("hex").slice(0, 12)}`;

const SOCKET = [
  process.env.DOCKER_HOST?.replace(/^unix:\/\//, ""),
  `${homedir()}/.docker/run/docker.sock`,
  "/var/run/docker.sock",
  `${homedir()}/.colima/default/docker.sock`,
  `${homedir()}/.orbstack/run/docker.sock`,
].find((path) => path && existsSync(path));
if (!SOCKET) throw new Error("No Docker socket found. Is Docker running? Or set DOCKER_HOST=unix:///path/to/docker.sock");

class HttpError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

interface Container {
  Id: string;
  State: string;
  Labels: Record<string, string>;
  Ports?: { PublicPort?: number }[];
}

interface Task {
  step: string;
  since: number;
  error?: string;
  port?: number;
  image?: string;
  abort: AbortController;
}

interface Saved {
  apiKey: string;
  note: string | null;
}

// Metabase can't show an API key again, so what setup produced lives here, by container id.
const state: Record<string, Saved> = existsSync(STATE_FILE) ? JSON.parse(readFileSync(STATE_FILE, "utf8")) : {};
const saveState = () => writeFileSync(STATE_FILE, JSON.stringify(state, null, 2), { mode: 0o600 });

const dbPortOf = (port: number) => port + PG.portOffset;
// A hostname per instance: browsers keep cookies per host, not per port, so signing in to one never signs out another.
const siteUrl = (name: string, port: number) => `http://${name}.localhost:${port}`;

// ---- Docker ---------------------------------------------------------------------------------

// A body is sent as JSON, or as is when it's bytes: a tar, the build context.
async function docker(method: string, path: string, body?: unknown, signal?: AbortSignal) {
  const tar = body instanceof Uint8Array;
  const res = await fetch(`http://docker${path}`, {
    method,
    unix: SOCKET,
    signal,
    headers: body === undefined ? {} : { "content-type": tar ? "application/x-tar" : "application/json" },
    body: body === undefined ? undefined : tar ? body : JSON.stringify(body),
  });
  if (res.status >= 400) {
    const text = await res.text();
    let message = text;
    try {
      message = JSON.parse(text).message ?? text;
    } catch {}
    throw new HttpError(res.status, `Docker: ${message.trim()}`);
  }
  return res;
}

async function labeled(label: string): Promise<Container[]> {
  const filters = encodeURIComponent(JSON.stringify({ label: [label] }));
  return (await docker("GET", `/containers/json?all=1&filters=${filters}`)).json();
}

const managed = () => labeled(LABEL.name);
const find = async (name: string) => (await labeled(`${LABEL.name}=${name}`))[0];
const findDb = async (name: string) => (await labeled(`${LABEL.db}=${name}`))[0];

async function mustFind(name: string) {
  const container = await find(name);
  if (!container) throw new HttpError(404, `No instance named "${name}"`);
  return container;
}

async function ensureImage(task: Task, image: string) {
  if ((await fetch(`http://docker/images/${image}/json`, { unix: SOCKET })).ok) return;
  step(task, `pulling ${image}`);
  const at = image.lastIndexOf("@");
  const colon = image.lastIndexOf(":");
  const [from, tag] =
    at > 0 ? [image.slice(0, at), image.slice(at + 1)]
    : colon > image.lastIndexOf("/") ? [image.slice(0, colon), image.slice(colon + 1)]
    : [image, "latest"];
  const query = `fromImage=${encodeURIComponent(from)}&tag=${encodeURIComponent(tag)}`;
  const res = await docker("POST", `/images/create?${query}`, undefined, task.abort.signal);
  // A failed pull still answers 200; the error is the last line of the progress stream.
  const failure = (await res.text())
    .split("\n")
    .map((line) => {
      try {
        return JSON.parse(line).error;
      } catch {
        return undefined;
      }
    })
    .find(Boolean);
  if (failure) throw new Error(failure);
}

// The Postgres image is built here, from db/, the first time an instance needs it: a few minutes, most of them
// downloading the Stack Exchange dump. Instances created meanwhile wait for the same build.
let building: Promise<void> | undefined;
let buildStep = "";

async function ensurePostgresImage(task: Task) {
  if ((await fetch(`http://docker/images/${PG_IMAGE}/json`, { unix: SOCKET })).ok) return;
  if (!building) {
    building = buildPostgresImage().finally(() => (building = undefined));
    building.catch(() => {}); // failures reach every waiting task below, even if all of them were deleted
  }
  const done = building.then(() => true);
  for (;;) {
    step(task, `building the Postgres image, first time only: ${buildStep}`);
    if (await Promise.race([done, Bun.sleep(1000).then(() => false)])) return;
  }
}

async function buildPostgresImage() {
  buildStep = "starting";
  const context = await new Bun.Archive(DB_CONTEXT).bytes();
  const res = await docker("POST", `/build?t=${encodeURIComponent(PG_IMAGE)}&rm=1&forcerm=1`, context);
  // The output streams as JSON lines. A failed build still answers 200: its error is a line of the stream.
  let output = "";
  const failed = (error: string) =>
    new Error(`Building ${PG_IMAGE}: ${error}\n${output.trim().split("\n").slice(-10).join("\n")}`);
  let pending = "";
  for await (const chunk of res.body!.pipeThrough(new TextDecoderStream())) {
    const lines = (pending + chunk).split("\n");
    pending = lines.pop()!;
    for (const line of lines.filter(Boolean)) {
      const { stream, error } = JSON.parse(line);
      if (error) throw failed(error);
      if (!stream) continue;
      output += stream;
      const at = /^Step (\d+\/\d+) : (.*)/.exec(stream);
      if (at) buildStep = `step ${at[1]}, ${at[2].slice(0, 60)}`;
    }
  }
  if (!(await fetch(`http://docker/images/${PG_IMAGE}/json`, { unix: SOCKET })).ok) throw failed("no image came out");
}

function metabaseSpec(name: string, image: string, port: number) {
  const env = {
    MB_SITE_URL: siteUrl(name, port),
    MB_LOAD_SAMPLE_CONTENT: "false", // no H2 Sample Database, no example collection
    MB_WAREHOUSE_ALLOWED_NETWORKS: "allow-all", // warehouses on localhost / host.docker.internal
    MB_RUN_MODE: "e2e", // with MB_STORE_USE_STAGING: the token store `rde init` uses
    MB_STORE_USE_STAGING: "true",
    MB_ANON_TRACKING_ENABLED: "false",
    MB_CHECK_FOR_UPDATES: "false",
    ...(LICENSE && { MB_PREMIUM_EMBEDDING_TOKEN: LICENSE }),
  };
  return {
    Image: image,
    Labels: { [LABEL.name]: name, [LABEL.port]: String(port), [LABEL.image]: image },
    Env: Object.entries(env).map(([key, value]) => `${key}=${value}`),
    ExposedPorts: { "3000/tcp": {} },
    HostConfig: {
      PortBindings: { "3000/tcp": [{ HostIp: "127.0.0.1", HostPort: String(port) }] },
      ExtraHosts: ["host.docker.internal:host-gateway"],
    },
  };
}

function postgresSpec(name: string, port: number) {
  const dbPort = String(dbPortOf(port));
  return {
    Image: PG_IMAGE,
    Labels: { [LABEL.db]: name, [LABEL.port]: dbPort, [LABEL.databases]: DATABASES.map((d) => d.dbname).join(",") },
    ExposedPorts: { "5432/tcp": {} },
    HostConfig: { PortBindings: { "5432/tcp": [{ HostIp: "127.0.0.1", HostPort: dbPort }] } },
  };
}

// The databases a Postgres container holds. Those created before db/ existed hold the Sample Database only.
function databasesIn(db: Container | undefined): Database[] {
  if (!db) return [];
  const names = (db.Labels[LABEL.databases] ?? "sample").split(",");
  return DATABASES.filter((d) => names.includes(d.dbname));
}

async function createAndStart(containerName: string, spec: object): Promise<string> {
  const { Id } = await (await docker("POST", `/containers/create?name=${containerName}`, spec)).json();
  await docker("POST", `/containers/${Id}/start`);
  return Id;
}

async function launchPostgres(task: Task, name: string, port: number) {
  await ensurePostgresImage(task);
  step(task, "creating containers");
  return createAndStart(`mbo-${name}.postgres`, postgresSpec(name, port));
}

// A fresh pair: Postgres first, then Metabase. Returns the Metabase container id.
async function launch(task: Task, name: string, image: string, port: number) {
  await launchPostgres(task, name, port);
  return createAndStart(`mbo-${name}`, metabaseSpec(name, image, port));
}

async function drop(name: string) {
  for (const container of [await find(name), await findDb(name)]) {
    if (!container) continue;
    await docker("DELETE", `/containers/${container.Id}?force=1&v=1`);
    if (state[container.Id]) {
      delete state[container.Id];
      saveState();
    }
  }
}

const isListening = (port: number) =>
  new Promise<boolean>((resolve) => {
    const socket = connect(port, "127.0.0.1");
    socket.setTimeout(500);
    socket.once("connect", () => (socket.destroy(), resolve(true)));
    socket.once("timeout", () => (socket.destroy(), resolve(false)));
    socket.once("error", () => resolve(false));
  });

// The first port from FIRST_PORT that is free for Metabase, with port + 10000 free for its Postgres.
async function freePort() {
  const all: Container[] = await (await docker("GET", "/containers/json?all=1")).json();
  const taken = new Set([
    ...all.flatMap((c) => (c.Ports ?? []).map((p) => p.PublicPort)),
    ...all.map((c) => Number(c.Labels?.[LABEL.port])), // stopped instances keep their ports
    ...[...tasks.values()].flatMap((t) => (t.port ? [t.port, dbPortOf(t.port)] : [])),
  ]);
  for (let port = FIRST_PORT; dbPortOf(port) < 65536; port++) {
    if (taken.has(port) || taken.has(dbPortOf(port))) continue;
    if (!(await isListening(port)) && !(await isListening(dbPortOf(port)))) return port;
  }
  throw new HttpError(503, "No free port");
}

// ---- Metabase -------------------------------------------------------------------------------

async function metabase(base: string, method: string, path: string, body?: unknown, session?: string) {
  const res = await fetch(base + path, {
    method,
    headers: { "content-type": "application/json", ...(session && { "x-metabase-session": session }) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  if (!res.ok) throw new Error(`${method} ${path} → ${res.status}: ${text.slice(0, 300)}`);
  return text ? JSON.parse(text) : null;
}

const isUp = (url: string) => fetch(url, { signal: AbortSignal.timeout(1500) }).then((r) => r.ok, () => false);

async function waitReady(task: Task, id: string, base: string) {
  const deadline = Date.now() + READY_TIMEOUT_MS;
  while (Date.now() < deadline) {
    task.abort.signal.throwIfAborted();
    const { State } = await (await docker("GET", `/containers/${id}/json`)).json();
    if (!State.Running) throw new Error(`Container exited (code ${State.ExitCode}), see logs`);
    // /api/health goes green a little before the API reliably answers.
    if ((await isUp(`${base}/api/health`)) && (await isUp(`${base}/api/session/properties`))) return;
    await Bun.sleep(1000);
  }
  throw new Error("Metabase did not come up in 15 minutes, see logs");
}

async function createReadOnlyRole(task: Task, port: number, dbname: string) {
  const url = `postgres://${PG.owner}:${PG.password}@127.0.0.1:${dbPortOf(port)}/${dbname}`;
  for (let attempt = 1; ; attempt++) {
    const sql = new SQL({ url, max: 1 });
    try {
      return await sql.unsafe(readOnlyRole(dbname));
    } catch (error) {
      if (attempt === 30) throw new Error(`Postgres on :${dbPortOf(port)}: ${error instanceof Error ? error.message : error}`);
    } finally {
      await sql.close();
    }
    task.abort.signal.throwIfAborted();
    await Bun.sleep(1000);
  }
}

const details = (port: number, dbname: string, user: string) => ({
  host: "host.docker.internal",
  port: dbPortOf(port),
  dbname,
  user,
  password: PG.password,
  ssl: false,
});

// Returns a note when the instance can't have writable connections.
async function connectDatabases(task: Task, base: string, session: string, port: number, databases: Database[]) {
  const call = (method: string, path: string, body?: unknown) => metabase(base, method, path, body, session);
  // Default privileges are per database, so the read-only role gets its grants in each.
  for (const { dbname } of databases) await createReadOnlyRole(task, port, dbname);
  const features = (await call("GET", "/api/session/properties"))["token-features"] ?? {};
  const writable = Boolean(features.writable_connection || features["writable-connection"]);
  // The main connection is final from the start: changing it closes its pool and aborts a running sync.
  // Without a writable connection it keeps the owner, so writes still work.
  const { data } = await call("GET", "/api/database");
  const connected: { id: number; database: Database }[] = [];
  for (const database of databases) {
    const db =
      data.find((d: { name: string }) => d.name === database.name) ??
      (await call("POST", "/api/database", {
        engine: "postgres",
        name: database.name,
        details: details(port, database.dbname, writable ? PG.reader : PG.owner),
      }));
    connected.push({ id: db.id, database });
  }
  for (const { id, database } of connected) {
    step(task, `syncing ${database.name}`);
    for (let second = 0; ; second++) {
      const status = (await call("GET", `/api/database/${id}`)).initial_sync_status;
      if (status === "complete") break;
      if (status === "aborted") throw new Error(`The first sync of ${database.name} was aborted, see logs`);
      if (second === 300) throw new Error(`The first sync of ${database.name} took over 5 minutes, see logs`);
      task.abort.signal.throwIfAborted();
      await Bun.sleep(1000);
    }
  }
  step(task, writable ? "adding the writable connections" : "describing the databases");
  for (const { id, database } of connected) {
    // Creating a database ignores its description. write_data_details overlays the main details and must carry the
    // marker; Metabase tests it before saving.
    const changes = {
      ...(database.description && { description: database.description }),
      ...(writable && {
        write_data_details: { ...details(port, database.dbname, PG.owner), "write-data-connection": true },
      }),
    };
    if (Object.keys(changes).length) await call("PUT", `/api/database/${id}`, changes);
  }
  return writable
    ? null
    : `No writable connection: it needs a license token with the writable-connection feature${LICENSE ? "" : " (none set)"}.`;
}

async function setUp(task: Task, name: string, id: string, port: number) {
  const base = `http://127.0.0.1:${port}`;
  step(task, "starting Metabase");
  await waitReady(task, id, base);
  step(task, "creating the admin");
  const properties = await metabase(base, "GET", "/api/session/properties");
  if (!properties["has-user-setup"]) {
    await metabase(base, "POST", "/api/setup", {
      token: properties["setup-token"],
      user: { email: ADMIN.email, password: ADMIN.password },
      prefs: { site_name: name },
    });
  }
  const session = (await metabase(base, "POST", "/api/session", { username: ADMIN.email, password: ADMIN.password })).id;
  // A per-user setting; its default, "auto", follows the OS into dark mode.
  await metabase(base, "PUT", "/api/setting/color-scheme", { value: "light" }, session);
  step(task, "connecting the databases");
  const note = await connectDatabases(task, base, session, port, databasesIn(await findDb(name)));
  step(task, "creating an API key");
  const groups = await metabase(base, "GET", "/api/permissions/group", undefined, session);
  const admins = groups.find?.((g: { name: string }) => g.name === "Administrators")?.id ?? 2;
  const key = await metabase(base, "POST", "/api/api-key", { name: `mbo-${Date.now()}`, group_id: admins }, session);
  state[id] = { apiKey: key.unmasked_key, note };
  saveState();
}

// ---- Background tasks -----------------------------------------------------------------------

const tasks = new Map<string, Task>();

function run(name: string, info: Pick<Task, "step" | "port" | "image">, job: (task: Task) => Promise<unknown>) {
  const task: Task = { ...info, since: Date.now(), abort: new AbortController() };
  tasks.set(name, task);
  job(task).then(
    () => tasks.get(name) === task && tasks.delete(name),
    (error) => {
      if (tasks.get(name) === task) task.error = error instanceof Error ? error.message : String(error);
    },
  );
}

function step(task: Task, text: string) {
  task.abort.signal.throwIfAborted();
  task.step = text;
  task.since = Date.now();
}

function assertIdle(name: string) {
  const task = tasks.get(name);
  if (task && !task.error) throw new HttpError(409, `"${name}" is busy: ${task.step}`);
}

let queue: Promise<unknown> = Promise.resolve();
function serially<T>(fn: () => Promise<T>): Promise<T> {
  const next = queue.then(fn);
  queue = next.catch(() => {});
  return next;
}

// ---- Instances ------------------------------------------------------------------------------

interface Facts {
  container: boolean;
  postgres: Database[];
  running: boolean;
  healthy: boolean;
  apiKey: string | null;
  note: string | null;
}

function view(name: string, port: number | undefined, image: string | undefined, task: Task | undefined, facts: Facts) {
  const status = task ? (task.error ? "error" : "busy") : !facts.running ? "stopped" : facts.healthy ? "ready" : "starting";
  return {
    ...facts,
    name,
    url: port ? siteUrl(name, port) : null,
    // Each database's URL, as its owner.
    postgres: port && facts.postgres.length
      ? facts.postgres.map((d) => ({ name: d.name, url: `postgres://${PG.owner}:${PG.password}@localhost:${dbPortOf(port)}/${d.dbname}` }))
      : null,
    image: image ?? null,
    state: status,
    detail: task?.error ?? task?.step ?? null,
    since: task && !task.error ? task.since : null,
  };
}

async function list() {
  const postgres = new Map((await labeled(LABEL.db)).map((c) => [c.Labels[LABEL.db], c]));
  const rows = await Promise.all(
    (await managed()).map(async (c) => {
      const name = c.Labels[LABEL.name];
      const port = Number(c.Labels[LABEL.port]);
      const task = tasks.get(name);
      const running = c.State === "running";
      const healthy = running && !task && (await isUp(`http://127.0.0.1:${port}/api/health`));
      const saved = state[c.Id];
      const facts = {
        container: true,
        postgres: databasesIn(postgres.get(name)),
        running,
        healthy,
        apiKey: saved?.apiKey ?? null,
        note: saved?.note ?? null,
      };
      return view(name, port, c.Labels[LABEL.image], task, facts);
    }),
  );
  for (const [name, task] of tasks) {
    if (!rows.some((row) => row.name === name)) {
      const facts = { container: false, postgres: databasesIn(postgres.get(name)), running: false, healthy: false, apiKey: null, note: null };
      rows.push(view(name, task.port, task.image, task, facts));
    }
  }
  return rows.sort((a, b) => a.name.localeCompare(b.name));
}

function create(body: { name?: unknown; image?: unknown } | null) {
  const name = String(body?.name ?? "").trim();
  const image = String(body?.image ?? "").trim() || DEFAULT_IMAGE;
  if (!NAME.test(name)) throw new HttpError(400, "Name: up to 40 lowercase letters, digits or '-', like a hostname");
  if (/\s/.test(image)) throw new HttpError(400, "Image: no spaces");
  return serially(async () => {
    if (tasks.has(name) || (await find(name)) || (await findDb(name))) throw new HttpError(409, `"${name}" already exists`);
    const port = await freePort();
    run(name, { step: "checking images", port, image }, async (task) => {
      await ensureImage(task, image);
      await setUp(task, name, await launch(task, name, image, port), port);
    });
    return { name, url: siteUrl(name, port) };
  });
}

async function act(name: string, action: string) {
  const c = await mustFind(name);
  assertIdle(name);
  const port = Number(c.Labels[LABEL.port]);
  const image = c.Labels[LABEL.image];
  const info = { port, image };
  switch (action) {
    case "start": // also retries a failed setup on a running container
      return run(name, { step: "starting containers", ...info }, async (task) => {
        const db = await findDb(name);
        await (db ? docker("POST", `/containers/${db.Id}/start`) : launchPostgres(task, name, port));
        await docker("POST", `/containers/${c.Id}/start`);
        if (!state[c.Id]) await setUp(task, name, c.Id, port);
      });
    case "stop":
      return run(name, { step: "stopping", ...info }, async () => {
        await docker("POST", `/containers/${c.Id}/stop?t=10`);
        const db = await findDb(name);
        if (db) await docker("POST", `/containers/${db.Id}/stop?t=10`);
      });
    case "reset": // new containers on the same name and ports: a fresh application database and Postgres
      return run(name, { step: "wiping", ...info }, async (task) => {
        await drop(name);
        await setUp(task, name, await launch(task, name, image, port), port);
      });
    default:
      throw new HttpError(404, `Unknown action "${action}"`);
  }
}

async function remove(name: string) {
  tasks.get(name)?.abort.abort();
  tasks.delete(name);
  await drop(name);
}

async function logs(name: string) {
  const c = await mustFind(name);
  const buffer = await (await docker("GET", `/containers/${c.Id}/logs?stdout=1&stderr=1&tail=2000`)).arrayBuffer();
  // Without a TTY, Docker frames the stream: an 8-byte header (stream, 0, 0, 0, size) per chunk.
  const view = new DataView(buffer);
  const chunks: ArrayBuffer[] = [];
  for (let i = 0; i + 8 <= buffer.byteLength; i += 8 + view.getUint32(i + 4)) {
    chunks.push(buffer.slice(i + 8, i + 8 + view.getUint32(i + 4)));
  }
  return new Response(new Blob(chunks), { headers: { "content-type": "text/plain; charset=utf-8" } });
}

async function images() {
  const local: { RepoTags?: string[] }[] = await (await docker("GET", "/images/json")).json();
  const tags = local.flatMap((image) => image.RepoTags ?? []).filter((tag) => tag.startsWith("metabase/metabase"));
  const suggestions = [DEFAULT_IMAGE, "metabase/metabase-enterprise:latest", "metabase/metabase-enterprise-head:latest"];
  return [...new Set([...suggestions, ...tags])].sort();
}

// ---- HTTP -----------------------------------------------------------------------------------

const HOSTS = new Set([`localhost:${APP_PORT}`, `127.0.0.1:${APP_PORT}`]);

// Only this page may drive Docker: refuse other sites (Origin) and DNS-rebound names (Host).
function route(handler: (req: Request & { params: Record<string, string> }) => Promise<Response> | Response) {
  return async (req: Request & { params: Record<string, string> }) => {
    try {
      const origin = req.headers.get("origin");
      const foreign = origin !== null && !HOSTS.has(origin.replace(/^https?:\/\//, ""));
      if (!HOSTS.has(req.headers.get("host") ?? "") || foreign) throw new HttpError(403, "Forbidden");
      return await handler(req);
    } catch (error) {
      const status = error instanceof HttpError ? error.status : 500;
      return Response.json({ error: error instanceof Error ? error.message : String(error) }, { status });
    }
  };
}

const accepted = () => Response.json({ ok: true }, { status: 202 });

Bun.serve({
  hostname: "127.0.0.1",
  port: APP_PORT,
  idleTimeout: 120, // Bun's 10s default drops requests while a busy Docker is slow to answer
  routes: {
    "/": route(() => new Response(Bun.file(`${import.meta.dir}/index.html`))),
    "/api/info": {
      GET: route(async () => {
        const { Version } = await (await docker("GET", "/version")).json();
        return Response.json({ docker: Version, license: Boolean(LICENSE), admin: ADMIN, defaultImage: DEFAULT_IMAGE });
      }),
    },
    "/api/images": { GET: route(async () => Response.json(await images())) },
    "/api/instances": {
      GET: route(async () => Response.json(await list())),
      POST: route(async (req) => Response.json(await create(await req.json().catch(() => null)), { status: 202 })),
    },
    "/api/instances/:name": {
      DELETE: route(async (req) => (await remove(req.params.name), accepted())),
    },
    "/api/instances/:name/:action": {
      GET: route((req) => {
        if (req.params.action !== "logs") throw new HttpError(404, "Not found");
        return logs(req.params.name);
      }),
      POST: route(async (req) => (await act(req.params.name, req.params.action), accepted())),
    },
  },
  fetch: () => new Response("Not found", { status: 404 }),
});

// Pick up where a restart left off: forget state of containers that are gone, drop a Postgres whose
// Metabase never got created, and finish setups cut short.
const existing = await managed();
const ids = new Set(existing.map((c) => c.Id));
if (Object.keys(state).some((id) => !ids.has(id))) {
  for (const id of Object.keys(state)) if (!ids.has(id)) delete state[id];
  saveState();
}
const names = new Set(existing.map((c) => c.Labels[LABEL.name]));
for (const db of await labeled(LABEL.db)) {
  if (!names.has(db.Labels[LABEL.db])) await docker("DELETE", `/containers/${db.Id}?force=1&v=1`);
}
for (const c of existing) {
  if (c.State === "running" && !state[c.Id]) {
    const name = c.Labels[LABEL.name];
    const port = Number(c.Labels[LABEL.port]);
    run(name, { step: "resuming setup", port, image: c.Labels[LABEL.image] }, (task) => setUp(task, name, c.Id, port));
  }
}

console.log(`Metabase instances → http://localhost:${APP_PORT}  (docker: ${SOCKET}, license token: ${LICENSE ? "set" : "not set"})`);
