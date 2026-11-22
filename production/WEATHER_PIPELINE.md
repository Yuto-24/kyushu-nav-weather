# 日次気象データの生成

採用済みの固定A〜J区画と代表10レグを維持し、当日の実MSM、取得不能時はGSMから紙面JSONを作る。ChatGPTの定期タスク、Library、過去1,332ケース、外部NavMate checkoutには実行時に依存しない。

## 実行インタフェース

Python 3.12のLinux環境、リポジトリrootでインストールする。

```sh
python -m pip install -r production/requirements-weather.txt
python production/weather_pipeline.py --date 2026-11-22 --hours 9 \
  --output /tmp/kyushu-job/0900.json --cache /tmp/kyushu-job/cache \
  --budget-seconds 230
```

`--hours 14` で午後版だけを生成する。2版まとめての検証には `--hours 9 14` を使えるが、定時運用では各枠を独立実行する。`--date` は日本時間の対象日。省略時は実行時のJST日付。

```python
from production.weather_pipeline import generate_forecast, WeatherUnavailable

forecast = generate_forecast(
    day="2026-11-22", hour=9, as_of=None,
    cache="/tmp/request-specific-directory/cache", budget_seconds=230,
)
payload = {
    "schema": "pseudo-kyushu-paper-forecast-1",
    "primary_sector_count": 10,
    "forecasts": [forecast],
}
```

`generate_forecast` は `{metadata, groups}` を返す。`load_assets()` の返値を `assets=` へ渡すと、同じプロセスで固定資産を再利用できる。定時イベントの `scheduledAt` はJST対象日の決定に使用し、通常の `as_of` は省略して実行開始時点を使う。

全候補の取得が失敗した場合、関数は `WeatherUnavailable` を送出し、`error.attempts` に試行記録を持つ。CLIは終了コード2と出力パスに対応する `.failure.json` を返し、成功JSONを新しい日付へ書き換えない。行単位の `missing`、地形除外による `terrain`、一部除外を伴う `valid + partial_flag` は別の状態。

## 確定した計算と出典

- `weather_assets/fixed_leg_weights.npz` は採用済み31レグの603地点距離重み・地点座標・地形だけを保持する。過去の予報field/平均値は入れない。
- A=L16、B=L25、C=L31、D=L21、E=L09、F=L05、G=L04、H=L03、I=L27、J=L07。各表は一つの代表レグ全長の有効距離でu/v/OATを平均する。区画の面積平均や複数レグの合成平均は行わない。
- 9高度は9500→2500 ft MSLの降順。風は成分平均後に風向を10°へhalf-up、北360。Fuelは固定NavMateの最終 `row.gph` を60で割り、0.001 US gal/minへdecimal half-up。
- NavMate commit `62a7896036ca365b734044fd8bde1739abb9173e` の計算・性能表を変更せず同梱。65% Best Power、nose fairingあり、A/C OFF、MSLの数値=PAを維持する。実行時に全固定資産のSHA-256を照合する。
- 元気象はRISH配布のJMA原GRIB2。ダウンロードは `analysis/fetch_msm.py` と同じurllib→pygrib/jma-gpv-weather方式。Run、valid、必須9気圧面×4変数、重複record、格子、単調な気圧面高度、欠損、GRIB先頭末尾、Content-Lengthを検査する。原本URL・Last-Modified・SHA-256・抽出NPZ SHAを保持する。
- `jma-gpv-weather==0.6.0` は既存NavMateのvendor wheelをそのまま同梱した。wheel SHA-256は `d008bb9a7125f6b69012562efffd1edd5d517345b2672359d3b55799fcc63787`。

## 時刻とMSM/GSMの選択

09:00版の取得cutoffは対象日05:00 JST、14:00版は10:00 JST。実際には `min(実行開始now, 明示as_of, 締切)` を使い、未来の初期値や締切後の公開を使わない。Run時刻が過去であるだけでは可用とせず、HTTP HEAD/GETの実在とLast-Modifiedがcutoff以下であることを確認する。

MSMを優先し、モデル内で新しい初期値から探す。取得不能・必要record不足・元値非finiteの候補は捨て、対象時刻を予報範囲に含む旧MSM、次にGSMへ進む。MSMは初期値遡及36時間・使用予報範囲39時間まで、GSMは初期値遡及48時間・使用予報範囲72時間までを探索する。同一forecastへ別Runや別モデルを混ぜない。候補を拒否した理由もmetadataに残す。

