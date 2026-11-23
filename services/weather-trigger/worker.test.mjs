import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import worker, { tick, occurrence, STATUS_LIMIT, WORKFLOW, HISTORY_LIMIT, HISTORY_PAGES } from "./worker.mjs";

const env = {
  MODE: "dispatch", STATUS_URL: "https://weather.example.test/status.json",
  GITHUB_REPOSITORY: "Example/kyushu-weather", GITHUB_BRANCH: "main", GITHUB_TOKEN: "test-secret-only",
};
const at = (clock, day = "2026-11-23") => Date.parse(`${day}T${clock}:00+09:00`);
const pending = () => ({ schema: "kyushu-pages-status-1", slots: { "0900": null, "1400": null } });
const sheet = (overrides = {}) => ({
  valid_date: "2026-11-23", quality: "complete", model: "MSM",
  initial_time_jst: "2026-11-23T00:00:00+09:00", generated_at_jst: "2026-11-23T04:05:00+09:00",
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
const historyRun = (requestClock = "0420", created = "04:20", id = 1, overrides = {}) => ({
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
  const result = await tick(env, at("04:20"), { fetcher: network.fetcher, now: () => at("04:21") });
  assert.equal(result.status, "DISPATCH_ACCEPTED");
  assert.equal(result.publication, "NOT_VERIFIED");
  assert.equal(result.workflow_run_id, 987);
  assert.equal(network.calls.length, 8);
  assert.equal(network.posts().length, 1);
  assert.deepEqual(JSON.parse(network.posts()[0].options.body), { ref: "main", return_run_details: true, inputs: {
    target_date: "2026-11-23", slot: "0900", request_id: "worker-20261123-0900-0420", trigger: "worker",
    publish: "true", operation: "refresh",
  } });
});

test("real Cloudflare scheduledTime seconds are accepted for the nominated minute", async () => {
  // Actual 2026-11-23 04:20/04:40/09:20 JST invocations contained .197 seconds.
  for (const [scheduledTime, slot, hhmm] of [
    [1795375207197, "0900", "0420"],
    [1795376407197, "0900", "0440"],
    [1795393207197, "1400", "0920"],
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
      worker.scheduled({ scheduledTime: 1795375207197 }, { ...env, MODE: "invalid" }),
      /Weather trigger check failed: INVALID_MODE/,
    );
  } finally {
    console.log = originalLog;
  }
  assert.equal(JSON.parse(log[0]).reason, "INVALID_MODE");
});

test("complete recent MSM needs only the small public hint", async () => {
  const network = mock({ status: hint() });
  const result = await tick({ ...env, GITHUB_TOKEN: undefined }, at("04:20"), {
    fetcher: network.fetcher, now: () => at("04:20"),
  });
  assert.equal(result.status, "READY_MSM");
  assert.equal(network.calls.length, 1);
});

test("03:02 bootstrap must not suppress the first morning refresh", async () => {
  const network = mock({ status: hint(sheet({ generated_at_jst: "2026-11-23T03:02:00+09:00" })) });
  const result = await tick(env, at("04:20"), { fetcher: network.fetcher, now: () => at("04:20") });
  assert.equal(result.status, "DISPATCH_ACCEPTED");
});

test("40-minute tick requests the second check if the first completed at 04:05", async () => {
  const network = mock({ status: hint() });
  const result = await tick(env, at("04:40"), { fetcher: network.fetcher, now: () => at("04:40") });
  assert.equal(result.status, "DISPATCH_ACCEPTED");
  assert.equal(result.request_id, "worker-20261123-0900-0440");
});

test("40-minute tick accepts completed MSM checked since 04:25", async () => {
  const network = mock({ status: hint(sheet({ generated_at_jst: "2026-11-23T04:25:00+09:00" })) });
  const result = await tick(env, at("04:40"), { fetcher: network.fetcher, now: () => at("04:40") });
  assert.equal(result.status, "READY_MSM");
  assert.equal(network.calls.length, 1);
});

test("14 slot uses 09:25 second-check threshold and its own request ID", async () => {
  const network = mock({ status: hint(sheet({ generated_at_jst: "2026-11-23T09:24:00+09:00" }), "1400") });
  const result = await tick(env, at("09:40"), { fetcher: network.fetcher, now: () => at("09:40") });
  assert.equal(result.slot, "1400");
  assert.equal(result.request_id, "worker-20261123-1400-0940");
  assert.equal(result.status, "DISPATCH_ACCEPTED");
});

test("GSM fallback and terrain-partial MSM both remain recovery candidates", async () => {
  for (const entry of [sheet({ model: "GSM" }), sheet({ quality: "partial" })]) {
    const network = mock({ status: hint(entry) });
    assert.equal((await tick(env, at("04:20"), { fetcher: network.fetcher, now: () => at("04:20") })).status,
      "DISPATCH_ACCEPTED");
  }
});

test("yesterday's truthful publication is stale, not malformed", async () => {
  const network = mock({ status: hint(sheet({ valid_date: "2026-11-22",
    initial_time_jst: "2026-11-22T00:00:00+09:00", generated_at_jst: "2026-11-22T04:05:00+09:00" })) });
  assert.equal((await tick(env, at("04:20"), { fetcher: network.fetcher, now: () => at("04:20") })).status,
    "DISPATCH_ACCEPTED");
});

test("all active states including old approval waits stop dispatch", async () => {
  for (const state of ["queued", "in_progress", "waiting", "pending", "requested"]) {
    const network = mock({ active: state });
    const result = await tick(env, at("04:20"), { fetcher: network.fetcher, now: () => at("04:20") });
    assert.equal(result.status, "IN_FLIGHT");
    assert.equal(network.posts().length, 0);
  }
});

test("a job appearing during the active-query race is caught in recent history", async () => {
  const network = mock({ recent: [historyRun("0420", "04:20", 1, { status: "queued" })] });
  const result = await tick(env, at("04:20"), { fetcher: network.fetcher, now: () => at("04:21") });
  assert.equal(result.status, "IN_FLIGHT");
  assert.equal(network.posts().length, 0);
});

test("same occurrence is not dispatched again after either success or failure", async () => {
  for (const conclusion of ["success", "failure", "cancelled"]) {
    const network = mock({ recent: [historyRun("0420", "04:20", 1, { conclusion })] });
    const result = await tick(env, at("04:20"), { fetcher: network.fetcher, now: () => at("04:21") });
    assert.equal(result.status, "DUPLICATE_REQUEST");
    assert.equal(network.posts().length, 0);
  }
});

test("recent failed runs do not impose a cooldown", async () => {
  const network = mock({ recent: [historyRun("0420", "04:30")] });
  const result = await tick(env, at("04:40"), { fetcher: network.fetcher, now: () => at("04:40") });
  assert.equal(result.status, "DISPATCH_ACCEPTED");
  assert.equal(network.posts().length, 1);
});

test("past failures and run count do not consume a slot cap", async () => {
  const network = mock({ recent: [historyRun("0420", "04:20", 1), historyRun("0420", "04:21", 2)] });
  const result = await tick(env, at("04:40"), { fetcher: network.fetcher, now: () => at("04:40") });
  assert.equal(result.status, "DISPATCH_ACCEPTED");
  assert.equal(network.posts().length, 1);
});

test("unknown/truncated history never proves no work exists", async () => {
  for (const options of [{ historyTotal: 31 }, { historyTotal: 1 },
    { historyHeaders: { link: '<https://api.github.com/next>; rel="next"' } }]) {
    const network = mock(options);
    await assert.rejects(tick(env, at("04:20"), { fetcher: network.fetcher, now: () => at("04:20") }), /TRUNCATED_ACTIONS/);
    assert.equal(network.posts().length, 0);
  }
});

test("malformed worker names cannot evade duplicate gates", async () => {
  const network = mock({ recent: [historyRun("0420", "04:20", 1, {
    display_title: "weather-worker/2026-11-23/0900/worker-20261123-1400-0920",
  })] });
  await assert.rejects(tick(env, at("04:40"), { fetcher: network.fetcher, now: () => at("04:40") }), /UNKNOWN_WORKER_HISTORY/);
  assert.equal(network.posts().length, 0);
});

test("missing, HTML, oversized, invalid-schema hints fail closed before GitHub", async () => {
  for (const status of [new Response("Not found", { status: 404 }),
    new Response("<html>SPA fallback</html>", { headers: { "content-type": "text/html" } }),
    { schema: "wrong", slots: {} }, { ...pending(), unexpected: "x".repeat(STATUS_LIMIT) }]) {
    const network = mock({ status });
    await assert.rejects(tick(env, at("04:20"), { fetcher: network.fetcher, now: () => at("04:20") }));
    assert.equal(network.calls.length, 1);
  }
});

test("future generation or an invalid date cannot be treated as ready", async () => {
  for (const entry of [sheet({ generated_at_jst: "2026-11-23T04:22:00+09:00" }),
    sheet({ valid_date: "2026-02-30" }), sheet({ initial_time_jst: "2026-11-23T04:15:00+09:00" })]) {
    const network = mock({ status: hint(entry) });
    await assert.rejects(tick(env, at("04:20"), { fetcher: network.fetcher, now: () => at("04:20") }));
    assert.equal(network.posts().length, 0);
  }
});

test("prior-day, future and non-schedule events make no network request", async () => {
  const network = mock();
  for (const [eventTime, now] of [[at("04:20", "2026-11-22"), at("04:20")],
    [at("04:20") + 1, at("04:20")], [at("04:30"), at("04:30")], [NaN, at("04:20")],
    [at("09:40"), at("09:39")], [at("03:20"), at("04:20")]]) {
    await assert.rejects(tick(env, eventTime, { fetcher: network.fetcher, now: () => now }));
  }
  assert.equal(network.calls.length, 0);
});

test("slow preflight is rechecked before POST", async () => {
  let now = at("04:40");
  const network = mock({ afterHistory: () => { now = at("00:00", "2026-11-24"); } });
  await assert.rejects(tick(env, at("04:40"), { fetcher: network.fetcher, now: () => now }), /EXPIRED_DAY/);
  assert.equal(network.posts().length, 0);
});

test("observe mode exercises the gates and never sends a POST", async () => {
  const network = mock();
  const result = await tick({ ...env, MODE: "observe" }, at("04:20"), { fetcher: network.fetcher, now: () => at("04:20") });
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
    await assert.rejects(tick(env, at("04:20"), { fetcher: network.fetcher, now: () => at("04:20") }));
    assert.equal(network.posts().length, 1);
  }
});

test("HTTP entry point is closed and checked-in configuration uses the approved daily schedule", async () => {
  const response = await worker.fetch(new Request("https://worker.test/run"), env);
  assert.equal(response.status, 404);
  const config = JSON.parse(await readFile(new URL("wrangler.jsonc", import.meta.url), "utf8"));
  assert.equal(config.vars.MODE, "dispatch");
  assert.deepEqual(config.triggers.crons, ["*/20 0-2,19-21 * * *"]);
  assert.equal(config.workers_dev, false);
  assert.equal(config.preview_urls, false);
  assert.equal(config.kv_namespaces, undefined);
  assert.equal(config.durable_objects, undefined);
});

test("JST calendar and readiness floors are derived from the exact occurrence", () => {
  const first = occurrence(Date.parse("2026-11-22T19:20:00Z"), at("04:20"));
  assert.equal(first.targetDate, "2026-11-23");
  assert.equal(first.readyAfter, at("04:00"));
  const second = occurrence(at("09:40"), at("09:40"));
  assert.equal(second.readyAfter, at("09:25"));
});

test("hours-late same-day delivery preserves every slot and occurrence", async () => {
  for (const clock of ["04:20","04:40","05:00","05:20","05:40","06:00","06:20","09:20","09:40","10:00","10:20","10:40","11:00"]) {
    const network = mock();
    const result = await tick(env, at(clock) + 7197, {
      fetcher: network.fetcher, now: () => at("23:59"),
    });
    assert.equal(result.status, "DISPATCH_ACCEPTED");
    assert.equal(result.request_id.slice(-4), clock.replace(":", ""));
    assert.equal(result.slot, ["04", "05", "06"].includes(clock.slice(0, 2)) ? "0900" : "1400");
  }
});

test("freshness thresholds still apply to every 20-minute recovery tick", async () => {
  const floors = [
    ...["04:20","04:40","05:00","05:20","05:40","06:00","06:20"].map((clock, i) => [clock, i === 0 ? "04:00" : "04:25", "0900"]),
    ...["09:20","09:40","10:00","10:20","10:40","11:00"].map((clock, i) => [clock, i === 0 ? "09:00" : "09:25", "1400"]),
  ];
  for (const [clock, floor, slot] of floors) {
    for (const offset of [-1, 0]) {
      const generated = new Date(at(floor) + 9 * 3600_000 + offset).toISOString().replace("Z", "+09:00");
      const network = mock({ status: hint(sheet({ generated_at_jst: generated }), slot) });
      const result = await tick(env, at(clock), { fetcher: network.fetcher, now: () => at("22:00") });
      assert.equal(result.status, offset ? "DISPATCH_ACCEPTED" : "READY_MSM");
    }
  }
});

test("all 13 scheduled checks have unique IDs, including the last 06:20 and 11:00", async () => {
  const seen = new Set();
  for (const [slot, clocks] of [["0900", ["04:20","04:40","05:00","05:20","05:40","06:00","06:20"]], ["1400", ["09:20","09:40","10:00","10:20","10:40","11:00"]]]) {
    for (const clock of clocks) {
      const network = mock();
      const result = await tick(env, at(clock), { fetcher: network.fetcher, now: () => at(clock) });
      assert.equal(result.status, "DISPATCH_ACCEPTED");
      assert.equal(result.slot, slot);
      assert.equal(result.request_id, `worker-20261123-${slot}-${clock.replace(":", "")}`);
      assert.ok(!seen.has(result.request_id));
      seen.add(result.request_id);
      assert.equal(network.posts().length, 1);
    }
  }
  assert.equal(seen.size, 13);
});

test("five extra Cron ticks are deliberate no-ops without any network request", async () => {
  for (const clock of ["04:00", "06:40", "09:00", "11:20", "11:40"]) {
    const result = await tick({ ...env, GITHUB_TOKEN: undefined }, at(clock) + 7197, {
      now: () => at(clock) + 7198,
      fetcher: () => { throw new Error("NOOP must never fetch"); },
    });
    assert.equal(result.status, "SKIPPED_CRON");
    assert.equal(result.publication, "NOT_VERIFIED");
  }
  for (const clock of ["04:10", "06:30", "09:10", "11:10", "11:59"]) {
    await assert.rejects(tick(env, at(clock), { now: () => at(clock),
      fetcher: () => { throw new Error("invalid tick must never fetch"); } }), /INVALID_OCCURRENCE/);
  }
});

test("new retry IDs are checked for duplication against completed history", async () => {
  for (const [slot, clock] of [["0900", "06:20"], ["1400", "11:00"]]) {
    const hhmm = clock.replace(":", "");
    const network = mock({ recent: [historyRun(hhmm, clock, 7, {
      display_title: `weather-worker/2026-11-23/${slot}/worker-20261123-${slot}-${hhmm}`,
    })] });
    const result = await tick(env, at(clock), { fetcher: network.fetcher, now: () => at(clock) });
    assert.equal(result.status, "DUPLICATE_REQUEST");
    assert.equal(network.posts().length, 0);
  }
});

test("history paginates through 300 failures and finds a duplicate on the last page", async () => {
  const runs = Array.from({ length: HISTORY_LIMIT * HISTORY_PAGES }, (_, i) => historyRun("0420", "04:20", i + 1));
  for (const duplicate of [false, true]) {
    const recent = structuredClone(runs);
    if (duplicate) recent.at(-1).display_title = historyRun("0440").display_title;
    const network = mock({ recent });
    const result = await tick(env, at("04:40"), { fetcher: network.fetcher, now: () => at("22:00") });
    assert.equal(result.status, duplicate ? "DUPLICATE_REQUEST" : "DISPATCH_ACCEPTED");
    assert.equal(network.calls.filter(call => new URL(call.url).searchParams.has("page")).length, HISTORY_PAGES);
  }
});

test("missing, shifting, repeated or over-budget pages fail closed", async () => {
  const runs = Array.from({ length: 31 }, (_, i) => historyRun("0420", "04:20", i + 1));
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
    await assert.rejects(tick(env, at("04:40"), { fetcher, now: () => at("22:00"), sleep: async () => {} }));
    assert.equal(network.posts().length, 0);
    if (mode === "unavailable") assert.equal(attempts, 3);
  }
});

test("transient history reads retry twice with 1s and 3s delays, including active and later pages", async () => {
  for (const stage of ["active", "recent", "page2"]) {
    for (const failure of ["io", 408, 429, 500, 503]) {
      const runs = Array.from({ length: 31 }, (_, i) => historyRun("0420", "04:20", i + 1));
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
      const result = await tick(env, at("04:40"), { fetcher, now: () => at("22:00"),
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
    await assert.rejects(tick(env, at("04:20"), { fetcher, now: () => at("04:20"),
      sleep: async ms => delays.push(ms) }), new RegExp(`HTTP_${status}`));
    assert.equal(attempts, [429, 503].includes(status) ? 3 : 1);
    assert.equal(delays.length, attempts - 1);
    assert.equal(network.posts().length, 0);
  }
});
