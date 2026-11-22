# 日次紙面 renderer

採用済みの固定 A-J 疑似セクター、代表レグ、左右 5 表、9 高度、Fuel 列、引出線を使い、入力 JSON の対象日・予報時刻に対応する A4 横 1 ページを生成する。気象取得、定時起動、固定 URL への公開は別工程である。ChatGPT や Library を実行依存に含めない。

## 実行

Python 3.12 で、リポジトリのルートから実行する。

```sh
python -m pip install -r production/requirements-renderer.txt
python production/render_daily.py \
  --input production/output/2026-11-22/forecast_0900.json \
  --output-dir production/output/2026-11-22/0900 \
  --require-date 2026-11-22
```

`--input` は `pseudo-kyushu-paper-forecast-1` の JSON。`forecasts` は 1 件でも、同じ日の 09:00・14:00 の 2 件でもよい。入力に存在する予報だけを生成する。1 件の実行で他方の予報を生成・削除しない。

`--require-date` は公開対象日と入力の日付を一致させる検査である。違う日のデータを当日の日付へ変更しない。定時ジョブでは省略せず、日本時間で求めた対象日を渡す。

生成時刻は実際の実行時刻が既定値。再現確認だけに使う場合は `--generated-at 2026-11-22T04:30:00+09:00` のように UTC オフセットを含む値を指定できる。公開用の実行では既定値を使う。`--qa` は出力先の `qa/` に 150 dpi の確認画像を追加する。

## 出力と公開側の契約

09:00 の 1 件を実行した場合は次を出力する。以下の日付は例であり、すべて入力から決まる。

| ファイル | 内容 |
| --- | --- |
| `kyushu_pseudo_0900_20261122.pdf` | A4 横 1 ページ |
| `kyushu_pseudo_0900_20261122.png` | 300 dpi、3508 × 2481 px、グレースケール |
| `kyushu_pseudo_forecasts_20261122_0900.json` | 受け取った気象 JSON の完全なコピー |
| `layout_checks_20261122_0900.json` | 全表示値、引出線座標、レイアウト検証 |
| `manifest_20261122_0900.json` | この日時の配布 manifest |
| `manifest.json` | この実行が完了したことを示す同内容の manifest |

2 件を同時生成した場合は、各 1 ページ PDF/PNG に加え `kyushu_pseudo_weather_20261122.pdf` の 2 ページ版も作る。1 件しかないのに 2 枚そろった PDF を作ることはない。

配布 manifest の schema は `kyushu-daily-publication-1`。公開側が使う主要フィールドは以下。

| フィールド | 意味 |
| --- | --- |
| `target_date` / `timezone` | 入力の対象日 / `Asia/Tokyo` |
| `generated_time_jst` | 画像生成時刻 |
| `forecasts[].slot` | `0900` または `1400` |
| `forecasts[].valid_date` / `valid_at_jst` | 各画像の実際の予報対象日時 |
| `forecasts[].model` | 実際の `MSM` / `GSM`、全候補不可なら `UNAVAILABLE` |
| `forecasts[].initial_time_jst` / `initial_time_utc` | 実際の初期時刻。取得不可の場合は null |
| `forecasts[].weather_generated_time_jst` | 気象計算工程の生成時刻 |
| `forecasts[].image_generated_time_jst` | 描画工程の生成時刻 |
| `forecasts[].historical` | 生成時点の日本時間の日付より対象日が過去か |
| `forecasts[].target_date_matches_generation_date` | 対象日が描画当日か |
| `forecasts[].files.png` / `files.pdf` | ファイル名、media type、byte 数、SHA-256 |
| `forecasts[].coverage` | 有効・欠損・地形・部分除外・数値完全行数など |
| `forecasts[].source_metadata` | 入力に含まれる取得元、実 Run、フォールバック等の来歴を保持 |
| `forecast_json` / `layout_report` | 気象原値と描画検証の配布情報・SHA-256 |
| `combined_pdf` | 2 枚あるときだけ結合 PDF の配布情報、それ以外は null |

