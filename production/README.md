# 九州 上空風・気温 — Cloudflare Pages と日次更新

固定A〜Jの疑似セクターによる九州共通A4資料を、毎日09:00・14:00の2版として更新する。GitHub ActionsがMSM/GSM取得・計算・描画を担当し、Cloudflare Pagesから生成済みHTML・PNG・PDF・JSONを配信する。Cloudflare Workerは更新状況を確認してActionsを補完起動する。ChatGPTや利用者PCの常時起動に依存しない。

## 運用状態

公開先は **https://kyushu-wx.yuto24.com**。2026-11-22に09:00・14:00版をMSMの実データで生成し、独自ドメインから画像・PDF・JSONを読み戻して確認した。2つの専用認証を接続し、Actionsの自動更新とWorkerのdispatch/Cronを有効にした。旧 `weather.yuto24.com` の接続とCNAMEは削除済み。

設定と実行の確認結果は `production/deployment.json` に記録する。初回は午後の手動公開であり、締切内の定時更新実績ではない。Workerの実環境でのstatus・GitHub読み取りは成功したが、CPU時間、Workerからのdispatch、自然Cronの実行は未確認。

GitHub repository: https://github.com/Yuto-24/kyushu-nav-weather

以前検討したVercel案は採用しない。既存NavMateとは独立したrepository、Pages project、Workerで運用し、既存MSMの起動・認証・配布物を上書きしない。source repositoryは非公開、完成した気象図の閲覧ページは公開する。

## 実行時刻

| 予報 | Actions定時実行（JST） | Worker補完確認（JST） | 出力締切 |
| --- | --- | --- | --- |
| 09:00版 | 04:03 / 04:33 | 04:20 / 04:40 | 05:00 |
| 14:00版 | 09:03 / 09:33 | 09:20 / 09:40 | 10:00 |

ActionsのcronはUTCの `3,33 19 * * *` と `3,33 0 * * *`。Workerの設定はUTCの `20,40 0,19 * * *`。

定時実行では、前のMSM版が完成済みでも最新の取得可能Runを選び直す。20分のWorkerは00分以降、40分のWorkerは25分以降に取得・計算された当日complete MSMが公開されていれば終了する。GSM、地形由来partial、未更新は補完候補とし、GitHubの実行中・待機中状態と同じ要求IDを確認して起動する。別IDの失敗履歴、日次起動回数、15分のクールダウンでは止めない。画像の再描画時刻で鮮度を更新しない。

予定から遅れても同じJST日付内なら対象版を処理する。Actions定時実行はCron文字列で版を選び、GitHubのRun作成日をJST対象日とする。Run作成日と実行日が違う要求や、当日の予定より早い作成は拒否する。GitHubのschedule payloadには本来の配達予定日がないため、作成自体が翌日に遅れたイベントの元の日付は復元できない。Worker要求は予定日・版・要求IDを明示し、90分を超えて待っても当日中なら有効とする。

05:00/10:00のsource cutoffは変更しない。遅延復旧はその時刻までに入手可能だった気象データを対象に行う。補完の詳細と履歴取得不能時の扱いは [Worker運用手順](../services/weather-trigger/README.md) を参照する。

Workerは最大4KiBの `status.json` とGitHubの実行metadataだけを読む。GRIBのダウンロード・Run探索・デコード・全PNG/PDF照合をWorkerへ持ち込まない。通常のHTTPアクセスからは起動できない。

GitHubの待ち時間、取得元障害、Cron配達遅延まで含めた締切保証ではない。タイムアウト18分はrunner開始後の上限であり、起動待ち時間を含まない。締切達成は候補の生成時刻から推測せず、公開先の読戻し完了時刻で確認する。

## 紙面と計算

採用済みの10固定区画、各区画の代表レグ全長に沿った距離加重u/v・気温平均、9高度を維持する。9500→2500 ft MSLの順、風向は10度half-up、北360。SR22 65% Best Power、nose fairingあり、A/C OFF、MSL=PA。FuelはUS gal/minで0.001単位。紙面に経路線・レグ参照表・中点のXは表示しない。

最新の利用可能MSMを優先し、旧MSM、GSMの順に試す。一枚のモデル・初期値を混在させない。source cutoffは実生成開始時刻と対象日の05:00/10:00の早い方。地形以下と取得失敗を区別する。

詳細: `production/WEATHER_PIPELINE.md`、`production/renderer_README.md`、`output/kyushu_weather_sector_requirements.md`。

## 完成版だけを公開する

Actionsは最初に現在公開中のstatus・2枠のpointer・版manifest・PNG/PDF/原値JSONを、サイズ・SHA-256・日付を検証して復元する。対象枠を更新して他方を保持し、全体で整合する静的ディレクトリを作ってからPagesへ一括アップロードする。

