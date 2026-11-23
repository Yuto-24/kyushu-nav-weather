# 九州気象紙面の補完起動 Worker

GitHub Actions の日次更新を補う、紙面専用の独立 Worker。公開された小さな `/status.json` と GitHub の実行履歴だけを読み、必要なときだけ `weather-daily.yml` を起動する。MSM/GSM の取得・GRIB デコード・計算・描画・Pages 公開は Actions 側で行う。

このディレクトリは **`MODE=dispatch`、Cron `*/20 0-2,19-21 * * *`（UTC）、HTTP入口なし** の本番設定を保持する。既存NavMateのWorker、KV、GitHub workflow、公開サイトは変更しない。新しい環境の初回確認では、下記手順に従ってobserve・Cronなしから始める。

2026-11-22に専用GitHub認証を接続し、`STATUS_URL=https://kyushu-wx.yuto24.com/status.json`、対象repository `Yuto-24/kyushu-nav-weather` で上記設定を有効化した。provider上の未公開previewでstatusとGitHub履歴の読み取りは成功した。previewのGET検証では本番の時刻判定・dispatchを実行していない。CPU時間、Workerからのdispatch、自然Cronは未確認であり、本番受入完了とは扱わない。運用状態の正本は `production/deployment.json`。

## 時刻と完成判定

| 紙面 | Actions の基本起動（JST） | Worker の補完確認（JST） | 公開締切 |
| --- | --- | --- | --- |
| 09:00 | 04:03 / 04:33 | 04:20〜06:20（20分間隔・計7回） | 05:00 |
| 14:00 | 09:03 / 09:33 | 09:20〜11:00（20分間隔・計6回） | 10:00 |

設定ファイルのWorker Cronは `*/20 0-2,19-21 * * *`（UTC）。Cloudflare Freeのアカウント全体でCron Triggerは5件までのため、6つの時刻別式ではなく1つの式を使う。1日18回発火し、JST 04:00・06:40・09:00・11:20・11:40の5回は `SKIPPED_CRON` として外部通信なしで正常終了する。補完判定するのは残り13回だけ。マージ後もWorkerの再デプロイが必要で、現在の本番設定を変えた証拠とは区別する。

04:20/09:20の初回は、当日の `complete` な MSM が **04:00/09:00以降**に生成済みなら終了する。04:40〜06:20/09:40〜11:00の各回は **04:25/09:25以降**を必要とする。03時台の初期表示や古い前半版で、2回目以降の更新確認が省略されないようにする。GSM と `partial` は追加取得の候補に残す。

対象日・版・要求IDは `event.scheduledTime` のJST日付と時・分から決める。秒・ミリ秒のずれは許容する。予定した補完時刻が06:20/11:00以前なら、同じJST日付の遅延実行も受け付ける。前日分、未来の予定時刻、予定外の時・分は拒否し、POST直前にも日付を確認する。

05:00/10:00は公開目標と採用する気象データのsource cutoffであり、補完起動の締切ではない。Actionsは実生成開始時刻とsource cutoffの早い方で気象データを選ぶ。遅れて復旧しても、締切内の公開実績にはしない。

### 2026-11-23 の時刻判定失敗（案件固有のObservation）

Cloudflareの本番Cronで04:20:07.197、04:40:07.197、09:20:07.197 JSTの`scheduledTime`を観測。旧実装は「秒・ミリ秒が0」を要求しており、GitHub照会前に`INVALID_OCCURRENCE`となる条件だった。PR #5の最初の修正は時・分での識別に変更したが、15分遅延上限と補完起動の締切は残していた。同PRの再設計でこれらを撤廃し、上記の当日内復旧に統一した。Cloudflare側で秒単位の揺れが常に発生するとの一般化はしない。

補完起動のGitHub workflow dispatchでは`return_run_details: true`を明示する。GitHubのAPIは未指定時に204（本文なし）を返し得るが、現行の受領検証には200と実行IDが必要。例外ログにも安全な失敗理由コードを含め、イベント失敗の根拠をCloudflare側から確認可能にする。これは本番POST成功の証明ではない。

## 公開 status の契約

同じ Pages 配布物の `/status.json` に、次の形を載せる。以下は形式例であり、本番取得の記録ではない。

```json
{
  "schema": "kyushu-pages-status-1",
  "slots": {
    "0900": {
      "valid_date": "2026-11-23",
      "quality": "complete",
      "model": "MSM",
      "initial_time_jst": "2026-11-23T00:00:00+09:00",
      "generated_at_jst": "2026-11-23T04:34:00+09:00",
      "release_id": "20261123-0900-example"
    },
    "1400": null
  }
}
```

- 両 slot キーを必須にする。未公開は `null`、取得失敗後に前回版を残した場合は前回版の実際の日付・時刻のままにする。
- `quality` は `complete` または `partial`、`model` は `MSM` または `GSM`。時刻は秒と `+09:00` を含める。
- **`generated_at_jst` は気象取得・計算の実時刻**。画像の再描画時刻、status の保存時刻、公開確認時刻で更新しない。同じ数値の初期時刻や生成時刻を書き換えて更新済みに見せない。
- 読み取りは HTTPS、redirect 禁止、no-store、8 秒、4 KiB 上限。404、HTML、過大、不正 JSON、不正 schema、未来時刻は `UNKNOWN` となり、GitHub に送信しない。
- 初回有効化の前に、両 slot が `null` でもよいので正しい status を公開しておく。404 を「未生成」と読み替えて無条件起動しない。

