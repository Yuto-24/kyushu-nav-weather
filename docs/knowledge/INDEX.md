# 再利用する知見

利用方法は [README.md](README.md)。関係する項目だけを読み、現在のコード・依存関係との整合を確認する。

| 項目 | 状態・参照条件 |
| --- | --- |
| [NavMateから継承する開発規則](entries/navmate-defaults.md) | verified：開発規則、テンプレート、レビュー、文書管理を変更する時 |
| [紙面の風を使う将来連携](../navmate-wind-input.md) | 2026-11-22ユーザー決定。丸め値・同じ版の使用を検討する時 |
| [定時公開に関する既存の設計判断](../../production/infra_reuse_notes.md) | 2026-11-24ローカル検証済み：初期時刻の更新目標、06:15の両版判定、当日内の遅延復旧、source cutoff、両版保持、公開読戻しを変更する時 |
| [MSM/GSMの取得と計算](../../production/WEATHER_PIPELINE.md) | Run選択、締切時点の入手可能性、補間、地形除外を変更する時 |
| [Workerの補完起動](../../services/weather-trigger/README.md) | Cron、重複、遅延イベント、認証、運用状態を変更する時 |

既存の正本をリンクし、同じ知見のコピーを別エントリに増やさない。