取得失敗、復元不一致、描画不備のときは配備しない。古い図の日付を書き換えない。全公開処理は同じconcurrency groupで直列化し、同じWorker要求IDの重複とWorkerのrerunをActions側でも止める。履歴は30件ずつ最大10ページを照合し、欠落・件数変化・重複・上限超過があれば公開へ進まない。アップロード直前にJST日付、候補identity、公開元deploymentの不変を再検査する。

同日の同じモデルでは、古いRun、古い気象生成時刻、古い画像生成時刻への差替えを拒否する。再描画だけで古い気象データを新しく見せない。

候補の生成は公開成功ではない。静的pointerの `deadline_met` は未確定nullで、下記の対象ファイルすべての公開読戻しに成功した結果だけをworkflowの公開確認として扱う。GSMからMSMへの回復、新Runへの更新では、正常な地形除外だけを理由に古い完全値へ固定しない。

公開確認では気象ファイルに加え、トップページ・JavaScript・CSS・設定JSON・初期表示用manifestの5ファイルを実際に取得し、候補のhashと照合する。トップページは `/` を読む。HTMLは観測・固定したCloudflare Analyticsの単一挿入を末尾に1回だけ許容し、その除去後の本文全体を候補と照合する。任意のscript除去やHTML正規化はしない。`404.html` と配備設定 `_headers` は候補のローカル照合対象だが、公開先の404応答・header設定の検証済みとは扱わない。

アップロード後の再試行は5秒間隔・最大90秒の開始予算とし、正常な別status、旧pointer／閲覧用ファイルとの混在、読戻し最後のstatus変更、一時的なHTTP 404/408/429/5xx・通信切断・不完全受信に限定する。毎回statusから完全な照合をやり直し、失敗した試行のファイルは次へ持ち越さない。認証・TLS・redirect、status／pointerの不正JSON、不正パス、サイズ上限超過、immutableな気象アセットの破損は即失敗する。待機ログはstderrへ出し、公開確認JSONには混ぜない。

正常に進む全ファイル転送には、短い試行ごとの打切りを設けない。読戻し全体は300秒、全試行累計96リクエスト・128 MiBの取得上限予約で制限し、失敗した要求も予算に含める。HTTPの20秒socket timeoutに加えて受信chunkごとに期限を確認し、Actionsの確認stepも5分で停止する。90秒以内に開始した最後の完全照合は、全体300秒の範囲で継続できる。期限後の成功結果は採用しない。

CLIで同じ待機を使う場合は `verify --origin <固定URL> --expected-dir <配備したディレクトリ> --wait-seconds 90` を指定する。既定値0は一度だけの照合で、復元・アップロード直前の競合検査には待機を加えない。待機後も不一致なら、期待・観測deployment IDと、独自ドメイン／Pages標準ドメイン／その配備固有URLのstatusを比較する。アップロードが成功している場合は、既に新しい内容が公開済みでもActionsが失敗表示になるため、まず公開先を確認する。

ページURLは固定で、`?time=0900` / `?time=1400` で直接選択できる。表示は同じoriginの静的ファイルだけを読み、閲覧時に気象計算しない。Blob・R2・DBは使用しない。

## PR #5の反映と障害復旧

この再設計はPR内の実装であり、本番Workerへの配置・本番設定変更を含まない。反映には次の操作が必要となる。

1. Hosted CI、独立レビュー、CodeRabbit、未解決スレッド等の必須条件を確認し、承認後にPR #5をmainへマージする
2. main上のActionsを使用し、専用の `kyushu-weather-trigger` を既存設定のまま承認済み手順で再デプロイする。マージだけではWorkerの実行版は変わらない
3. 自然Cron、Workerのdispatch受領、Actionsの生成、公開ファイル全体の読戻しを個別に確認する。provider CPU時間は実環境で確認する

公開が更新されない場合は、まずstatusの日付・モデル・気象生成時刻とActionsの実行中状態を読む。履歴GETの一時障害はWorker内で再確認する。履歴が不明のまま送信したり、受付不明のPOSTを再送したりしない。

既存の4回のCronで回復できなかった場合は、公開先を確認したうえで `weather-daily` の `operation=refresh, trigger=manual, target_date=当日JST, slot=対象版, publish=true` を実行する。Workerの同じIDを再利用しない。main制限、source cutoff、完成版検証、公開直列化は手動復旧にも適用する。前日分を当日として公開し直さない。

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
3. `weather-daily` を `operation=initialize, trigger=manual, publish=true` で一度だけ実行し、空siteを公開する。この初回は `WEATHER_ORIGIN` 未設定とする。接続済みの独自ドメイン `https://kyushu-wx.yuto24.com` でHTTPSとstatusの読戻しを確認し、以後 `WEATHER_ORIGIN` に設定する。
4. `operation=refresh, slot=both, target_date=当日JST, trigger=manual, publish=true` で実生成・公開・全読戻しを確認する。画像の日付・初期値・生成時刻を確認する。利用者向け固定URLとWorkerの参照先には独自ドメインを使う。
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
