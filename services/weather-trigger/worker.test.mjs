import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { execFileSync } from "node:child_process";
import worker, { tick, occurrence, STATUS_LIMIT, WORKFLOW, HISTORY_LIMIT, HISTORY_PAGES } from "./worker.mjs";

const env = {
  MODE: "dispatch", STATUS_URL: "https://weather.example.test/status.json",
  GITHUB_REPOSITORY: "Example/kyushu-weather", GITHUB_BRANCH: "main", GITHUB_TOKEN: "test-secret-only",
};
const at = (clock, day = "2026-11-23") => Date.parse(`${day}T${clock}:00+09:00`);
const pending = () => ({ schema: "kyushu-pages-status-1", slots: { "0900": null, "1400": null } });
const sheet = (overrides = {}) => ({
  valid_date: "2026-11-23", quality: "complete", model: "MSM",
  initial_time_jst: "2026-11-23T03:00:00+09:00", generated_at_jst: "2026-11-23T03:05:00+09:00",
  release_id: "20261123-0900-example", ...overrides,
});
const hint = (entry = sheet(), slot = "0900") => {
  const value = pending(); value.slots[slot] = entry; return value;
};
const json = (value, options = {}) => new Response(JSON.stringify(value), {
  status: 200, headers: { "content-type": "application/json" }, ...options,
});
const receipt = () => ({ workflow_run_id: 987,
  run_url: "https://api.github.com/repos/Example/kyushu-weather/actions/runs/987",
  html_url: "https://github.com/Example/kyushu-weather/actions/runs/987" });
const historyRun = (requestClock = "0315", created = "03:15", id = 1, overrides = {}) => ({
  id, status: "completed", conclusion: "failure", created_at: new Date(at(created)).toISOString(),
  display_title: `weather-worker/2026-11-23/0900/worker-20261123-0900-${requestClock}`, ...overrides,
});

function mock({ status = pending(), active, recent = [], historyTotal, dispatch, afterHistory, historyHeaders } = {}) {
  const calls = [];
  const fetcher = async (url, options = {}) => {
    calls.push({ url, options });
    assert.equal(options.redirect, "manual");
    if (url === env.STATUS_URL) {
      assert.equal(options.cache, "no-store");
      assert.equal(options.headers?.Authorization, undefined, "Never send GitHub credentials to the site");
      return status instanceof Response ? status : json(status);
    }
    const parsed = new URL(url);
    assert.equal(parsed.origin, "https://api.github.com");
    assert.ok(parsed.pathname.includes(`/actions/workflows/${WORKFLOW}`));
    assert.equal(options.headers.Authorization, `Bearer ${env.GITHUB_TOKEN}`);
    assert.equal(options.headers["X-GitHub-Api-Version"], "2026-03-10");
    if (parsed.pathname.endsWith("/dispatches")) {
      assert.equal(options.method, "POST");
      if (dispatch) return dispatch(url, options);
      return json(receipt());
    }
    const state = parsed.searchParams.get("status");
    if (state) {
      assert.equal(parsed.searchParams.get("per_page"), "1");
      return json({ total_count: state === active ? 1 : 0, workflow_runs: state === active ? [{ id: 11 }] : [] });
    }
    assert.equal(parsed.searchParams.get("event"), "workflow_dispatch");
    assert.equal(parsed.searchParams.get("created"), ">=2026-11-22T15:00:00.000Z");
    afterHistory?.();
    const page = Number(parsed.searchParams.get("page"));
    assert.ok(page >= 1);
    assert.equal(parsed.searchParams.get("per_page"), String(HISTORY_LIMIT));
    const batch = recent.slice((page - 1) * HISTORY_LIMIT, page * HISTORY_LIMIT);
    const headers = { "content-type": "application/json" };
    if (page * HISTORY_LIMIT < recent.length) headers.link = `<${url}>; rel="next"`;
    return json({ total_count: historyTotal ?? recent.length, workflow_runs: batch },
      { headers: { ...headers, ...historyHeaders } });
  };
  const posts = () => calls.filter(call => call.options.method === "POST");
  return { calls, posts, fetcher };
}

