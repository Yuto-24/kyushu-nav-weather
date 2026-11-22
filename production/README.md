# 九州 NAV LOG — Cloudflare Pages と日次更新

固定A〜Jの疑似セクターによる九州共通A4資料を、毎日09:00・14:00の2版として更新する。GitHub ActionsがMSM/GSM取得・計算・描画を担当し、Cloudflare Pagesから生成済みHTML・PNG・PDF・JSONを配信する。Cloudflare Workerは更新状況を確認してActionsを補完起動する。ChatGPTや利用者PCの常時起動に依存しない。

## 運用状態

実際の状態は `production/deployment.json` に記録する。2026-11-22時点では、専用GitHub repositoryを作成し、取得・描画・Actions・補完Worker・Pages配信処理を実装した。Cloudflareの管理画面がログイン待ちのため、Pagesへの公開とWorkerのCronはまだ有効化していない。

GitHub repository: https://github.com/Yuto-24/kyushu-nav-weather

以前検討したVercel案は採用しない。既存NavMateとは独立したrepository、Pages project、Workerで運用し、既存MSMの起動・認証・配布物を上書きしない。source repositoryは非公開、完成した気象図の閲覧ページは公開する。

## 実行時刻

| 予報 | Actions定時実行（JST） | Worker補完確認（JST） | 出力締切 |
| --- | --- | --- | --- |
| 09:00版 | 04:03 / 04:33 | 04:20 / 04:40 | 05:00 |
| 14:00版 | 09:03 / 09:33 | 09:20 / 09:40 | 10:00 |

ActionsのcronはUTCの `3,33 19 * * *` と `3,33 0 * * *`。Workerは有効化時にUTCの `20,40 0,19 * * *` を設定する。

定時実行では、前のMSM版が完成済みでも最新の取得可能Runを選び直す。20分のWorkerは00分以降、40分のWorkerは25分以降に取得・計算された当日complete MSMが公開されていれば終了する。GSM、地形由来partial、未更新は補完候補とし、GitHubの実行中状態・同じ要求ID・再試行間隔・回数を確認して起動する。画像の再描画時刻で鮮度を更新しない。

Workerは最大4KiBの `status.json` とGitHubの実行metadataだけを読む。GRIBのダウンロード・Run探索・デコード・全PNG/PDF照合をWorkerへ持ち込まない。通常のHTTPアクセスからは起動できない。

GitHubの待ち時間、取得元障害、Cron配達遅延まで含めた締切保証ではない。タイムアウト18分はrunner開始後の上限であり、起動待ち時間を含まない。締切達成は候補の生成時刻から推測せず、公開先の読戻し完了時刻で確認する。

## 紙面と計算

採用済みの10固定区画、各区画の代表レグ全長に沿った距離加重u/v・気温平均、9高度を維持する。9500→2500 ft MSLの順、風向は10度half-up、北360。SR22 65% Best Power、nose fairingあり、A/C OFF、MSL=PA。FuelはUS gal/minで0.001単位。紙面に経路線・レグ参照表・中点のXは表示しない。

最新の利用可能MSMを優先し、旧MSM、GSMの順に試す。一枚のモデル・初期値を混在させない。source cutoffは実生成開始時刻と対象日の05:00/10:00の早い方。地形以下と取得失敗を区別する。

詳細: `production/WEATHER_PIPELINE.md`、`production/renderer_README.md`、`output/kyushu_weather_sector_requirements.md`。

## 完成版だけを公開する

Actionsは最初に現在公開中のstatus・2枠のpointer・版manifest・PNG/PDF/原値JSONを、サイズ・SHA-256・日付を検証して復元する。対象枠を更新して他方を保持し、全体で整合する静的ディレクトリを作ってからPagesへ一括アップロードする。

取得失敗、復元不一致、描画不備のときは配備しない。古い図の日付を書き換えない。全公開処理は同じconcurrency groupで直列化し、同じWorker要求IDの重複をActions側でも止める。アップロード直前にJST日付、候補identity、公開元deploymentの不変を再検査する。

