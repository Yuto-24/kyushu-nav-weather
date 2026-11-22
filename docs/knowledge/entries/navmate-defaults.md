# NavMateから継承する開発規則

- ID: NAVMATE-DEFAULTS
- Status: verified
- Scope: kyushu-nav-weather の開発規則・テンプレート
- Trigger / search terms: AGENTS、NavMate defaults、独立レビュー、UI文言、ドキュメント統制
- Last verified: 2026-11-22。参照元NavMate `6bb1cd5eeade053019cc35358f1417b7e878cbc4`
- Evidence: [参照元AGENTS](https://github.com/Yuto-24/NavMate/blob/6bb1cd5eeade053019cc35358f1417b7e878cbc4/AGENTS.md)、[このrepoのAGENTS](../../../AGENTS.md)

## 決定と適用範囲

ユーザーが「AGENTS.mdのデフォルト等をおおよそNavMateと同じにし、NavMate固有のものだけ省く」と指定した。
共通の範囲制約、調査と知見の再利用、選択モデルの維持、他モデルによる独立レビュー、
UI文言のGemini確認、文章規範、Issue/PRテンプレート、CodeRabbitの初回自動レビュー、
CI・外部レビューを無断で継続監視しない運用を継承する。

## 省いた固有事項と理由

- NavMateの地図・操作パネル配置と画面幅の受入：本サービスは既に採用した紙面と閲覧UIを持つ。
- Docker/Compose、Legacy、Firebase、NavMate共有volume・ポート：本サービスの実行・公開環境には存在しない。
- NavMateの全pytest、CI shard、型検査・release script：このrepoのPython/Node suiteへ置き換える。
- VERSION、release/1.x、個別package版数、VERSION/CHANGELOG限定レビュー例外：NavMateの版数リリース契約であり、毎日の気象図更新には適用しない。
- NavMate固有の過去の証跡・UIコピー例へのリンク：現在の正本と手順へ置き換える。

個人のCodex設定、既存NavMateのコード・本番設定、GitHubの保護ルールをコピー・緩和する指示ではない。
規則を取り込んだことだけで、CodeRabbitの導入、認証の共有、外部設定の完了を意味しない。

## 次回の確認

NavMateの後続変更を自動で追従させず、要求と現在の構成に対する適用性を確認してから更新する。
詳細な運用・検証条件は各正本を更新し、AGENTSを作業日誌にしない。