test("pending 09 slot dispatches one exact scheduled request, never downloads GPV", async () => {
  const network = mock();
  const result = await tick(env, at("03:15"), { fetcher: network.fetcher, now: () => at("04:21") });
  assert.equal(result.status, "DISPATCH_ACCEPTED");
  assert.equal(result.publication, "NOT_VERIFIED");
  assert.equal(result.workflow_run_id, 987);
  assert.equal(network.calls.length, 8);
  assert.equal(network.posts().length, 1);
  assert.deepEqual(JSON.parse(network.posts()[0].options.body), { ref: "main", return_run_details: true, inputs: {
    target_date: "2026-11-23", slot: "0900", request_id: "worker-20261123-0900-0315", trigger: "worker",
    publish: "true", operation: "refresh",
  } });
});

test("real Cloudflare scheduledTime seconds are accepted for the nominated minute", async () => {
  // Preserve tolerance for seconds/milliseconds observed in the former schedule.
  for (const [scheduledTime, slot, hhmm] of [
    [at("03:15") + 7197, "0900", "0315"],
    [at("03:35") + 7197, "0900", "0335"],
    [at("09:15") + 7197, "1400", "0915"],
  ]) {
    const network = mock();
    const result = await tick(env, scheduledTime, {
      fetcher: network.fetcher, now: () => scheduledTime + 500,
    });
    assert.equal(result.status, "DISPATCH_ACCEPTED");
    assert.equal(result.slot, slot);
    assert.equal(result.request_id, `worker-20261123-${slot}-${hhmm}`);
    assert.equal(network.posts().length, 1);
  }
});

test("failed scheduled invocation exposes safe reason code in exception", async () => {
  const originalLog = console.log;
  const log = [];
  console.log = (message) => log.push(message);
  try {
    await assert.rejects(
      worker.scheduled({ scheduledTime: at("03:15") + 7197 }, { ...env, MODE: "invalid" }),
      /Weather trigger check failed: INVALID_MODE/,
    );
  } finally {
    console.log = originalLog;
  }
  assert.equal(JSON.parse(log[0]).reason, "INVALID_MODE");
});

test("complete recent MSM needs only the small public hint", async () => {
  const network = mock({ status: hint() });
  const result = await tick({ ...env, GITHUB_TOKEN: undefined }, at("03:15"), {
    fetcher: network.fetcher, now: () => at("03:15"),
  });
  assert.equal(result.status, "READY_MSM");
  assert.equal(network.calls.length, 1);
});

test("recently regenerated 0000I does not suppress the 0300I target", async () => {
  const network = mock({ status: hint(sheet({ initial_time_jst: "2026-11-23T00:00:00+09:00", generated_at_jst: "2026-11-23T03:14:00+09:00" })) });
  const result = await tick(env, at("03:15"), { fetcher: network.fetcher, now: () => at("03:15") });
  assert.equal(result.status, "DISPATCH_ACCEPTED");
});

test("GSM fallback and terrain-partial MSM both remain recovery candidates", async () => {
  for (const entry of [sheet({ model: "GSM" }), sheet({ quality: "partial" })]) {
    const network = mock({ status: hint(entry) });
    assert.equal((await tick(env, at("03:15"), { fetcher: network.fetcher, now: () => at("03:15") })).status,
      "DISPATCH_ACCEPTED");
  }
});

test("yesterday's truthful publication is stale, not malformed", async () => {
  const network = mock({ status: hint(sheet({ valid_date: "2026-11-22",
    initial_time_jst: "2026-11-22T00:00:00+09:00", generated_at_jst: "2026-11-22T04:05:00+09:00" })) });
  assert.equal((await tick(env, at("03:15"), { fetcher: network.fetcher, now: () => at("03:15") })).status,
    "DISPATCH_ACCEPTED");
});

test("all active states including old approval waits stop dispatch", async () => {
  for (const state of ["queued", "in_progress", "waiting", "pending", "requested"]) {
    const network = mock({ active: state });
    const result = await tick(env, at("03:15"), { fetcher: network.fetcher, now: () => at("03:15") });
    assert.equal(result.status, "IN_FLIGHT");
    assert.equal(network.posts().length, 0);
  }
});

test("a job appearing during the active-query race is caught in recent history", async () => {
  const network = mock({ recent: [historyRun("0315", "03:15", 1, { status: "queued" })] });
  const result = await tick(env, at("03:15"), { fetcher: network.fetcher, now: () => at("04:21") });
  assert.equal(result.status, "IN_FLIGHT");
  assert.equal(network.posts().length, 0);
});

