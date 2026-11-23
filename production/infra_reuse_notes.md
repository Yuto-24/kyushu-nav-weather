# Daily weather production: existing infrastructure findings

## Initial-time readiness and overlapping editions — 2026-11-24 JST

Status: verified locally for [Issue #9: initial-time readiness and earlier recovery](https://github.com/Yuto-24/kyushu-nav-weather/issues/9); production rollout is not authorized by this implementation task. This supersedes the generation-time floors and Cron expressions in the dated records below. Current times and request contracts remain canonical in [the Worker guide](../services/weather-trigger/README.md).

A freshly generated complete MSM sheet can still contain an old Run. Readiness must compare `initial_time_jst` with the current stage's target; `generated_at_jst` validates chronology and cannot prove a new Run was acquired. The stage uses decision time, while request identity uses scheduled time, so late delivery or a queued Actions run cannot retain an obsolete target. Run existence and Last-Modified remain acquisition-layer evidence; reaching the nominal initial time is not evidence of availability. Source cutoffs remain 05:00/10:00.

At 06:15 two editions are due. Calling the whole single-slot preflight twice would both exceed the 50-subrequest worst-case budget and risk suppressing the second edition behind the first newly dispatched run. One status/active/history preflight admits the needed pair, with distinct slot-bound IDs and at most two non-retried POSTs (48 requests including all bounded GET retries). The existing `queue: max` and `cancel-in-progress: false` serialize publication without replacing a pending counterpart. Unknown history blocks both; an ambiguous POST is recorded per edition and never replayed. No atomic/exactly-once guarantee is added. See [Cloudflare limits](https://developers.cloudflare.com/workers/platform/limits/) and [GitHub concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency), checked 2026-11-24.

Same-day history can contain the former schedule after rollout. Recognize its valid IDs only when reading history; keep the new Worker occurrence and Actions input allowlists strict. Offline tests compare Worker admission with the actual Actions validator for all minutes and both editions, compare stage/chronology decisions, and exercise the two-dispatch resource bound. Python weather/publication suites retain cutoff, fallback, digest, date and regression coverage. Provider CPU, Cron delivery, dispatch and public readback still require separate production evidence.

## Extended bounded recovery windows — 2026-11-23 JST

Historical decision for PR #8; its schedule and freshness floors are superseded by Issue #9 above.

Observation/decision: the user requested 20-minute recovery opportunities for the 09:00 edition through JST06:30 (last check 06:20), and the 14:00 edition through JST11:00 inclusive (last check 11:00). The previous two opportunities per edition were insufficient when GitHub or weather sources were transiently delayed. The existing complete MSM and generation-time floors remain unchanged: first checks at 04:20/09:20 use 04:00/09:00; later checks use 04:25/09:25. Source cutoffs remain 05:00/10:00 and are not recovery deadlines. Same-day late delivery remains valid; no past-day publication is enabled.

Cloudflare Free permits only five Cron Trigger expressions across the account ([limits](https://developers.cloudflare.com/workers/platform/limits/)). Six exact time-group expressions would exceed that quota, potentially affecting unrelated Workers. One `*/20 0-2,19-21 * * *` UTC expression covers 18 nominal ticks; five extra ticks (JST04:00, 06:40, 09:00, 11:20, 11:40) must be explicit no-I/O no-ops, leaving 13 decision opportunities. The Worker and Actions allowlists must match each other and the deploy-workflow config guard. Requests remain idempotent by nominal HHMM, not arrival time. This is a schedule design decision, not proof that every tick will execute or dispatch.

## Same-day recovery after scheduler delays — 2026-11-23 JST

Status: verified locally for [PR #5: automatic update and recovery](https://github.com/Yuto-24/kyushu-nav-weather/pull/5); production rollout and provider CPU acceptance are pending. Applies to Worker admission, Actions day/edition identity, and publication ordering. The current operation contract is in [the Worker guide](../services/weather-trigger/README.md) and [production operations](README.md); older timing restrictions below describe earlier deployments.

The PR's incident record supplied scheduledTime values 1795375207197, 1795376407197, and 1795393207197 (JST04:20:07.197, 04:40:07.197, 09:20:07.197). Exact-second matching rejected them before I/O. Reusing the original PR correction preserved minute-based identity, `return_run_details: true`, and safe exception codes. The additional design decision separates admission time from source cutoff: late execution remains valid within the same JST day, while source availability stays bounded by 05:00/10:00. First/second-check freshness floors were unchanged in PR #5 (superseded by Issue #9 above). Reinstating a cooldown or failure-count cap would prevent a different valid occurrence from recovering.

The history bound is a resource budget, not a dispatch quota. Thirty rows per page and ten pages allow 300 records while reserving enough subrequests for five active-state checks, two transient retries per GET, status and one POST (at most 47). [Cloudflare's documented Free limit](https://developers.cloudflare.com/workers/platform/limits/) is 50 subrequests; [GitHub's filtered history limit](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-workflow) is 1,000 results. Stay below both. Incomplete/changing page counts, repeated IDs or excess history must stop admission, not silently truncate. GET retries cannot establish POST delivery: an uncertain POST is never retried automatically. Atomic history snapshots and exactly-once delivery remain outside this architecture.

The same-day test fixtures exposed two differences from the former operational wording. Actions claimed to suppress Worker reruns but checked only smaller run IDs, which do not catch another attempt of the same run. Preparation now validates `run_attempt` as well. Publication wording promised generation ordering, but the shared check compared image-generation time only. Pages now also rejects older weather-generation time for the same day/model, while retaining the accepted GSM-to-MSM preference and terrain-only exceptions. These checks close gaps in the requested deduplication and no-regression behavior; they do not change weather values or introduce state storage.

Evidence: `worker.test.mjs` covers the incident times, delayed delivery, retained freshness floors, all active states, duplicates, 300-row pagination, transient exhaustion and uncertain POST; `test_actions_job.py` covers late creation/queueing, date/branch/request identity, pagination and reruns; `test_weather_pipeline.py` confirms late generation retains both cutoffs; `test_pages_publication.py` covers late two-edition assembly, weather-generation regression and complete asset validation. Tests use offline fixtures, not live provider mutation. Actions schedule payload identifies a cron expression, not its intended calendar occurrence: run creation date is the available JST date anchor, and cannot recover an intended prior day if creation itself crosses midnight.

## Accepted Cloudflare operation — 2026-11-22 JST

The user explicitly selected a Cloudflare Worker that externally supplements GitHub Actions, using the same operational pattern as the existing MSM pipeline. This supersedes the unaccepted/provider-review status below. The implementation now uses dedicated Actions/Pages/Worker resources, not Vercel.

Actions schedules at JST04:03/04:33 and09:03/09:33 always reselect the latest available source. Worker ticks at04:20/04:40 and09:20/09:40 check real weather-generation time, not image regeneration time. The later tick requires a check since minute25 so an early complete olderRun cannot conceal a missing later scheduled job. GitHub history is only an admission hint; the final deduplication/publication is serialized in Actions.

Review caught two concrete publication errors before deployment: candidate-build time cannot establish actual public availability, so deadline claims move to the complete public readback result; and candidate date/base identity must be rechecked after dependency installation immediately before upload. Another correction preserves source preference when a newer valid Run has terrain-only exclusions: these exclusions must not pin the publication to an older complete Run. True missing/source failures still prevent publication.

A separate private repository `Yuto-24/kyushu-nav-weather` and dedicated Pages/Worker resources now serve `https://kyushu-wx.yuto24.com`. The user selected this Kyushu-specific hostname after the initial setup; the former `weather.yuto24.com` Pages association and CNAME were removed. Both dedicated credentials are connected. Actions run [45001148511](https://github.com/Yuto-24/kyushu-nav-weather/actions/runs/45001148511) generated both editions from MSM and completed full public readback at 17:05:59 JST on 2026-11-22. This afternoon manual publication did not meet either morning deadline. Actions automation and Worker dispatch/Cron are enabled; the first natural Cron, Worker POST, and provider CPU acceptance remain unverified. Current state is recorded in `deployment.json`. No existing NavMate resources were changed.

### Credential and dashboard observations — 2026-11-22 JST

- The account-token form exposes Pages Write at account scope. Its narrower resource selectors include individual Workers but no individual Pages project. The intended deployment is limited by workflow configuration to `kyushu-nav-weather`; this does not make the credential itself project-scoped. Approval must disclose the account-wide Pages capability and the target GitHub repository secret.
- GitHub required sudo-mode reauthentication before opening the fine-grained token form. This is distinct from the signed-in session that could edit ordinary Actions variables. Existing sealed credentials are not a substitute and were not extracted.
- In this session, clicking the token form's copy button did not reliably replace the clipboard: the initial Pages secret received stale configuration text and failed before the request with an invalid-header-value error. Rotating the dedicated token, checking the transfer shape without printing the value, saving only to its approved secret destination, and rerunning the failed job resolved it. For the dedicated GitHub token, the visible newly generated field supplied the value after copying remained empty. No sealed existing credential was extracted. This is a dated transfer observation, not a claim that every clipboard or provider is unreliable.
- Cloudflare Quick Edit reported a TypeScript diagnostic for the omitted `TextDecoder` `ignoreBOM` option, while accepting and activating the unchanged JavaScript module. Editor contents matched the source after CRLF normalization before deployment. A later provider preview successfully decoded actual status and GitHub responses with the existing decoder. The editor diagnostic alone did not justify changing tested runtime code.

### Provider read-path observation — 2026-11-22 JST

The production occurrence gate rejects an invocation outside the fixed 04:20/04:40 and 09:20/09:40 JST windows before network access. An afternoon test cannot establish real network behavior merely by invoking that gate. An unpublished Quick Edit preview instead used the real bindings and current clock, reusing the existing bounded request, decoding, status, and history validators. It made only GET requests: status (1,583 bytes), five active-state queries (36 bytes each), and recent history (25,964 bytes) all returned HTTP 200 at 08:07:53 UTC. No active runs or conflicting recent decision were observed. No dispatch was attempted, and no production occurrence was executed.

The temporary preview handler was removed and the original source restored exactly after newline normalization; it was never deployed. The production module remained unchanged. This verifies the provider read paths and GitHub read permission only. The preview exposed no CPU timing; elapsed time is not CPU evidence, and GET success does not prove the Actions write path, dispatch receipt, natural Cron delivery, or deadline completion. Configuration activation is recorded separately from those pending acceptance observations.

### Post-upload status mismatch — observation, 2026-11-22 JST

On main commit `3ed7e21`, [weather-daily run 45024236501](https://github.com/Yuto-24/kyushu-nav-weather/actions/runs/45024236501) successfully uploaded candidate `pages-c168698b0572a45cdd3daaf3`. Wrangler reported deployment complete at 21:50:09.716 JST; the immediate one-shot status comparison failed at 21:50:09.976. At 21:54 JST, cache-busted reads from the custom domain, the project pages.dev hostname, and the deployment-specific hostname all returned that exact candidate identity and identical status bytes. The custom-domain response had `Cache-Control: no-store` and `CF-Cache-Status: DYNAMIC`.

The verified application defect is treating the first post-upload mismatch as a final publication failure without a propagation allowance. These observations are consistent with a brief serving transition; the failed run did not retain its observed status, so they do not establish which deployment it saw or whether routing propagation or another cache layer caused it. A single occurrence does not establish a universal Cloudflare delay or justify changing cache rules or credentials.

Waiting is limited to post-upload verification with a known expected candidate. Exact status bytes and a complete coherent readback remain required; the analogous-failure audit below expands which temporary failures may restart a complete attempt. Keep diagnostics on stderr because stdout is parsed as the workflow's JSON publication proof. Deadline assessment must use successful readback completion after any wait. For a failure after successful upload, compare the logged candidate identity with all three origins before regenerating or deploying again; an Actions failure does not imply that the upload was rolled back.

### Analogous-failure audit — verified code gaps, 2026-11-22 JST

The user requested a comparison with NavMate and fixes for the same failure class in this PR. Applicable prior evidence is [NavMate PR #227](https://github.com/Yuto-24/NavMate/pull/227) (one-shot canonical mismatch after successful upload), [#264](https://github.com/Yuto-24/NavMate/pull/264) (matching release metadata did not prove the served catalog matched), and [#450](https://github.com/Yuto-24/NavMate/pull/450) (a 30-second full-transfer cap discarded useful progress until the 300-second overall budget expired). The current `wait_for_candidate.py` gives a complete payload pass the remaining total budget. This audit changes only Kyushu publication code, not NavMate or either provider's settings.

At Kyushu PR #4's initial commit `555e6e5`, offline regressions demonstrated three gaps before the correction: a new status with an old pointer failed without retry; a newly referenced file returning a temporary 404 failed without retry; and old `app.js`, `config.json`, or bootstrap bytes still yielded `verified: true` because frontend hashes were never checked against served files. These fixtures establish implementation behavior, not additional observed Cloudflare outages. HTTP response tests additionally cover 503 recovery, truncated `Content-Length`, interrupted bodies, and continuously trickling data that would evade a socket timeout alone.

The correction keeps each successful verification complete, with no files reused from failed attempts. Mutable-file transitions and explicitly classified temporary GET failures may restart within one retry window; invalid paths/schema/size, TLS/auth/redirect failures and immutable weather corruption remain failures. Total wall time, request count and requested response capacity are shared across attempts. Each request reserves its maximum permitted response before I/O, so failed and partial downloads cannot escape the cumulative capacity bound. A full progressing pass is not killed at a short per-attempt deadline. The workflow's five-minute step bound also terminates blocking I/O; chunk-clock checks reject a late result without claiming publication success.

Two frontend details prevent a naive extension from causing another false failure. Pages redirects `/index.html` to `/`, so root is selected explicitly while general redirects remain forbidden. The actual custom-domain HTML differs from the candidate only by one Cloudflare Analytics insertion immediately before `</body>`. Its exact version, integrity value, public site identifier and bytes match the second reviewed insertion in NavMate's `app_release.py`. Only that fixed single insertion is accepted; duplicates, moved/unknown snippets or any other HTML change fail. A future provider snippet change needs a new evidence-based review rather than automatic allowlist learning. [Cloudflare documents this automatic HTML injection](https://developers.cloudflare.com/web-analytics/get-started/).

Finally, weather restoration intentionally contains no frontend files even though its status retains their hashes. Candidate build, predeploy and expected-directory checks now verify all seven local frontend files explicitly; restored prior weather keeps its original contract. Public verification reads the five files used by the viewer, leaving `_headers` and `404.html` as local-only checks. The snapshot request bound accounts for these distinct sets. The root normalization is for verification only and never modifies uploaded HTML, provider configuration, weather values or the printed output.

## Provider-choice review — 2026-11-22 JST

The user asked why a second hosting provider was selected while their custom-domain sites already use Cloudflare Pages. The earlier Vercel choice was driven by the authenticated project/deployment tools available in this session and the prospect of combining Python, scheduling and storage. It was not based on measured superiority in deadline reliability, native-runtime compatibility or cost. The new Vercel runtime and Blob publication remain unverified.

The preferred recommendation from this review is to reuse the existing architecture: run the standalone Python generation in GitHub-hosted Actions and publish the completed static directory to a separate Cloudflare Pages project under the existing domain. Cloudflare's official CI Direct Upload workflow supports this. The native Python/GRIB requirement is a reason to select a suitable generation environment, not a reason to change the static hosting provider. No R2 or Vercel Blob store is inherently required when all completed public files are deployed with the Pages site.

This is a recommendation in response to a request for rationale, not an accepted migration decision. No provider configuration, publication or source implementation was changed by the review. A separate Pages project avoids replacing NavMate's full deployment; using a path within the existing NavMate project would instead require coordinated inclusion in that project's complete static artifact. Actions scheduling still needs completion checks and retry lead time; neither provider choice establishes a hard 05:00/10:00 completion guarantee. Preserve the already implemented weather calculation, renderer and viewer when comparing deployment adapters.

Evidence: [Cloudflare CI Direct Upload](https://developers.cloudflare.com/pages/how-to/use-direct-upload-with-continuous-integration/), [Python Workers package compatibility](https://developers.cloudflare.com/workers/languages/python/packages/), [GitHub schedule delivery limits](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule), and the existing NavMate `docs/static_production.md` / `.github/workflows/static-production.yml`. This review does not assert that the existing optional MSM trigger Worker is currently active.

Checked 2026-11-21 17:56 UTC / 2026-11-22 02:56 JST. This is a read-only investigation of the existing NavMate repository and public sources; no NavMate files, production settings, workflows, credentials, or scheduled jobs were changed.

## Choice for this sheet

Use the existing task's native Python RISH fetch/decode path for each daily sheet. It already supplies the pressure-grid fields needed by the accepted representative-leg calculation. Keep source selection and numerical processing in an external scheduled Python job, with the resulting dated PNG/PDF and metadata published at a fixed static-site URL. A static browser page alone cannot run on a schedule without an execution service. The user explicitly requires external serverless operation; ChatGPT Automations and Sites recurring tasks are outside the accepted deployment design.

The existing NavMate public prepared feed is technically reusable but should not be a mandatory dependency for the morning deadline: it was already past its catalog expiry in this observation while newer RISH data were available. The feed remains useful as an optional distinct acquisition path if its real Run/time coverage and hash are verified. Do not copy its timestamps to make it appear fresh.

## Sources and observed access

| Source | Observation | Contract |
| --- | --- | --- |
| `https://database.rish.kyoto-u.ac.jp/arch/jmadata/data/gpv/original/2026/11/21/` | HTTP 200 through Python HTTPS in this workspace | Daily original GRIB listing; no browser CORS assumption |
| `https://navmate.yuto24.com/weather/msm/catalog.json` | HTTP 200; 940,232 bytes | Schema 1; `generated_at`, `expires_at`, `listings`, `assets` |
| `https://navmate.yuto24.com/weather/msm/47279d76691916bbe55d844aebdf4b7bf6ca3ca710654daf97a18f7e0d0f7331.npz` | HTTP 200; 7,428,486 bytes; catalog SHA-256 matched | Latest observed Run `20261121060000` |
| `https://navmate.yuto24.com/weather/gsm/catalog.json` | HTTP 404 | GSM support exists in code, but a public GSM feed was not present |

The observed MSM catalog was generated at `2026-11-21T11:32:53.333052Z`, expired at `2026-11-21T17:32:53.333052Z`, and contained 51 retained assets and 11 daily listings. The observation occurred at `2026-11-21T17:56:11Z`; an expiry is a freshness warning, not proof that all covered forecast values are unusable.

Requests with an explicit `User-Agent: NavMate-daily-source-audit/1.0` succeeded. A separate default-urllib User-Agent request returned HTTP 403. This single observation does not establish the cause or a universal policy; retain an explicit identifying User-Agent in the acquisition code.

## Existing schemas and calculation reuse

NavMate's catalog asset is `{run, file, sha256, bytes}`. `file` must equal `<sha256>.npz`, and each compressed asset is bounded to 32 MiB. The payload is the public `jma-gpv-weather==0.6.0` `MsmPreparedData`/`GsmPreparedData` format, not the task's compact historical-subset schema. Parse it with the matching library and verify its hash and actual coverage.

Prepared-data records have keys `(aware valid time, pressure level hPa, variable)` and arrays `(values, latitude, longitude)`. Pressure variables are `hgt` metres, `tmp` kelvin, and `u`/`v` metres per second. The metadata preserves original source file identities and SHA-256. The archive includes shared grid arrays, `metadata.npy` containing JSON bytes, and numbered record arrays. Do not treat a catalog entry alone as evidence that every desired valid time or pressure level exists.

Relevant reusable code and source:

- Task: `analysis/fetch_msm.py` fetches native RISH pressure subsets, records integrity hashes and explicit source/valid times, and checks GRIB boundaries, lengths and extracted identities.
- NavMate: `/workspace/scratch/13b305c5fa22/NavMate/scripts/prepare_msm_feed.py` uses the public upstream library for MSM and explicit on-demand GSM generation.
- NavMate: `src/autonavlog/weather/local_msm.py` defines the catalog and prepared-asset schema.
- Installed source: `/workspace/scratch/13b305c5fa22/.ci-release-only-452-venv/lib/python3.12/site-packages/jma_gpv_weather/`.

GSM current-library source contract: `Z__C_RJTD_<YYYYMMDDHHMMSS>_GSM_GPV_Rjp_Gll0p1deg_L-pall_FD0000-0100_grib2.bin` covers hours 0–24. `FD` encodes days and hours, not a plain forecast-hour number. GSM Run hours are 00/06/12/18 UTC; pressure records are every three hours through hour 132. The library supports longer horizons (264 h for 00/12 UTC, 132 h for 06/18 UTC). Source: installed `gsm/spec.py`, documented there against JMA specification No.12501 and corrected TI 601. The daily weather agent is independently verifying live GSM file availability.

## Existing production schedule: reuse boundaries

NavMate's `.github/workflows/static-production.yml` declares `23 * * * *`; hourly operation is conditional on `STATIC_AUTOMATION_ENABLED`. The workflow renews the approved app's feed, builds and validates the entire static deployment, and shares `static-production` concurrency. This is an existing production system, not an authorized place to silently add the new sheet pipeline.

`docs/local_weather.md` describes normal production feed generation with `--hours 25 --full-run`: usually up to three current Runs, each extended to the library's actual forecast horizon; older assets are retained for seven days. Its catalog lifetime is six hours. GSM generation is explicitly on-demand and is not included in the ordinary MSM workflow.

`docs/msm_external_trigger.md` describes a lightweight Worker that supplements missing GitHub schedules. It does not solve RISH listing/GRIB timeouts. Current enablement and provider execution state were not inspected; source code is not proof that a scheduler is currently active.

For the requested deadlines, trigger acquisition with enough lead time for retries, and publish only a completed coherent generation. Set the target date using Asia/Tokyo independently of the host timezone. Retain the old completed sheet on acquisition failure but never relabel its date or generation time as today's successful update. A retry or scheduled trigger is not a guaranteed completion time.

## Prior observations worth preserving

- `docs/local_weather.md`: actual Windows Edge HTTPS/HTTP and Range requests to RISH were unreadable because of CORS (2026-09-16). Moving GRIB decoding into a browser, changing HTTP to HTTPS, or adding a Range header did not resolve that observed browser path. Native Python is a different path and succeeded here.
- `docs/knowledge/entries/msm-verification-budget.md`: a 30-second whole-process budget repeatedly restarted full retained-payload verification; a final filename in a timed-out log was not proof that the file was corrupt. Download only the required new Run assets for this sheet and bound the total operation separately from the socket timeout.
- `docs/evidence/issue-354-msm-discovery-2026-11-04.md`: independent listing/discovery in preflight and generation can choose different Runs; root cause of that incident was not proven. Preserve selected Run and source identity through fetch, calculation, publication and readback, rather than silently rediscovering during each phase.
- Native Python dependencies include `pygrib` and `jma-gpv-weather`; a recurring task must restore a reproducible runtime and durable source bundle. The existing scratch directory or a one-off venv path is not a long-term deployment contract.

These are scoped observations, not modifications to NavMate's rules. All new scheduling and publication decisions belong to this separate daily-sheet task.

## External serverless deployment follow-up

### Verified access and existing infrastructure

The source repository's actual upstream is `https://github.com/Yuto-24/NavMate.git`. The task's reused checkout has a local scratch checkout as its immediate `origin`; it must not be mistaken for the external deployment remote. GitHub connector metadata returned public visibility, `main` as the default branch, and admin/push permissions on `Yuto-24/NavMate`. The connector can list the user's repositories. This does not establish that a new repository can be created through the exposed connector: no create-repository, Pages-settings, workflow-dispatch, or arbitrary mutation endpoint was exposed in this session.

The canonical NavMate site is `https://navmate.yuto24.com`, Cloudflare Pages project `navmate`. `navmate.pages.dev` is documented as a former transition entry, not the canonical URL. Its worker source is `services/msm-trigger/worker.ts`, a metadata-only scheduler that reads a freshness hint and dispatches `static-production.yml`; Python acquisition/decode/build runs in GitHub Actions, not the Worker. The checked-in Wrangler template uses `MODE=observe` and empty `crons`; actual provider activation cannot be inferred from this template.

This session has GitHub connector access, but `gh auth status` reports no authenticated host. No GitHub, Cloudflare, or AWS credential environment variable was present in the narrowly checked names, and no standard CLI auth config was present. Cloudflare/AWS dedicated tools were not exposed. Existing provider credentials were not read or printed, and existing secrets cannot be assumed transferable to a new project.

Vercel's authenticated tools are available: `list_teams` returned `yuto24s-projects`, and `list_projects` returned no projects. New-project and deployment tools exist, but the returned team object did not expose a billing plan. No Vercel project or deployment was created. This is an available secondary provider path, not evidence that the native weather runtime or appropriate scheduling plan has already been validated there.

### Implementation choices

1. **Shortest native-Python path: independent GitHub repository + GitHub-hosted Actions + GitHub Pages.** Use Ubuntu and Python 3.12, install pinned native-compatible dependencies and fonts, run acquisition/calculation/render, and deploy only the resulting static directory. This adds no continuously running server, no calculation on page access, and does not require a separate Cloudflare token. Suggested independent repository name is `kyushu-nav-weather`; the default project-site URL would normally be `https://yuto-24.github.io/kyushu-nav-weather/` after actual repository/Pages creation and readback. That URL is proposed, not deployed. The new project must not replace the existing `yuto-24.github.io` site's content. GitHub Pages needs its source enabled for Actions once; deployment permissions are `pages: write` and `id-token: write`, with the `github-pages` environment. See [GitHub Pages custom workflows](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages).
2. **Same runner, independent Cloudflare Pages site.** Run the same Python job in Actions and upload the completed directory using `wrangler pages deploy <directory> --project-name <new-project> --branch main`. Create a separate Pages project; its fixed `<project>.pages.dev` hostname requires no change to `navmate.yuto24.com` or DNS. An account-scoped Pages Edit credential must be provisioned for that new job through the provider/Secrets UI; do not read an existing secret back into chat. Direct Upload can create and deploy a project; it is distinct from Git integration. See [Cloudflare Direct Upload](https://developers.cloudflare.com/pages/get-started/direct-upload/) and [CI upload](https://developers.cloudflare.com/pages/how-to/use-direct-upload-with-continuous-integration/).
3. **Scheduler with explicit delivery/retry controls: EventBridge Scheduler + Lambda container + S3/CloudFront.** Python 3.12 has an official Lambda AL2023 base image; a container can carry pygrib/ecCodes, NumPy, ReportLab, PyMuPDF, fonts and the fixed configuration. Scheduler uses `Asia/Tokyo`, flexible window OFF, bounded delivery retries and a DLQ. The Lambda invocation is asynchronous, so a successful Scheduler API delivery does not by itself prove the weather job or publication succeeded; the function needs its own retry/failure handling and an externally readable successful-generation status. This requires new AWS account access, IAM roles, ECR, storage and publication configuration, none of which is currently available through this session's tools. It is a coherent deadline-oriented design, but not the shortest deployment from the verified existing account access. Sources: [Scheduler precision and timezones](https://docs.aws.amazon.com/scheduler/latest/UserGuide/schedule-types.html), [Lambda scheduled invocation](https://docs.aws.amazon.com/lambda/latest/dg/with-eventbridge-scheduler.html), [Python container images](https://docs.aws.amazon.com/lambda/latest/dg/python-image.html).

### Deadline and retry design

[GitHub's primary documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule) explicitly states that scheduled events can be delayed and, at sufficiently high load, queued jobs can be dropped. Avoiding the start of the hour reduces a known contention point but is not a deadline guarantee. Only the default branch runs; public-repository scheduled workflows are disabled after 60 days without repository activity. A daily Actions run alone should not be assumed to prevent that inactivity shutdown.

An initial schedule proposal for the two requested deadlines is:

| Forecast / deadline | Attempts in JST | Plain UTC cron expressions shared across both sheets |
| --- | --- | --- |
| 09:00 / 05:00 | 03:17, 03:47, 04:17, 04:37 | `17,47 18,23 * * *` and `17,37 19,0 * * *` |
| 14:00 / 10:00 | 08:17, 08:47, 09:17, 09:37 | Same two expressions; select the sheet from scheduled event/explicit job input |

These are proposed buffer/retry times, not a completed configuration or a proven service level. Run the weather operation with a total deadline (e.g. 15 minutes), leaving publication/readback time before the stated sheet deadline. Derive the target date and slot consistently from the scheduled target, not merely the eventual runner start time, because a delayed event may cross the next slot or date. Do not let a delayed older job overwrite a newer successful generation.

Use a lightweight public `status.json` to identify target date, forecast time, source Run, generation time and output digests. Subsequent attempts should return quickly when the required generation is already valid; otherwise they retry missing work. Serialize publication, compare the current successful generation before replacing it, and publish one coherent set of PNG/PDF/metadata. Limit retry work rather than reprocessing an entire seven-day retained archive. A fresh GSM fallback and later MSM recovery need an explicit source preference comparison; simple 'already exists today' checks would otherwise prevent the requested MSM preference from recovering.

For additional trigger resilience, an independent lightweight Cloudflare Worker can check `status.json` shortly before each deadline and call `workflow_dispatch` if no successful generation and no active job exists. Reuse the existing **pattern**, not NavMate's production Worker or repository. This still cannot remove GitHub runner queueing or source outages. [Cloudflare Cron documentation](https://developers.cloudflare.com/workers/configuration/cron-triggers/) specifies UTC and warns that new/changed schedules can take up to 15 minutes to propagate; create schedules well before first required execution.

Vercel is not an automatic improvement merely because its deployment tool is connected. Its [legacy Cron documentation](https://vercel.com/docs/schedules/cron-jobs/usage-and-pricing) gives Hobby one execution per day per job and per-hour timing; Pro/Enterprise have per-minute timing. [Cron delivery documentation](https://vercel.com/docs/schedules/cron-jobs/manage-cron-jobs) describes best-effort delivery, possible duplicate/missed runs and no failed-invocation retry. Any Vercel route would need matching native dependency and durable output validation; the current investigation does not establish those.

### Provisioning boundary

All files and the production workflow can be prepared locally before asking the user to take an account-authentication step. The present concrete missing capability for the minimal GitHub Pages design is creating/enabling the independent repository/Pages source with an authenticated provider session. Existing repo admin metadata does not substitute for an exposed creation or settings operation. No unrelated existing repository should be repurposed merely to avoid this boundary.

## Vercel Schedules: previously selected connected-provider path

The legacy Cron limits above do **not** apply to the newer Vercel Schedules product. Primary documentation checked during this task describes Schedules as public beta on all plans, with static configuration deployed in `vercel.json`. Given the verified Vercel connection and new-project/deployment capabilities, this is a practical external-serverless path that avoids the unresolved GitHub repository-creation step. No ChatGPT automation is involved.

### Verified product behavior

- [Schedules overview](https://vercel.com/docs/schedules): static targets can be a private function, deployment HTTP path, or queue topic. Dynamic schedules support queue targets only. Static schedules in production start active; preview schedules start inactive.
- [Schedules limits](https://vercel.com/docs/schedules/limits): Hobby permits 1,000 schedules/team, up to 100 static schedules/deployment, and 5,000 scheduled/manual runs per billing period. Hobby's jitter is always 15 minutes and its shortest cron interval is 15 minutes. Pro/Enterprise allow 1–15 minutes of jitter, default 1 minute, and every-minute schedules. Exceeding the Hobby run/creation limit blocks/skips runs until the next billing period.
- [Schedules configuration](https://vercel.com/docs/schedules/configuration): `schedules` is an object keyed by stable names. Each schedule has `schedule`, optional `timezone` and `jitter`, and exactly one of `path`, `function`, or `topic`. Path defaults to POST; `requestMethod` can select GET. IANA timezone `Asia/Tokyo` can be used directly. Static schedules have no payload, so identify the forecast slot through the schedule name or a dedicated route.
- [Handling runs](https://vercel.com/docs/schedules/handle-runs): path delivery is an ordinary HTTP request to the application's route. Set `CRON_SECRET` and verify `Authorization: Bearer ...`. Relevant headers are `ce-vssschedulename`, `ce-vssscheduledat` (ISO occurrence time before jitter), and `ce-id` (stable execution ID across duplicate delivery). This path is language-neutral. Applying it to a supported Python HTTP function is an inference from the documented HTTP contract and Python runtime support, not a Python-specific Schedules SDK claim.
- [Schedules concepts](https://vercel.com/docs/schedules/concepts): function/path errors or timeouts are **not automatically retried**; queue consumers are retried. Delayed deliveries can catch up and duplicate deliveries are possible. A recurring occurrence expires after jitter plus the shortest schedule interval (the interval counts as at least 10 minutes). Different runs can overlap. Normal jitter is not an upper bound on all delivery failures/delays.
- [Workflow integration](https://vercel.com/docs/schedules/workflows): a durable Workflow can retain completed steps and retry failed steps, including recovery after crashes/deployments; a normal function invocation has no such checkpoint durability. Queue plus Workflow is additional machinery and is not required for an idempotent sub-five-minute batch that can be retried during a bounded morning window.
- [Python runtime](https://vercel.com/docs/functions/runtimes/python): Python 3.12 is supported and is the current default; dependencies can come from `requirements.txt` or `pyproject.toml`. Standard Python function bundle limit is 500 MB uncompressed, and Python files/dependencies are not automatically tree-shaken. Include only the pinned runtime, required fonts, and fixed map/performance inputs.
- [Function limits](https://vercel.com/docs/functions/limitations): with Fluid compute, Hobby supports 300 seconds and 2 GB / 1 vCPU. A fresh provider invocation must still verify pygrib/native-wheel loading, RISH access, total runtime and Blob publication; a local timing alone is not provider acceptance.

### Minimal proposed configuration

The following is a reviewed configuration proposal, not a record that it has been deployed:

```json
{
  "functions": {
    "api/refresh.py": { "maxDuration": 300, "memory": 2048 }
  },
  "schedules": {
    "weather-09": {
      "schedule": "0,30 4 * * *",
      "timezone": "Asia/Tokyo",
      "jitter": 15,
      "path": "/api/refresh",
      "requestMethod": "POST"
    },
    "weather-14": {
      "schedule": "0,30 9 * * *",
      "timezone": "Asia/Tokyo",
      "jitter": 15,
      "path": "/api/refresh",
      "requestMethod": "POST"
    }
  }
}
```

This provides two attempts for each sheet: 04:00/04:30 JST and 09:00/09:30 JST. The earlier 03:00/08:00 proposal was rejected: an early successful MSM publication would make later attempts no-op and could lock in an unnecessarily old source Run. Under the normal 15-minute jitter plus a maximum five-minute invocation, the last attempt finishes by 04:50/09:50, leaving ten minutes before the requested deadline. Exceptional delayed delivery and upstream outages remain possible and must not be presented as a deadline guarantee. Four occurrences per day total 124 in a 31-day month; successful no-op attempts still count as runs, but this is well below the documented 5,000-run Hobby allowance before other project usage.

The Python handler should verify the secret first, parse the stable scheduled timestamp/name, inspect the successful-generation manifest, and either return without regenerating or perform one bounded refresh. It should permit a preferred MSM result to replace a GSM fallback and must prevent stale/duplicate execution from replacing a newer generation. Write dated image/data objects first and the visible complete-generation pointer last. The HTML at a fixed project URL can stay static and read the published manifest/assets; page access performs no weather acquisition or rendering.

A private function target would avoid a public route, but the documented examples use JavaScript `handleSchedule` and require the `schedule/v1beta` trigger. Its Python-specific build acceptance was not verified. The simple HTTP path with a checked secret is the least speculative Python integration. Native Schedules-specific tools were not exposed, but static declarations are applied by ordinary production deployment, so a dedicated schedule-creation tool is unnecessary.

## Implemented publication decisions and verification boundary

The standalone implementation is in `production/publication.py`, `production/refresh_job.py`, `api/refresh.py`, and `production/schedule_config.json`. These files do not modify NavMate. `production/test_publication.py` exercises the transaction against an isolated filesystem fake; it is not evidence that provider storage is provisioned.

- **One public commit object per forecast.** The static viewer reads `latest/0900.json` or `latest/1400.json` and loads the immutable URLs in the same pointer. Weather JSON, PNG, PDF, and a release manifest are uploaded and read back before the pointer is replaced. Separately mutable image aliases were removed because two images plus metadata cannot be switched atomically by ordinary Blob PUT. The fixed page URL is the stable user entry.
- **Preserve valid completed output.** An acquisition, rendering, asset upload, or validation failure leaves the prior pointer and its immutable files in place. A response lost during pointer PUT is an ambiguous commit; the implementation reads back to determine whether the new complete pointer actually committed instead of blindly retrying. It never relabels an old sheet with a new date. Same-day complete MSM is a no-op; GSM can recover to MSM. Older dates, older generations, older Runs of the same model, and a GSM downgrade from same-day MSM are rejected.
- **Terrain is distinct from source loss.** Missing rows, nonfinite source values, unresolved source acquisition loss, and incomplete numeric fields cannot be committed as success. Valid partial rows caused only by terrain can be published with `quality=partial`; this does not receive the complete-MSM no-op treatment. Entire terrain rows are lawful exclusions. This avoids retaining yesterday's weather just because a low altitude lies below terrain.
- **Bounded concurrency.** A per-slot seven-minute lease uses create-without-overwrite, owner/expiry metadata, and conditional deletion of the observed ETag. It is intentionally longer than the function's hard 300-second execution limit. Expired lease reclamation and cleanup use conditional deletion so a stale owner cannot remove a successor. The official Python SDK lacks destination-conditional PUT; this design depends on that bounded worker lifetime and must not be reused for an unbounded executor. Immutable files are verified before final ownership/pointer checks.
- **Time and authentication.** Only POST with `Authorization: Bearer CRON_SECRET` can start work. Schedule name/time are parsed only after authentication; the schedule's instant determines the intended JST day. An expired prior-day delivery is rejected. Source cutoff uses the actual invocation time, clamped by the weather engine to 05:00/10:00. Actual late publication remains labelled by its real timestamp and `deadline_met=false`. Public GET never starts generation.
- **Dry runtime verification.** Authenticated `POST /api/refresh?slot=0900&validate_only=1` (or `1400`) runs the real acquisition and renderer but publishes nothing. It returns coverage, source/timing metadata, and artifact digests only, with `published=false` and `storage_status=not_attempted`. This separates native runtime verification from the missing Blob provisioning step; it is not an unauthenticated public compute endpoint.
- **Retention.** After a successful commit, best-effort cleanup removes at most 1,000 recognized release objects older than 31 days. It strictly checks generated release IDs and known names; completed generations additionally require their manifest's schema/date/slot/release ID. Both latest pointers' referenced releases are protected even when unusually old. Old orphan staging without a manifest is eligible only under the same strict path and age rules. Foreign paths remain untouched. A cleanup failure does not undo the completed publication.
- **Official SDK details.** The selected `vercel-blob==0.6.2` exposes `vercel.blob.sync` and OIDC resolution using `BLOB_STORE_ID` and scoped request headers. It depends on `httpx2`, not a separately installed `httpx`. `HeadersContext` restores request headers after the invocation. Authoritative HEAD metadata and cache-busted delivery GET must have matching ETags for mutable reads. SDK context/transport/model construction was verified locally without network calls or credentials. See [Python SDK reference](https://vercel.com/docs/vercel-blob/python-sdk-reference).
- **Child runtime dependency inheritance.** Vercel can add unpacked dependency directories to the parent's `sys.path` during cold start without changing `PYTHONPATH`. A subprocess launched with only the original environment can consequently lose NumPy/pygrib even when parent imports work. The worker environment explicitly carries the current absolute `sys.path` into `PYTHONPATH`. Native work has a 250-second maximum subprocess timeout, with budget reserved for commit; every output/cache is under a fresh `/tmp` directory.

As of this local implementation, 17 filesystem/authentication integration tests pass, including interrupted upload, pointer write failure, date rollover, slot independence, GSM recovery, duplicate no-op, terrain versus missing coverage, lease takeover, retention, and unauthenticated rejection. Compilation and actual SDK context construction pass. Provider-side execution, source networking, Blob writes, and schedule activation are still unverified.

The parent created a separate Vercel project, but Blob provisioning returned provider HTTP 403. A later source upload was rejected by automatic approval review because transfer of the private map/sector geometry to Vercel had not been explicitly authorized. No alternative transfer route was attempted. This supersedes the earlier investigation snapshot that no project existed. Completion now requires an explicit user decision for that concrete external deployment and authorized Blob provisioning; neither a local test nor a configured JSON schedule implies live daily updates.