`files.*.path` は出力ディレクトリに対する相対ファイル名。すべての PNG/PDF を描画・検証してから成果物を移し、最後に `manifest.json` を置き換える。renderer 自体は `0900.png` / `1400.png` のような日付なしの公開 URL を作らない。公開側は対象日・対象 slot・ファイル hash・coverage を確認してから該当 slot の固定 URL を更新する。全欠損の診断画像を生成できることと、公開側がそれを成功版へ昇格させることは分ける。

## 数字と表示の扱い

入力の気象値・性能値を renderer で再計算しない。風向の 10 度 half-up 表示、北を 360 とする表記、既存の整数風速・OAT・CAS・TAS、Fuel の 3 桁文字列をそのまま採用済み表へ描画する。高度は 9500 → 2500 ft MSL。数字は埋込 Liberation Sans regular/bold、地図・表の寸法と配置は採用版のまま。

紙面上部の主表示は対象日と `09:00 / 14:00 JST`。MSM 固定だった取得元表示を実モデルに切り替え、初期時刻と画像生成時刻も実値を表示する。対象日が描画当日なら `過去予報` を出さず、過去日なら出す。取得不可の行は `?`、地形以下は `T`。取得元が GSM になった事実は上部でわかる。既存の `*` / `!` / `†` は従来の意味を保ち、GSM フォールバック記号へ転用しない。

フォールバックは気象工程で予報単位に選ぶ契約であり、同じ 1 枚で複数モデル・初期時刻を暗黙に混ぜない。混在モデルの JSON はここでは受け付けない。空欄がある場合も、それを 0 や前日の数値で埋めない。性能値が利用できない有効気象行では当該数値欄を `?` とし、`complete_numeric_rows` に反映する。

## 軽量 runtime bundle

```sh
python production/build_runtime_bundle.py \
  --output production/output/kyushu_daily_runtime.zip
```

固定の map、geometry、代表選定、font とライセンス、採用 renderer、日次 Python モジュール、requirements、運用 README、`production/weather_assets/` を集める。過去 74 日の解析、実行 cache、過去の PNG/PDF、認証情報は含めない。`runtime_manifest.json` に全ファイル SHA-256 とサイズを収める。

配備設定が必要なら `--include vercel.json --include api` のように明示する。指定した追加ディレクトリは再帰的に含むため、認証情報・cache・output を含む広いディレクトリは指定しない。依存ライブラリは外部実行環境で requirements から導入する。気象取得の既存実装に合わせて固定した `jma_gpv_weather-0.6.0` の wheel だけは `production/vendor/` として同梱する。

## 今回確認できたこと

2026-11-20 の既存 2 予報を別出力先へ再描画し、全 180 行の表示値と全引出線座標が採用版に一致した。1 予報入力・2 予報入力の両方が完了し、PDF は各 1 ページ A4 横、PNG は 300 dpi グレースケール。Poppler で 09:00 の再描画を確認した。

その後、実取得した 2026-11-22 の 09:00・14:00 を `production/output/20261122_actual/` へ描画した。両方 90/90 行の全数値が揃い、初期は 11/21 21:00 JST の実 MSM、当日ラベルに `過去予報` はない。上部の日付・時刻・初期を PDF 抽出で、09:00 を Poppler、14:00 を MuPDF の画像で確認した。実機での印刷・コピーは未実施。

この環境の Python 3.12 では、1 予報の入力検証・PDF/PNG 描画・manifest 生成に 0.959 秒、子プロセスの peak RSS は 89,008 KB だった。気象ダウンロード時間やサーバレス環境での起動時間は含まない。1 回のローカル計測であり、本番性能の保証値ではない。

今回の非自明な判断は、取得元情報はヘッダーと manifest に持たせ、採用済みの風の印を再定義しないこと、および日付つき成果物の生成と固定 URL の更新を分けること。過去日を当日扱いする失敗を防ぎつつ、09:00 だけを更新した際にも 14:00 の前回成功版を公開側で保持できる。