test("same occurrence is not dispatched again after either success or failure", async () => {
  for (const conclusion of ["success", "failure", "cancelled"]) {
    const network = mock({ recent: [historyRun("0315", "03:15", 1, { conclusion })] });
    const result = await tick(env, at("03:15"), { fetcher: network.fetcher, now: () => at("04:21") });
    assert.equal(result.status, "DUPLICATE_REQUEST");
    assert.equal(network.posts().length, 0);
  }
});

test("recent failed runs do not impose a cooldown", async () => {
  const network = mock({ recent: [historyRun("0315", "03:30")] });
  const result = await tick(env, at("03:35"), { fetcher: network.fetcher, now: () => at("03:35") });
  assert.equal(result.status, "DISPATCH_ACCEPTED");
  assert.equal(network.posts().length, 1);
});

test("past failures and run count do not consume a slot cap", async () => {
  const network = mock({ recent: [historyRun("0315", "03:15", 1), historyRun("0315", "03:16", 2)] });
  const result = await tick(env, at("03:35"), { fetcher: network.fetcher, now: () => at("03:35") });
  assert.equal(result.status, "DISPATCH_ACCEPTED");
  assert.equal(network.posts().length, 1);
});

test("unknown/truncated history never proves no work exists", async () => {
  for (const options of [{ historyTotal: 31 }, { historyTotal: 1 },
    { historyHeaders: { link: '<https://api.github.com/next>; rel="next"' } }]) {
    const network = mock(options);
    await assert.rejects(tick(env, at("03:15"), { fetcher: network.fetcher, now: () => at("03:15") }), /TRUNCATED_ACTIONS/);
    assert.equal(network.posts().length, 0);
  }
});

test("malformed worker names cannot evade duplicate gates", async () => {
  const network = mock({ recent: [historyRun("0315", "03:15", 1, {
    display_title: "weather-worker/2026-11-23/0900/worker-20261123-1400-0915",
  })] });
  await assert.rejects(tick(env, at("03:35"), { fetcher: network.fetcher, now: () => at("03:35") }), /UNKNOWN_WORKER_HISTORY/);
  assert.equal(network.posts().length, 0);
});

test("missing, HTML, oversized, invalid-schema hints fail closed before GitHub", async () => {
  for (const status of [new Response("Not found", { status: 404 }),
    new Response("<html>SPA fallback</html>", { headers: { "content-type": "text/html" } }),
    { schema: "wrong", slots: {} }, { ...pending(), unexpected: "x".repeat(STATUS_LIMIT) }]) {
    const network = mock({ status });
    await assert.rejects(tick(env, at("03:15"), { fetcher: network.fetcher, now: () => at("03:15") }));
    assert.equal(network.calls.length, 1);
  }
});

test("future generation or an invalid date cannot be treated as ready", async () => {
  for (const entry of [sheet({ generated_at_jst: "2026-11-23T03:17:00+09:00" }),
    sheet({ valid_date: "2026-02-30" }), sheet({ initial_time_jst: "2026-11-23T04:15:00+09:00" })]) {
    const network = mock({ status: hint(entry) });
    await assert.rejects(tick(env, at("03:15"), { fetcher: network.fetcher, now: () => at("03:15") }));
    assert.equal(network.posts().length, 0);
  }
});

test("prior-day, future and non-schedule events make no network request", async () => {
  const network = mock();
  for (const [eventTime, now] of [[at("03:15", "2026-11-22"), at("03:15")],
    [at("03:15") + 1, at("03:15")], [at("04:30"), at("04:30")], [NaN, at("03:15")],
    [at("09:35"), at("09:34")], [at("03:20"), at("03:15")]]) {
    await assert.rejects(tick(env, eventTime, { fetcher: network.fetcher, now: () => now }));
  }
  assert.equal(network.calls.length, 0);
});

test("slow preflight is rechecked before POST", async () => {
  let now = at("03:35");
  const network = mock({ afterHistory: () => { now = at("00:00", "2026-11-24"); } });
  await assert.rejects(tick(env, at("03:35"), { fetcher: network.fetcher, now: () => now }), /EXPIRED_DAY/);
  assert.equal(network.posts().length, 0);
});

