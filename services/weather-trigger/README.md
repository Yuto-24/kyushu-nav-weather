# 九州気象紙面の補完起動 Worker

GitHub Actions の日次更新を補う、紙面専用の独立 Worker。公開された小さな `/status.json` と GitHub の実行履歴だけを読み、必要なときだけ `weather-daily.yml` を起動する。MSM/GSM の取得・GRIB デコード・計算・描画・Pages 公開は Actions 側で行う。

このディレクトリの設定は **`MODE=observe`、Cron 空、HTTP 入口なし**。既存 NavMate の Worker、KV、GitHub workflow、公開サイトは変更しない。テストとローカル bundle の成功は、本番に配置されたことや定刻更新の成功を意味しない。

2026-11-22に、同じソースと上記設定を専用Workerへ配置した。`STATUS_URL` は `https://weather.yuto24.com/status.json`、対象repositoryは `Yuto-24/kyushu-nav-weather`。GitHub認証は未接続で、実status取得、dispatch、自然Cronはまだ未検証。運用状態の正本は `production/deployment.json`。

## 時刻と完成判定

| 紙面 | Actions の基本起動（JST） | Worker の補完確認（JST） | 公開締切 |
| --- | --- | --- | --- |
| 09:00 | 04:03 / 04:33 | 04:20 / 04:40 | 05:00 |
| 14:00 | 09:03 / 09:33 | 09:20 / 09:40 | 10:00 |

有効化後の Worker Cron 候補は `20,40 0,19 * * *`（UTC）。これは同じ JST 日の 04:20・04:40・09:20・09:40 に相当する。設定ファイルにはまだ適用していない。

20 分の確認は、当日の `complete` な MSM が **04:00 / 09:00 以降**に生成済みなら終了する。40 分の確認では **04:25 / 09:25 以降**を必要とする。03 時台の初期表示や、前半の古い予報だけで後半の取得確認が省略されることを防ぐ。GSM と `partial` は追加取得の候補に残す。

対象日は `event.scheduledTime` を JST に変換して決める。前日の遅着、予定から 15 分を超える遅延、締切後、予定外の分・時刻は起動しない。POST 直前にも時間を確認する。これらは締切を守るための制約で、GitHub の待ち時間や気象配信障害まで解消する保証ではない。

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

`request_id` は `worker-YYYYMMDD-SLOT-HHMM`。`HHMM` は予定された JST 時刻の `0420` / `0440` / `0920` / `0940` であり、実際の到着時刻や予報 Run ではない。同じ occurrence には同じ ID を使う。

Worker 起動の workflow `run-name` は、必ず次の文字列とする。

```text
weather-worker/2026-11-23/0900/worker-20261123-0900-0420
```

Actions 側は入力日付・slot・request ID・起動時刻・branch を検証し、同じ publication concurrency group（`cancel-in-progress: false`）で公開を直列化する。request ID の重複、rerun、古い日付、公開後の古い生成による上書きをそこで再検査する。Worker の確認だけを公開許可として扱わない。

GitHub API は既存 MSM 経路と同じ `X-GitHub-Api-Version: 2026-03-10`。HTTP 200 と `workflow_run_id`、同じ repository/run を指す `run_url`・`html_url` を確認して初めて `DISPATCH_ACCEPTED` と記録する。これは受付であり、実行完了や公開成功ではない。204、本文不正、送信後の通信断は受付未確認として扱い、POST をその invocation 内で再試行しない。

## 起動の抑制と保証範囲

1. 対象 workflow の active 状態を `queued` / `in_progress` / `waiting` / `pending` / `requested` ごとに確認する。日付で絞らず、古い承認待ちも対象にする。各問い合わせは `per_page=1` で、1 件でも存在すれば終了する。
2. 当日 JST 00:00 以降の `workflow_dispatch` 履歴を取得する。30 件・256 KiB が上限。件数不一致、次ページ、未知状態、worker 名の不正は不明として止める。
3. 同じ request ID は再送しない。当日・同 slot の worker Run が 2 件あれば追加しない。失敗・キャンセルも件数に含める。最後の当該 slot の worker 作成から 15 分以内は待つ。
4. `observe` なら `WOULD_DISPATCH` を返して終了。`dispatch` でも POST は 1 回だけ。

追加 KV / Durable Object は使用しない。GitHub 履歴がまだ見えない瞬間の同時 invocation、POST 受付不明と履歴反映の間について、厳密な一回配送や全送信試行の永続的な会計は保証しない。履歴上の「最大 2 件」は、観測済み Run の抑制である。Actions の共通 concurrency・重複 ID gate・公開前再確認が必要になる。

通常の完成判定は公開 status の 1 要求だけ。補完が必要な場合も最大 8 要求（status、active 5 状態、recent、dispatch）にとどめる。GPV、catalog、PNG、PDF の取得や hash 計算を Worker に持ち込まない。

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

## 再利用した判断

既存 NavMate の `services/msm-trigger/worker.ts`、`docs/msm_external_trigger.md`、`docs/evidence/issue-123/msm-worker-readback-2026-10-26.md` を確認し、metadata だけを読むこと、不明状態を未生成扱いしないこと、全 active 状態を見ること、POST 受付と公開成功を分けること、曖昧な POST を再送しないことを採用した。

今回の差分は固定の 1 日 2 窓・2 slot に限定できるため、履歴を用いた抑制にとどめて新規 KV/DO を作らない。厳密な配送制御を実現したと一般化しない。

一次資料:

- [Cloudflare Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/)
- [Cloudflare Workers limits](https://developers.cloudflare.com/workers/platform/limits/)
- [GitHub workflow runs](https://docs.github.com/en/rest/actions/workflow-runs)
- [GitHub workflow dispatch](https://docs.github.com/en/rest/actions/workflows?apiVersion=2026-03-10#create-a-workflow-dispatch-event)
- [GitHub schedule の遅延・取りこぼし](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
