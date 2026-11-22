# 変更に応じた検証

正本は `.github/workflows/verify.yml` と各テストである。Python 3.12とNode 22を使用する。
依存関係は `requirements.txt` と `services/weather-trigger/package-lock.json` に固定する。

| 変更対象 | 検証 |
| --- | --- |
| 気象取得・鉛直/時間補間・集計・性能値 | `python -m unittest discover -s production -p test_weather_pipeline.py -v` |
| Actionsの日付・起動・公開処理 | `python -m unittest -v production.test_actions_job production.test_pages_publication` |
| 補完Worker | `node --test services/weather-trigger/worker.test.mjs`、`node --check services/weather-trigger/worker.mjs` |
| 閲覧ページのJavaScript | `node --check production/web/app.js` と影響する表示・操作の確認 |
| 文書・開発規則・テンプレート | 相対リンク、参照先、現行実装との整合確認 |

UI文言を変更する場合は、機能検証とは別に [UI文言確認](ui_copy_review.md) を実施する。
文言確認記録を変更した場合、`python scripts/ui_copy_review.py check <記録ディレクトリ>` を実行する。
記録が存在しないことを、製品全体の文言が確認済みである証拠にはしない。

実MSM/GSMの再取得は、取得処理の変更、取得元の問題、初回公開など確認すべき具体的な理由がある場合に行う。
コードの変更なしに過去の成功試験を繰り返さない。採用日時、対象日時、初期値と実生成時刻を混同しない。

公開は、Pages上のstatus、両予報のpointer、manifest、JSON、画像、PDFを読み戻して確認する。
Actionsが成功したこと、Workerのdispatchが受理されたこと、公開締切を満たしたことは別に判定する。
定時実行は設定の読戻しと実際のCron実行を区別する。
