// Metadata-only supplement to weather-daily.yml. GPV and rendering stay in Actions.
export const WORKFLOW = "weather-daily.yml";
export const API_VERSION = "2026-03-10";
export const STATUS_LIMIT = 4096;
export const HISTORY_LIMIT = 30;
export const HISTORY_BYTES = 256 * 1024;
export const COOLDOWN_MS = 15 * 60_000;
export const MAX_DELAY_MS = 15 * 60_000;
export const SLOT_REQUEST_LIMIT = 2;
const SKEW_MS = 60_000;
const JST = 9 * 3600_000;
const ACTIVE = ["queued", "in_progress", "waiting", "pending", "requested"];
const decoder = new TextDecoder("utf-8", { fatal: true });
const noDiagnostics = () => {};

export class Fault extends Error {}
function requireThat(value, code) { if (!value) throw new Fault(code); }
function plain(value) { return value !== null && typeof value === "object" && !Array.isArray(value); }
const jstDate = (instant) => new Date(instant + JST).toISOString().slice(0, 10);

function isoDay(day) {
  requireThat(typeof day === "string" && /^\d{4}-\d{2}-\d{2}$/.test(day), "INVALID_DATE");
  const instant = Date.parse(`${day}T00:00:00+09:00`);
  requireThat(Number.isFinite(instant) && jstDate(instant) === day, "INVALID_DATE");
  return instant;
}

function jstTimestamp(value) {
  requireThat(typeof value === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?\+09:00$/.test(value), "INVALID_TIMESTAMP");
  const day = value.slice(0, 10);
  isoDay(day);
  const instant = Date.parse(value);
  requireThat(Number.isFinite(instant), "INVALID_TIMESTAMP");
  const normalized = new Date(instant + JST).toISOString();
  requireThat(normalized.slice(0, 19) === value.slice(0, 19), "INVALID_TIMESTAMP");
  return instant;
}

function utcTimestamp(value) {
  requireThat(typeof value === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/.test(value), "UNKNOWN_ACTIONS");
  const instant = Date.parse(value);
  requireThat(Number.isFinite(instant) && new Date(instant).toISOString().slice(0, 19) === value.slice(0, 19), "UNKNOWN_ACTIONS");
  return instant;
}

function configuration(env) {
  requireThat(env.MODE === "observe" || env.MODE === "dispatch", "INVALID_MODE");
  requireThat(typeof env.GITHUB_REPOSITORY === "string" &&
    /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(env.GITHUB_REPOSITORY) &&
    env.GITHUB_REPOSITORY !== "OWNER/REPOSITORY", "INVALID_REPOSITORY");
  requireThat(typeof env.GITHUB_BRANCH === "string" && /^[A-Za-z0-9_./-]{1,120}$/.test(env.GITHUB_BRANCH) &&
    !env.GITHUB_BRANCH.includes(".."), "INVALID_BRANCH");
  let statusURL;
  try { statusURL = new URL(env.STATUS_URL); } catch { throw new Fault("INVALID_STATUS_URL"); }
  requireThat(statusURL.protocol === "https:" && statusURL.pathname === "/status.json" &&
    !statusURL.username && !statusURL.password && !statusURL.search && !statusURL.hash &&
    !statusURL.hostname.endsWith(".invalid"), "INVALID_STATUS_URL");
  return { statusURL: statusURL.href, repository: env.GITHUB_REPOSITORY,
    api: `https://api.github.com/repos/${env.GITHUB_REPOSITORY}/actions/workflows/${WORKFLOW}` };
}

/** Derive the requested JST date/slot from the occurrence, never from late delivery. */
export function occurrence(scheduledTime, now) {
  requireThat(Number.isSafeInteger(scheduledTime) && scheduledTime > 0 &&
    Number.isFinite(now), "INVALID_OCCURRENCE");
  const local = new Date(scheduledTime + JST);
  const hour = local.getUTCHours(), minute = local.getUTCMinutes();
  // Cloudflare scheduledTime may include seconds/milliseconds (e.g. 04:20:07.197).
  // Use the scheduled minute; date, maximum delay and deadline remain strict.
  requireThat((hour === 4 || hour === 9) && (minute === 20 || minute === 40), "INVALID_OCCURRENCE");
  const targetDate = jstDate(scheduledTime);
  requireThat(jstDate(now) === targetDate, "EXPIRED_DAY");
  requireThat(scheduledTime <= now + SKEW_MS, "FUTURE_OCCURRENCE");
  requireThat(now - scheduledTime <= MAX_DELAY_MS, "EXPIRED_OCCURRENCE");
  const slot = hour === 4 ? "0900" : "1400";
  const deadline = isoDay(targetDate) + (hour + 1) * 3600_000;
  requireThat(now < deadline, "DEADLINE_PASSED");
  const hhmm = `${String(hour).padStart(2, "0")}${minute}`;
  return { targetDate, slot, scheduledTime, deadline,
    readyAfter: isoDay(targetDate) + hour * 3600_000 + (minute === 40 ? 25 : 0) * 60_000,
    requestId: `worker-${targetDate.replaceAll("-", "")}-${slot}-${hhmm}` };
}