Worker はこの metadata を起動判断に使うだけで、画像・PDF・数値の独立した完全性検証はしない。結果にも `publication: NOT_VERIFIED` を残す。

## Actions との契約

Worker Secret `GITHUB_TOKEN` に、対象 repository の Actions read/write が可能な認証を設定する。token を source・vars・公開 status・ログへ入れない。既存 MSM Worker の Secret を自動で読み出したり複製したりしない。

`GITHUB_REPOSITORY` と `GITHUB_BRANCH` は新しい紙面用 workflow の配置先。workflow ファイル名は固定の `weather-daily.yml`。dispatch body は次の形。

```json
{
  "ref": "main",
  "return_run_details": true,
  "inputs": {
    "target_date": "2026-11-23",
    "slot": "0900",
    "request_id": "worker-20261123-0900-0420",
    "trigger": "worker",
    "publish": "true",
    "operation": "refresh"
  }
}
```

`request_id` は `worker-YYYYMMDD-SLOT-HHMM`。`HHMM` は予定された JST 時刻の09:00版 `0420/0440/0500/0520/0540/0600/0620` または14:00版 `0920/0940/1000/1020/1040/1100` であり、実際の到着時刻や予報 Run ではない。同じ occurrence には同じ ID を使う。

Worker 起動の workflow `run-name` は、必ず次の文字列とする。

```text
weather-worker/2026-11-23/0900/worker-20261123-0900-0420
```

Actions 側は入力日付・slot・request ID・起動時刻・branch を検証し、同じ publication concurrency group（`cancel-in-progress: false`）で公開を直列化する。request IDの重複、Workerのrerun、古い日付、公開後の古い生成による上書きをそこで再検査する。Worker要求の90分制限は設けない。Worker の確認だけを公開許可として扱わない。

GitHub API は既存 MSM 経路と同じ `X-GitHub-Api-Version: 2026-03-10`。HTTP 200 と `workflow_run_id`、同じ repository/run を指す `run_url`・`html_url` を確認して初めて `DISPATCH_ACCEPTED` と記録する。これは受付であり、実行完了や公開成功ではない。204、本文不正、送信後の通信断は受付未確認として扱い、POST をその invocation 内で再試行しない。

## 起動の抑制と保証範囲

1. 対象 workflow の active 状態を `queued` / `in_progress` / `waiting` / `pending` / `requested` ごとに確認する。日付で絞らず、古い承認待ちも対象にする。各問い合わせは `per_page=1` で、1 件でも存在すれば終了する。
2. 当日JST 00:00以降の `workflow_dispatch` 履歴を30件ずつ最大10ページ（300件）取得する。1ページ256 KiB以内。URLは固定のAPIとページ番号から組み立て、Link先へ認証を転送しない。総件数の変化、欠落、重複ID、nextとの不一致、上限超過、未知状態、不正なWorker名は不明として止める
3. 各履歴GETの一時的な通信失敗とHTTP 408/429/5xxは、初回を含め最大3回確認する。待機は1秒、3秒。認証エラー、不正JSON・schemaなどは即失敗し、履歴の完全性を確認できなければPOSTしない
4. 同じrequest IDは成功・失敗・キャンセルを問わず再送しない。別IDの過去の失敗や起動回数、15分以内の作成履歴は次の補完を妨げない
5. `observe` なら `WOULD_DISPATCH` を返して終了。`dispatch` でもPOSTは1回だけ

追加 KV / Durable Object は使用しない。GitHub 履歴がまだ見えない瞬間の同時 invocation、POST 受付不明と履歴反映の間について、厳密な一回配送や全送信試行の永続的な会計は保証しない。この照会は原子的な履歴snapshotではない。Actions の共通 concurrency・重複 ID gate・公開前再確認が必要になる。

通常の完成判定は公開statusの1要求だけ。補完時は履歴1ページ・再試行なしなら8要求。10ページすべてとactive 5状態を各3回確認しても、statusとPOSTを含め最大47要求となる。これはCloudflare Freeの50 subrequests枠内に収めるための上限で、300件は日次起動回数の割当ではない。GPV、catalog、PNG、PDF の取得や hash 計算を Worker に持ち込まない。

## GitHub Actionsから手動デプロイ（ローカルNode/npm不要）

`.github/workflows/deploy-worker.yml` は手動実行（`workflow_dispatch`）だけを受け付ける。対象branchは `main` のみ。既定の `mode=validate` ではNode 22で依存関係を一時インストールし、Workerテスト・構文・bundle生成を確認するが、Cloudflareへは送信しない。