test("observe mode exercises the gates and never sends a POST", async () => {
  const network = mock();
  const result = await tick({ ...env, MODE: "observe" }, at("03:15"), { fetcher: network.fetcher, now: () => at("03:15") });
  assert.equal(result.status, "WOULD_DISPATCH");
  assert.equal(network.calls.length, 7);
  assert.equal(network.posts().length, 0);
});

test("dispatch network failure, 403 and ambiguous receipts are never retried", async () => {
  for (const dispatch of [() => { throw new Error("network failed after send"); },
    () => new Response("forbidden", { status: 403 }),
    () => new Response("unavailable", { status: 503 }),
    () => new Response(null, { status: 204 }), () => json({ workflow_run_id: 987 }),
    () => json({ ...receipt(), html_url: "https://github.com/Other/repo/actions/runs/987" })]) {
    const network = mock({ dispatch });
    await assert.rejects(tick(env, at("03:15"), { fetcher: network.fetcher, now: () => at("03:15") }));
    assert.equal(network.posts().length, 1);
  }
});

test("HTTP entry point is closed and checked-in configuration uses the approved daily schedule", async () => {
  const response = await worker.fetch(new Request("https://worker.test/run"), env);
  assert.equal(response.status, 404);
  const config = JSON.parse(await readFile(new URL("wrangler.jsonc", import.meta.url), "utf8"));
  assert.equal(config.vars.MODE, "dispatch");
  assert.deepEqual(config.triggers.crons, ["15,35,55 0,1,15-23 * * *"]);
  assert.equal(config.workers_dev, false);
  assert.equal(config.preview_urls, false);
  assert.equal(config.kv_namespaces, undefined);
  assert.equal(config.durable_objects, undefined);
});

test("history paginates through 300 failures and finds a duplicate on the last page", async () => {
  const runs = Array.from({ length: HISTORY_LIMIT * HISTORY_PAGES }, (_, i) => historyRun("0315", "03:15", i + 1));
  for (const duplicate of [false, true]) {
    const recent = structuredClone(runs);
    if (duplicate) recent.at(-1).display_title = historyRun("0335").display_title;
    const network = mock({ recent });
    const result = await tick(env, at("03:35"), { fetcher: network.fetcher, now: () => at("22:00") });
    assert.equal(result.status, duplicate ? "DUPLICATE_REQUEST" : "DISPATCH_ACCEPTED");
    assert.equal(network.calls.filter(call => new URL(call.url).searchParams.has("page")).length, HISTORY_PAGES);
  }
});

test("missing, shifting, repeated or over-budget pages fail closed", async () => {
  const runs = Array.from({ length: 31 }, (_, i) => historyRun("0315", "03:15", i + 1));
  for (const mode of ["missing", "shifted", "repeated", "over-budget", "unavailable"]) {
    const recent = structuredClone(runs);
    if (mode === "repeated") recent.at(-1).id = recent[0].id;
    const network = mock({ recent, ...(mode === "over-budget" ? { historyTotal: 301 } : {}) });
    let attempts = 0;
    const fetcher = async (url, options) => {
      if (new URL(url).searchParams.get("page") === "2") {
        attempts++;
        if (mode === "missing") return json({ total_count: 31, workflow_runs: [] });
        if (mode === "shifted") return json({ total_count: 32, workflow_runs: [runs.at(-1)] });
        if (mode === "unavailable") return new Response(null, { status: 503 });
      }
      return network.fetcher(url, options);
    };
    await assert.rejects(tick(env, at("03:35"), { fetcher, now: () => at("22:00"), sleep: async () => {} }));
    assert.equal(network.posts().length, 0);
    if (mode === "unavailable") assert.equal(attempts, 3);
  }
});

test("transient history reads retry twice with 1s and 3s delays, including active and later pages", async () => {
  for (const stage of ["active", "recent", "page2"]) {
    for (const failure of ["io", 408, 429, 500, 503]) {
      const runs = Array.from({ length: 31 }, (_, i) => historyRun("0315", "03:15", i + 1));
      const network = mock({ recent: runs });
      let attempts = 0;
      const delays = [];
      const fetcher = async (url, options) => {
        const query = new URL(url).searchParams;
        const selected = stage === "active" ? query.get("status") === "queued" :
          query.get("page") === (stage === "page2" ? "2" : "1");
        if (selected && ++attempts < 3) {
          if (failure === "io") throw new TypeError("controlled connection failure");
          return new Response(null, { status: failure });
        }
        return network.fetcher(url, options);
      };
      const result = await tick(env, at("03:35"), { fetcher, now: () => at("22:00"),
        sleep: async ms => delays.push(ms) });
      assert.equal(result.status, "DISPATCH_ACCEPTED");
      assert.equal(attempts, 3);
      assert.deepEqual(delays, [1000, 3000]);
      assert.equal(network.posts().length, 1);
    }
  }
});