/** One request, bounded body/time, no redirect, no automatic POST retry. */
async function request(stage, url, fetcher, diagnose, options = {}, limit = STATUS_LIMIT) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8_000);
  let status = "IO_ERROR", size = 0;
  try {
    const response = await fetcher(url, { ...options, redirect: "manual", signal: controller.signal });
    status = response.status;
    requireThat(response.status === 200, `${stage.toUpperCase()}_HTTP_${response.status}`);
    const contentType = response.headers.get("content-type") || "";
    requireThat(/^application\/(?:[a-z0-9.+-]+\+)?json(?:\s*;|$)/i.test(contentType), "NON_JSON_RESPONSE");
    const declared = Number(response.headers.get("content-length"));
    requireThat(!Number.isFinite(declared) || declared <= limit, "BODY_LIMIT");
    const reader = response.body?.getReader();
    const chunks = [];
    if (reader) for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.length;
      if (size > limit) {
        status = "BODY_LIMIT";
        await reader.cancel();
        throw new Fault("BODY_LIMIT");
      }
      chunks.push(value);
    }
    const bytes = chunks.length === 1 ? chunks[0] : new Uint8Array(size);
    if (chunks.length > 1) {
      let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
    }
    let value;
    try { value = JSON.parse(decoder.decode(bytes)); }
    catch { throw new Fault("INVALID_JSON"); }
    return { value, response };
  } finally {
    clearTimeout(timeout);
    // Code-owned stage/status/size only: no bodies, headers, token, or secret URL.
    diagnose({ stage, status, bytes: size });
  }
}

/** This tiny file is a scheduling hint, not an independent verification of PNG/PDF. */
function statusHint(value, now) {
  requireThat(plain(value) && value.schema === "kyushu-pages-status-1" && plain(value.slots), "INVALID_STATUS");
  for (const slot of ["0900", "1400"]) {
    requireThat(Object.hasOwn(value.slots, slot), "INVALID_STATUS");
    const item = value.slots[slot];
    if (item === null) continue;
    requireThat(plain(item) && (item.quality === "complete" || item.quality === "partial") &&
      (item.model === "MSM" || item.model === "GSM") && typeof item.release_id === "string" &&
      /^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$/.test(item.release_id), "INVALID_STATUS");
    isoDay(item.valid_date);
    const initial = jstTimestamp(item.initial_time_jst);
    const generated = jstTimestamp(item.generated_at_jst);
    requireThat(jstDate(generated) === item.valid_date && initial <= generated && generated <= now + SKEW_MS,
      "INVALID_STATUS_TIMES");
  }
  return value.slots;
}

function isReady(item, target, now) {
  return Boolean(item && item.valid_date === target.targetDate && item.quality === "complete" &&
    item.model === "MSM" && jstTimestamp(item.generated_at_jst) >= target.readyAfter &&
    jstTimestamp(item.generated_at_jst) <= now + SKEW_MS);
}

function listShape(value, response, limit) {
  requireThat(plain(value) && Number.isSafeInteger(value.total_count) && value.total_count >= 0 &&
    Array.isArray(value.workflow_runs), "UNKNOWN_ACTIONS");
  requireThat(value.total_count <= limit && value.workflow_runs.length === value.total_count &&
    !/rel="next"/.test(response.headers.get("link") || ""), "TRUNCATED_ACTIONS");
  return value.workflow_runs;
}

function workerIdentity(title) {
  if (!title.startsWith("weather-worker/")) return null;
  const match = /^weather-worker\/(\d{4}-\d{2}-\d{2})\/(0900|1400)\/(worker-\d{8}-(?:0900|1400)-(?:0420|0440|0920|0940))$/.exec(title);
  requireThat(match, "UNKNOWN_WORKER_HISTORY");
  const [, day, slot, requestId] = match;
  isoDay(day);
  const hhmm = requestId.slice(-4);
  requireThat(requestId === `worker-${day.replaceAll("-", "")}-${slot}-${hhmm}` &&
    ((slot === "0900" && hhmm.startsWith("04")) || (slot === "1400" && hhmm.startsWith("09"))),
    "UNKNOWN_WORKER_HISTORY");
  return { day, slot, requestId };
}

function checkRecent(runs, target, now) {
  const ids = new Set(), workers = [];
  for (const run of runs) {
    requireThat(plain(run) && Number.isSafeInteger(run.id) && run.id > 0 && !ids.has(run.id) &&
      typeof run.display_title === "string" && ["completed", ...ACTIVE].includes(run.status), "UNKNOWN_ACTIONS");
    ids.add(run.id);
    const created = utcTimestamp(run.created_at);
    requireThat(created >= isoDay(target.targetDate) && created <= now + SKEW_MS, "UNKNOWN_ACTIONS");
    if (run.status !== "completed") return "IN_FLIGHT";
    const identity = workerIdentity(run.display_title);
    if (identity?.day === target.targetDate && identity.slot === target.slot) workers.push({ ...identity, created });
  }
  if (workers.some(run => run.requestId === target.requestId)) return "DUPLICATE_REQUEST";
  if (workers.length >= SLOT_REQUEST_LIMIT) return "SLOT_REQUEST_LIMIT";
  if (workers.some(run => now - run.created < COOLDOWN_MS)) return "COOLDOWN";
  return null;
}