初回の認証接続では、CloudflareのAPI tokenに **既存の `kyushu-weather-trigger` のみを対象とするWorkers Editor** を付与する。他のWorker、Pages、Routesへ権限を広げない。Cloudflare [Workersの権限](https://developers.cloudflare.com/workers/authorization/workers/)を参照。生成したトークンをGitHub repository `Yuto-24/kyushu-nav-weather` の Settings → Secrets and variables → Actions のSecret `CLOUDFLARE_WORKER_TOKEN` に保存する。既存Variable `CLOUDFLARE_ACCOUNT_ID` を再利用し、Pages用 `CLOUDFLARE_PAGES_TOKEN` は使わない。

[Actionsのdeploy-worker](https://github.com/Yuto-24/kyushu-nav-weather/actions/workflows/deploy-worker.yml) で `main` を選び、まず `mode=validate` を実行する。検証と承認が済んだら `mode=deploy` を選ぶ。毎回テスト・構文・bundle・デプロイ対象（名前、Cron、Vars、HTTP公開設定）を確認した後、既存のWorkerだけをWranglerで更新する。通常の `wrangler deploy` はWorkerの既存Secret `GITHUB_TOKEN` を保持し、GitHub Secretsへコピーしない。

デプロイ成功とCron復旧は別の確認。CloudflareでWorkerのVersion ID、Secret名、Cron設定を確認し、次回の自然Cron・GitHub dispatch・公開ページの読戻しを別々に検証する。Secrets未登録時は`mode=deploy`が失敗し、本番Workerを変更しない。PRのマージ自体ではWorkerはデプロイされない。

### デプロイ用Wranglerの固定版（2026-11-23の観測）

GitHub Actions run [45098209634](https://github.com/Yuto-24/kyushu-nav-weather/actions/runs/45098209634)では、Workerテスト・bundle・配備対象確認が成功した後、Wrangler `4.152.0` のdashboard-originメタデータ取得が失敗した。Cloudflare [workers-sdk #16390](https://github.com/cloudflare/workers-sdk/issues/16390)に同一エラーとWorker単位Editorトークンでの再現報告があり、[修正PR #16531](https://github.com/cloudflare/workers-sdk/pull/16531)は`wrangler@4.156.2`のリリースノートに含まれる。

`deploy-worker.yml`はbundle検証と本番deployの両方を、`npm exec --yes --package=wrangler@4.156.2 -- wrangler ...`で実行する。既存のPages更新用Wranglerとlockfileを変更しないための限定的な対応。GitHub runnerの一時環境だけで取得し、WSLへは何も導入しない。**既存WorkerのEditorトークンの権限を広げずに再実行すること。** 別のAPIエラーが出た場合に権限を闇雲に追加せず、そのHTTPステータスとWranglerログで切り分ける。実際の再デプロイ成功・Cron動作はこの修正のPRだけでは未確認。

## ローカル確認

Node.js 22 以降を使う。

```sh
npm ci
npm test
npm run check
npm run bundle
```

`bundle` は `wrangler deploy --dry-run` で `/tmp/kyushu-weather-trigger-bundle` に bundle を作るだけで、本番に送信しない。Worker は HTTP を常に 404 とし、`workers_dev`・preview URL も無効。公開の手動起動 endpoint は設けない。

本番配置前に、対象 URL/repository/Secret と Actions gate を確認する。まず `observe` のまま provider 上の status 経路・GitHub 経路の CPU と結果を確認し、その後に本番 Cron と dispatch を有効にする。既存 MSM では比較的大きな metadata の読み取りでも Free の 10 ms CPU 枠を超えた観測があり、今回のローカルテスト時間や小さい bundle だけで provider CPU 合格と扱わない。

2026-11-22の初回設定では、未公開previewで読み取り結果まで確認して有効化した。previewからCPU時間を取得できなかったため、上記のCPU確認は未了として残す。現在時刻が正規の実行窓外の場合、時刻を偽装したり本番の時刻検証を緩めたりして自然Cronの証拠を作らない。

## 再利用した判断

既存 NavMate の `services/msm-trigger/worker.ts`、`docs/msm_external_trigger.md`、`docs/evidence/issue-123/msm-worker-readback-2026-10-26.md` を確認し、metadata だけを読むこと、不明状態を未生成扱いしないこと、全 active 状態を見ること、POST 受付と公開成功を分けること、曖昧な POST を再送しないことを採用した。

対象は固定の1日13補完時刻・2 slotに限定できるため、履歴を用いた抑制にとどめて新規 KV/DO を作らない。厳密な配送制御を実現したと一般化しない。Cron式は1つのまま、20分ごとの補完判定を延長する。全補完が消失・失敗した場合や、同一IDで既に実行済みの場合は、公開先を確認してActionsの当日manual refreshで復旧する。

一次資料:

- [Cloudflare Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/)
- [Cloudflare Workers limits](https://developers.cloudflare.com/workers/platform/limits/)
- [GitHub workflow runs](https://docs.github.com/en/rest/actions/workflow-runs)
- [GitHub workflow dispatch](https://docs.github.com/en/rest/actions/workflows?apiVersion=2026-03-10#create-a-workflow-dispatch-event)
- [GitHub schedule の遅延・取りこぼし](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