MSMのFH15/FH18などファイル境界にまたがる補間にも対応する。GSM名の `FD0000-0100` は0〜24時間であり、forecast hourの整数表現ではない。モデルの圧力面予報の3時間間隔から、targetを挟む実valid時刻を選ぶ。各地点で鉛直補間した後、その2時刻の差から時間重みを計算する。14時の重みやFH6/9を固定しない。

地形以下と、鉛直下側の気圧面が地形より下の状態を別に記録する。補間の両時刻が有効であることが必要。地形除外を、別モデルの値で埋めない。地形ガードは採用済みのMSM 2017モデル地形で、GSMでも同じ固定地点のガードを使用する。GSM固有のモデル地形や実地形クリアランスを保証するものではない。

今回取得したGSMの上層格子はMSMの採用済み地点に一致し、そのまま読む。GSM格子が一致しない場合のみ、固定地点の気圧面profileへ双線形補間してから同じ鉛直・時間・レグ距離平均を適用する。MSMの格子変更は黙って補間せず失敗させる。

## 実行時間と失敗時の扱い

既定の取得budgetは230秒。候補全体の累積elapsedを検査し、MSMには65%を割り当て、MSM側の障害で時間を使い切らないようGSMに35%を残す。HEADのsocket timeoutは最大12秒、GETは最大20秒。`read1()` を使い、一度のsocket読取りごとに累積elapsedへ制御が戻る。GRIB decode後にもbudgetを検査する。単一socket readやnative decode中の割込みは行わないため、230秒はハードなプロセス停止時刻ではない。300秒の外部実行枠では、呼出し側が約260秒のプロセス上限を設定し、render/uploadの余白を持たせる。

成功JSON・抽出NPZ・manifestは一時ファイルを検証後renameする。cacheはリクエストごとの `/tmp` ディレクトリを使う。公開先の同時更新制御、90行の状態判定、画像とmetadataの一括切替は呼出し側の責務。旧画像を新日付で出すことはしない。

RISHはMSMとGSMの共通配布元なので、配布元全体の停止はモデル切替だけでは回避できない。公開時刻は固定の到着保証として扱わない。今回、11/21 15UTCと18UTCのMSMが404の時点で12UTCを選べたことは、毎日の到着率や締切SLAを実測した証明ではない。

## 検証結果（2026-11-22 JST）

| 項目 | 結果 |
| --- | --- |
| 採用済み11/20例との比較 | 180行の気象・CAS/TAS/Fuel・表示・印一致。数値最大絶対差4.88×10^-13 |
| 当日MSM 09/14版 | 11/21 12UTC初期値、180行valid、一部除外0 |
| 当日MSM転送・計算 | 2予報計67.79秒、peak RSS157,024 KiB（約153.3 MiB） |
| MSMダウンロード量 | 09版50,522,465bytes、14版101,044,930bytes（2ファイル） |
| 当日GSM経路 | `--models GSM`で明示検証。11/21 12UTC→11/22 09時、90行valid |
| GSM転送・計算 | 22.12秒、peak RSS150,664 KiB（約147.1 MiB）、46,558,107bytes |
| 関数APIの実データ検査 | GSM14時を直接 `generate_forecast` で生成。90行valid、24.19秒、peak RSS150,632 KiB |
| 重点自動検査 | 未来Run/公開拒否、JSTtarget、14時ファイル境界、GSM FD、MSM失敗時GSMへ遷移、両endpoint、地形/欠損、原子的JSONの9件Pass |

実測はLinux/Python3.12、numpy2.5.5、pydantic2.14.3、pygrib2.1.8、jma-gpv-weather0.6.0。重点検査と180行同値照合は別のnumpy2.3.5/pydantic2.12.5環境でもPass。`requirements-weather.txt` は後者の固定版を指定する。pygribのLinux wheelには必要nativeライブラリが同梱されており、この環境では追加aptなしでdecodeできた。別のOS/ABIでsdistビルドへ落ちる場合にはecCodes等のビルド依存が別途必要になる。

当日14時の原本はMSM/GSMとも11/21 12UTC RunのFH15/FH18。validは11/22 03UTC/06UTC（12JST/15JST）、14時への時間重みは前1/3・後2/3である。コードはこれを実validから計算し、metadataにも保持する。

実測証拠は `verification/current_run_resources.json`、`gsm_run_resources.json`、`historical_parity.json`、当日JSONおよび抽出manifestに保持する。GSM試験は実際にMSM全配布が停止した観測ではなく、GSM取得経路そのものの実データ検証である。

```sh
python -m unittest discover -s production -p 'test_weather_pipeline.py' -v
```