function dispatchReceipt(value, repository) {
  requireThat(plain(value) && Number.isSafeInteger(value.workflow_run_id) && value.workflow_run_id > 0,
    "DISPATCH_UNCONFIRMED");
  const apiURL = `https://api.github.com/repos/${repository}/actions/runs/${value.workflow_run_id}`;
  const htmlURL = `https://github.com/${repository}/actions/runs/${value.workflow_run_id}`;
  requireThat(typeof value.run_url === "string" && value.run_url.toLowerCase() === apiURL.toLowerCase() &&
    typeof value.html_url === "string" && value.html_url.toLowerCase() === htmlURL.toLowerCase(), "DISPATCH_UNCONFIRMED");
  return { workflow_run_id: value.workflow_run_id, run_url: value.run_url, html_url: value.html_url };
}

/** Scheduled-only decision. History is an admission hint; Actions owns final dedup/publication. */
export async function tick(env, scheduledTime, { fetcher = fetch, now = Date.now, diagnose = noDiagnostics } = {}) {
  const config = configuration(env);
  const target = occurrence(scheduledTime, now());
  const result = (status) => ({ status, publication: "NOT_VERIFIED", target_date: target.targetDate,
    slot: target.slot, request_id: target.requestId });
  const status = await request("status", config.statusURL, fetcher, diagnose, { cache: "no-store" });
  const slots = statusHint(status.value, now());
  if (isReady(slots[target.slot], target, now())) return result("READY_MSM");
  requireThat(typeof env.GITHUB_TOKEN === "string" && env.GITHUB_TOKEN.length > 0, "AUTH_REQUIRED");
  const headers = { Accept: "application/vnd.github+json", "User-Agent": "Kyushu-weather-trigger",
    "X-GitHub-Api-Version": API_VERSION, Authorization: `Bearer ${env.GITHUB_TOKEN}` };
  // An unbounded-age active query catches old approvals too. One row proves busy;
  // do not download all historical active runs merely to decide not to dispatch.
  for (const active of ACTIVE) {
    const query = new URLSearchParams({ status: active, per_page: "1" });
    const response = await request(`github_${active}`, `${config.api}/runs?${query}`, fetcher, diagnose,
      { headers }, 64 * 1024);
    const value = response.value;
    requireThat(plain(value) && Number.isSafeInteger(value.total_count) && value.total_count >= 0 &&
      Array.isArray(value.workflow_runs), "UNKNOWN_ACTIONS");
    if (value.total_count > 0) return result("IN_FLIGHT");
    requireThat(value.workflow_runs.length === 0 && !/rel="next"/.test(response.response.headers.get("link") || ""),
      "UNKNOWN_ACTIONS");
  }
  const query = new URLSearchParams({ event: "workflow_dispatch", per_page: String(HISTORY_LIMIT),
    created: ">=" + new Date(isoDay(target.targetDate)).toISOString() });
  const history = await request("github_recent", `${config.api}/runs?${query}`, fetcher, diagnose,
    { headers }, HISTORY_BYTES);
  const reason = checkRecent(listShape(history.value, history.response, HISTORY_LIMIT), target, now());
  if (reason) return result(reason);
  // Slow network calls must not turn a previously valid occurrence into a late
  // prior-day or post-deadline request. Observe mode uses the same decision gates.
  occurrence(scheduledTime, now());
  if (env.MODE === "observe") return result("WOULD_DISPATCH");
  const receipt = await request("dispatch", `${config.api}/dispatches`, fetcher, diagnose, {
    method: "POST", headers: { ...headers, "Content-Type": "application/json" },
    body: JSON.stringify({ ref: env.GITHUB_BRANCH, return_run_details: true, inputs: {
      target_date: target.targetDate, slot: target.slot, request_id: target.requestId, trigger: "worker",
      publish: "true", operation: "refresh",
    } }),
  }, 4096);
  return { ...result("DISPATCH_ACCEPTED"), ...dispatchReceipt(receipt.value, config.repository) };
}

export default {
  // workers.dev/preview URLs are also disabled in configuration. There is no
  // public manual dispatch endpoint, even if a future operator adds a route.
  async fetch() { return new Response("Not found", { status: 404 }); },
  async scheduled(event, env) {
    const diagnostics = [];
    let result;
    try {
      result = await tick(env, event.scheduledTime, { diagnose: record => diagnostics.push(record) });
    } catch (error) {
      result = { status: "UNKNOWN", publication: "NOT_VERIFIED",
        reason: error instanceof Fault ? error.message : "IO_OR_VALIDATION_FAILED" };
      throw new Error(`Weather trigger check failed: ${result.reason}`);
    } finally {
      console.log(JSON.stringify({ ...result, diagnostics }));
    }
  },
};
