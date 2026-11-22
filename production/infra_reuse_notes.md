# Daily weather production: existing infrastructure findings

## Accepted Cloudflare operation — 2026-11-22 JST

The user explicitly selected a Cloudflare Worker that externally supplements GitHub Actions, using the same operational pattern as the existing MSM pipeline. This supersedes the unaccepted/provider-review status below. The implementation now uses dedicated Actions/Pages/Worker resources, not Vercel.

Actions schedules at JST04:03/04:33 and09:03/09:33 always reselect the latest available source. Worker ticks at04:20/04:40 and09:20/09:40 check real weather-generation time, not image regeneration time. The later tick requires a check since minute25 so an early complete olderRun cannot conceal a missing later scheduled job. GitHub history is only an admission hint; the final deduplication/publication is serialized in Actions.

Review caught two concrete publication errors before deployment: candidate-build time cannot establish actual public availability, so deadline claims move to the complete public readback result; and candidate date/base identity must be rechecked after dependency installation immediately before upload. Another correction preserves source preference when a newer valid Run has terrain-only exclusions: these exclusions must not pin the publication to an older complete Run. True missing/source failures still prevent publication.

A separate private repository `Yuto-24/kyushu-nav-weather` was created. On 2026-11-22 the authenticated Cloudflare dashboard accepted a dedicated Pages project, the supplemental Worker in observe mode without Cron/HTTP routes, and the `weather.yuto24.com` custom domain with its CNAME. Cloudflare subsequently showed the domain as active with SSL enabled at 16:28 JST; no Pages deployment had been uploaded. Credentials remain unconfigured; source, resource creation, and Worker deployment do not establish live daily updates. Current state is recorded in `deployment.json`. No existing NavMate resources were changed.

### Credential and dashboard observations — 2026-11-22 JST

- The account-token form exposes Pages Write at account scope. Its narrower resource selectors include individual Workers but no individual Pages project. The intended deployment is limited by workflow configuration to `kyushu-nav-weather`; this does not make the credential itself project-scoped. Approval must disclose the account-wide Pages capability and the target GitHub repository secret.
- GitHub required sudo-mode reauthentication before opening the fine-grained token form. This is distinct from the signed-in session that could edit ordinary Actions variables. Existing sealed credentials are not a substitute and were not extracted.
- Cloudflare Quick Edit reported a TypeScript diagnostic for the omitted `TextDecoder` `ignoreBOM` option, while accepting and activating the unchanged JavaScript module. Editor contents matched the source after CRLF normalization before deployment. This observation establishes upload acceptance only; scheduled execution and decoding remain part of provider verification. It is not a reason to change tested runtime code solely to silence the editor's type declaration.

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