候補の生成は公開成功ではない。静的pointerの `deadline_met` は未確定nullで、全ファイルの公開読戻しに成功した結果だけをworkflowの公開確認として扱う。GSMからMSMへの回復、新Runへの更新では、正常な地形除外だけを理由に古い完全値へ固定しない。

ページURLは固定で、`?time=0900` / `?time=1400` で直接選択できる。表示は同じoriginの静的ファイルだけを読み、閲覧時に気象計算しない。Blob・R2・DBは使用しない。

## ローカル実行

Python 3.12とNode 22を使用する。repository rootで実行する。

```sh
python -m pip install -r requirements.txt
python production/weather_pipeline.py --date YYYY-MM-DD --hours 9 \
  --output /tmp/kyushu/0900.json --cache /tmp/kyushu/cache --budget-seconds 230
python production/render_daily.py --input /tmp/kyushu/0900.json \
  --output-dir /tmp/kyushu/rendered --require-date YYYY-MM-DD
python -m production.build_pages build \
  --manifest /tmp/kyushu/rendered/manifest.json \
  --previous-origin https://ACTUAL_PROJECT.pages.dev --output /tmp/kyushu/site
```

最初の空のsiteを作る場合だけ `python -m production.build_pages build --initialize-empty --output /tmp/kyushu/site` を使う。空の枠はnullを持つ実statusとして公開する。404を未更新とみなしてWorkerを動かさない。

## 外部接続と初回公開

1. Cloudflareで独立Pages project `kyushu-nav-weather` を作成する。Direct Uploadを使う。既存 `navmate` projectへこの図だけをdeployしない。
2. GitHubのこのrepositoryにVariablesを設定する: `CLOUDFLARE_ACCOUNT_ID`、`CLOUDFLARE_PAGES_PROJECT=kyushu-nav-weather`。`WEATHER_AUTOMATION_ENABLED` は初回確認までfalse。Pages Edit権限の認証をSecret `CLOUDFLARE_PAGES_TOKEN` へ設定する。実値をsourceやchatへ書かない。
3. `weather-daily` を `operation=initialize, trigger=manual, publish=true` で一度だけ実行し、空siteを公開する。この初回は `WEATHER_ORIGIN` 未設定とする。生成されたPagesの実URLを確認し、以後 `WEATHER_ORIGIN` に設定する。
4. `operation=refresh, slot=both, target_date=当日JST, trigger=manual, publish=true` で実生成・公開・全読戻しを確認する。画像の日付・初期値・生成時刻を確認する。独自ドメインは実際の公開確認後に設定する。
5. `services/weather-trigger/README.md` に従い、新しいWorkerにこのrepositoryだけを対象とするGitHub Actions Read/Write認証をSecret `GITHUB_TOKEN` として接続する。Workerの `STATUS_URL` は確認済みoriginの `/status.json`、repositoryは `Yuto-24/kyushu-nav-weather`、branchはmain。配備時に認証値をログやファイルへ出さない。
6. Workerをobserveで確認後、`WEATHER_AUTOMATION_ENABLED=true` とWorkerのdispatch/Cronを有効化する。外部設定・予定時刻・実行結果を読み戻して、`production/deployment.json` の状態を更新する。

GitHubはrunner tokenを自動提供するため、Actions自身のmetadata照合用tokenを新規発行しない。WorkerからGitHubへアクセスする認証は別で、dispatchに必要な範囲だけを付与する。既存MSM用のSecretを読める・新repositoryへ転用できるとは仮定しない。

## 検証

```sh
python -m unittest discover -s production -p test_weather_pipeline.py -v
python -m unittest -v production.test_actions_job production.test_pages_publication
node --test services/weather-trigger/worker.test.mjs
node --check production/web/app.js
```

実MSM/GSM取得・採用済み180行の同値確認は実施済み。新経路ではJST対象日、Workerの起動契約、二重処理、40分での再確認、地形/欠損、公開の途中失敗、2枠保持、全読戻しを重点検証する。外部サービスの実行・公開・自然Cronはローカル検証とは別に確認する。

既知の失敗と設計判断は `production/infra_reuse_notes.md`、Worker固有の判断は `services/weather-trigger/README.md` に保持する。