test("permanent history errors stop immediately; transient exhaustion stops after three reads", async () => {
  for (const status of [401, 403, 404, 429, 503]) {
    const network = mock();
    let attempts = 0;
    const delays = [];
    const fetcher = (url, options) => {
      if (new URL(url).searchParams.get("status") === "queued") {
        attempts++;
        return new Response(null, { status });
      }
      return network.fetcher(url, options);
    };
    await assert.rejects(tick(env, at("03:15"), { fetcher, now: () => at("03:15"),
      sleep: async ms => delays.push(ms) }), new RegExp(`HTTP_${status}`));
    assert.equal(attempts, [429, 503].includes(status) ? 3 : 1);
    assert.equal(delays.length, attempts - 1);
    assert.equal(network.posts().length, 0);
  }
});

const resultsOf = result => result.results ?? [result];
const clocks = (start, end) => Array.from({ length: (end - start) / 20 + 1 }, (_, i) => {
  const minute = start + i * 20;
  return `${String(Math.floor(minute / 60)).padStart(2, "0")}:${String(minute % 60).padStart(2, "0")}`;
});

test("Cron covers exactly 33 events and 34 edition checks, with both editions at 06:15", async () => {
  const expected = { "0900": clocks(15, 375), "1400": clocks(375, 655) };
  const admitted = [];
  for (let minute = 0; minute < 1440; minute++) {
    const clock = clocks(minute, minute)[0];
    const slots = ["0900", "1400"].filter(slot => expected[slot].includes(clock));
    if (!slots.length) {
      assert.throws(() => occurrence(at(clock), at("23:59")), /INVALID_OCCURRENCE/);
      continue;
    }
    const targets = occurrence(at(clock) + 7197, at("23:59"));
    assert.deepEqual(targets.map(target => target.slot), slots);
    const network = mock();
    const result = await tick(env, at(clock) + 7197, { fetcher: network.fetcher, now: () => at("23:59") });
    assert.equal(network.posts().length, slots.length);
    for (const target of targets) {
      assert.equal(target.targetDate, "2026-11-23");
      assert.equal(target.requestId, `worker-20261123-${target.slot}-${clock.replace(":", "")}`);
      assert.equal(resultsOf(result).find(r => r.slot === target.slot).status, "DISPATCH_ACCEPTED");
      admitted.push([target.slot, clock.replace(":", "")]);
    }
  }
  assert.equal(admitted.length, 34);
  assert.equal(new Set(admitted.map(([, clock]) => clock)).size, 33);
  // Exercise the actual Actions validator, not a second JS copy of its allowlist.
  const python = execFileSync("python3", ["-c", `
import json
from production.actions_job import resolve_plan, JobError
admitted = []
for minute in range(1440):
    tick = f"{minute // 60:02d}{minute % 60:02d}"
    for slot in ("0900", "1400"):
        try:
            resolve_plan("workflow_dispatch", {"inputs": {
                "trigger": "worker", "target_date": "2026-11-23", "slot": slot,
                "request_id": f"worker-20261123-{slot}-{tick}", "publish": True}},
                created_at="2026-11-23T23:59:00+09:00", now="2026-11-23T23:59:00+09:00",
                run_id=1, ref="refs/heads/main")
            admitted.append([slot, tick])
        except JobError:
            pass
print(json.dumps(admitted))
`], { cwd: new URL("../../", import.meta.url), encoding: "utf8", env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" } });
  assert.deepEqual(JSON.parse(python), admitted);
  const config = JSON.parse(await readFile(new URL("wrangler.jsonc", import.meta.url), "utf8"));
  assert.equal(config.triggers.crons[0], "15,35,55 0,1,15-23 * * *");
  const utcHours = [0, 1, ...Array.from({ length: 9 }, (_, i) => i + 15)];
  const cronClocks = utcHours.flatMap(hour => [15, 35, 55].map(minute =>
    `${String((hour + 9) % 24).padStart(2, "0")}:${minute}`));
  assert.deepEqual(cronClocks.sort(), [...new Set(Object.values(expected).flat())].sort());
  const deploy = await readFile(new URL("../../.github/workflows/deploy-worker.yml", import.meta.url), "utf8");
  assert.ok(deploy.includes(`JSON.stringify(['${config.triggers.crons[0]}'])`));
  const workflow = await readFile(new URL("../../.github/workflows/weather-daily.yml", import.meta.url), "utf8");
  assert.match(workflow, /group: weather-publication\s+cancel-in-progress: false\s+queue: max/);
});

test("Worker and Actions agree on initial-time targets, delayed stages and time contradictions", async () => {
  const cases = [
    ["0900", "00:15", "00:15", "00:00", "00:10", true],
    ["0900", "02:55", "03:14", "00:00", "03:10", true],
    ["0900", "03:15", "03:15", "00:00", "03:14", false],
    ["0900", "03:15", "03:15", "03:00", "03:10", true],
    ["0900", "00:15", "04:35", "00:00", "04:34", false],
    ["0900", "00:15", "04:35", "03:00", "03:10", true],
    ["0900", "04:35", "04:35", "00:00", "04:35", false],
    ["1400", "06:15", "06:15", "06:00", "06:10", true],
    ["1400", "08:55", "09:14", "06:00", "09:10", true],
    ["1400", "09:15", "09:15", "06:00", "09:14", false],
    ["1400", "09:15", "09:15", "09:00", "09:10", true],
    ["1400", "06:15", "10:55", "06:00", "10:50", false],
    ["1400", "10:55", "10:55", "09:00", "10:50", true],
    ["0900", "03:15", "03:15", "03:00", "03:17", false],
    ["0900", "03:15", "03:15", "03:00", "02:59", false],
  ];
  const evidence = [];
  for (const [slot, scheduled, now, initial, generated, ready] of cases) {
    const status = hint(sheet({ initial_time_jst: `2026-11-23T${initial}:00+09:00`,
      generated_at_jst: `2026-11-23T${generated}:00+09:00` }), slot);
    const network = mock({ status });
    let actual = false;
    try {
      const result = await tick(env, at(scheduled), { fetcher: network.fetcher, now: () => at(now) });
      actual = resultsOf(result).find(r => r.slot === slot).status === "READY_MSM";
    } catch (error) { assert.match(error.message, /INVALID_STATUS_TIMES/); }
    assert.equal(actual, ready, JSON.stringify({ slot, scheduled, now, initial, generated }));
    evidence.push({ slot, scheduled, now, status, ready });
  }
  const python = execFileSync("python3", ["-c", `
import json, sys
from production.actions_job import resolve_plan, worker_ready, instant
result = []
for case in json.load(sys.stdin):
    timestamp = "2026-11-23T" + case["now"] + ":00+09:00"
    plan = resolve_plan("workflow_dispatch", {"inputs": {
        "trigger": "worker", "target_date": "2026-11-23", "slot": case["slot"],
        "request_id": "worker-20261123-" + case["slot"] + "-" + case["scheduled"].replace(":", ""),
        "publish": True}}, created_at=timestamp, now=timestamp, run_id=1, ref="refs/heads/main")
    result.append(worker_ready(case["status"], plan, instant(timestamp)))
print(json.dumps(result))
`], { cwd: new URL("../../", import.meta.url), input: JSON.stringify(evidence), encoding: "utf8",
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" } });
  assert.deepEqual(JSON.parse(python), evidence.map(item => item.ready));
});

test("06:15 evaluates each edition independently and shares all GETs", async () => {
  for (const readySlots of [[], ["0900"], ["1400"], ["0900", "1400"]]) {
    const status = pending();
    for (const slot of readySlots) status.slots[slot] = sheet({
      initial_time_jst: `2026-11-23T${slot === "0900" ? "03" : "06"}:00:00+09:00`,
      generated_at_jst: "2026-11-23T06:10:00+09:00",
    });
    const network = mock({ status });
    const result = await tick(env, at("06:15"), { fetcher: network.fetcher, now: () => at("06:15") });
    assert.equal(result.results.length, 2);
    for (const item of result.results) assert.equal(item.status,
      readySlots.includes(item.slot) ? "READY_MSM" : "DISPATCH_ACCEPTED");
    assert.equal(network.posts().length, 2 - readySlots.length);
    assert.equal(network.calls.length, readySlots.length === 2 ? 1 : 9 - readySlots.length);
  }
});

test("06:15 duplicate IDs suppress only their own edition; active runs suppress both", async () => {
  for (const slot of ["0900", "1400"]) {
    const network = mock({ recent: [historyRun("0615", "06:15", 7, {
      display_title: `weather-worker/2026-11-23/${slot}/worker-20261123-${slot}-0615`,
    })] });
    const result = await tick(env, at("06:15"), { fetcher: network.fetcher, now: () => at("06:16") });
    assert.equal(result.results.find(r => r.slot === slot).status, "DUPLICATE_REQUEST");
    assert.equal(network.posts().length, 1);
    assert.notEqual(JSON.parse(network.posts()[0].options.body).inputs.slot, slot);
  }
  for (const active of ["queued", "in_progress", "waiting", "pending", "requested"]) {
    const network = mock({ active });
    const result = await tick(env, at("06:15"), { fetcher: network.fetcher, now: () => at("06:15") });
    assert.ok(result.results.every(r => r.status === "IN_FLIGHT"));
    assert.equal(network.posts().length, 0);
  }
});

test("06:15 accounts for the second edition after an ambiguous first POST without replay", async () => {
  let attempt = 0;
  const network = mock({ dispatch: () => {
    if (++attempt === 1) throw new Error("response lost after send");
    return json(receipt());
  } });
  const result = await tick(env, at("06:15"), { fetcher: network.fetcher, now: () => at("06:15") });
  assert.equal(result.status, "UNKNOWN");
  assert.deepEqual(result.results.map(r => r.status), ["UNKNOWN", "DISPATCH_ACCEPTED"]);
  assert.deepEqual(network.posts().map(p => JSON.parse(p.options.body).inputs.slot), ["0900", "1400"]);
});

test("06:15 midnight rollover after the first POST blocks the second edition", async () => {
  let now = at("23:59");
  const network = mock({ dispatch: () => { now = at("00:00", "2026-11-24"); return json(receipt()); } });
  const result = await tick(env, at("06:15"), { fetcher: network.fetcher, now: () => now });
  assert.equal(network.posts().length, 1);
  assert.equal(result.results[1].reason, "EXPIRED_DAY");
});

test("06:15 maximum paginated GET retries and two POSTs fit the 50 subrequest budget", async () => {
  const recent = Array.from({ length: 300 }, (_, i) => historyRun("0015", "00:15", i + 1));
  const network = mock({ recent });
  const attempts = new Map();
  let requests = 0;
  const fetcher = (url, options) => {
    requests++;
    if (url !== env.STATUS_URL && options.method !== "POST") {
      const attempt = (attempts.get(url) ?? 0) + 1;
      attempts.set(url, attempt);
      if (attempt < 3) return new Response(null, { status: 503 });
    }
    return network.fetcher(url, options);
  };
  const result = await tick(env, at("06:15"), { fetcher, now: () => at("06:15"), sleep: async () => {} });
  assert.ok(result.results.every(r => r.status === "DISPATCH_ACCEPTED"));
  assert.equal(requests, 48);
});

test("old-schedule history is readable during rollout but cannot trigger a new request", async () => {
  const network = mock({ recent: [historyRun("0420", "04:20")] });
  assert.equal((await tick(env, at("04:35"), { fetcher: network.fetcher, now: () => at("04:35") })).status,
    "DISPATCH_ACCEPTED");
  assert.throws(() => occurrence(at("04:20"), at("04:35")), /INVALID_OCCURRENCE/);
});

test("an unavailable target Run leaves the next occurrence eligible", async () => {
  const status = hint(sheet({ initial_time_jst: "2026-11-23T00:00:00+09:00",
    generated_at_jst: "2026-11-23T03:20:00+09:00" }));
  const network = mock({ status, recent: [historyRun("0315", "03:15", 1, { conclusion: "success" })] });
  const result = await tick(env, at("03:35"), { fetcher: network.fetcher, now: () => at("03:35") });
  assert.equal(result.status, "DISPATCH_ACCEPTED");
  assert.equal(result.request_id, "worker-20261123-0900-0335");
});
