# 実装アーキテクチャ — dev2

本書は実コードの構成。独立レビュー・製品受入の証明ではない。

## 同一ユーザーの構成

```text
ユーザー ↔ Claude Codeの通常会話 / daikibo_dev Skill
                   │ CLI + 会話Hook（ローカル・認証なし）
                   ▼
        一つのPython管理プロセス
        ├─ SQLite: 原文参照、仕様revision、工程、裁定、Job、証拠の台帳
        ├─ blob: 原文、コード、出力、レビュー入力
        ├─ Git: baselineビュー、納品候補
        └─ subprocess: 実装者、Reviewer、test（すべて同じUID/HOME）
```

別コンテナ・別OSユーザー・providerプロキシ・暗号学的本人認証はない。
起動したプロセス群を個別に管理するが、process groupは停止範囲の識別であり隔離ではない。

## モジュール所有権

| Domain | モジュール | 責務 |
|---|---|---|
| D01 | interaction, native, hooks, cli | 原文、会話session/turn、提案提示、回答・未確認通知 |
| D02 | knowledge, documents, packets, db | 不変ID/revision、原文と分類、欠落のないpacket、関連グラフ |
| D03 | planning, contracts, review_scopes, supervisor | シナリオ/責務/契約/計画、変更、矛盾、差戻し、分割レビュー |
| D04 | workflow, jobs | 永続状態、依存、実行権・世代、取消、時間・進展、再開 |
| D05 | governance, security | Gate、logical role、規則、例外、証拠の照合・監査記録 |
| D06 | runtime, agents, providers | 通常プロセス起動、CLI接続、既存ログイン、観測と記録 |
| D07 | indexing | 多言語/差分索引、検索、未知、Contextの対象・予算・鮮度 |
| D08 | delivery, build_outputs, testreports, gitops, integrations, operations | 統合、build出力、実検証、納品、復旧 |

`control.py` が合成し、`rpc.py` は長さ付きJSONをUnix socketへ運ぶ。SQLは単一writerで直列化する。
`Security` という旧クラス名は互換用に残るが、新規運用では認証・秘密鍵発行をしない。
`Actor.role` は通常操作の責務誤適用を防ぐラベルであり、同一ユーザーに対するアクセス制御ではない。

## 原文と判断

sourcesは原文blobを保持する。native_turnsはsession/turn IDで重複受信を照合する。
同じ文章でも別turnなら別入力として残す。Hookから受けた発言とSkill経由発言はoriginで区別するが、どちらも認証しない。

裁定は、特定digestの提示 → その後の原文 → 選択肢と原文中の引用 → consistency review → 適用。
表記が一致しても意味の解釈が正しい保証ではない。曖昧な回答は会話側で確認する。
ユーザー入力そのものと、裁定の受領、正式適用は別記録。通知の表示とackも別状態。

## 分割・再検証

source.partitionはUTF-8 byte予算を守りつつ、元のcharacter spanを連続に分割する。総和で原文を再構成できる。
分類完了は登録範囲の検査であり、意味の抽出完了ではない。

program.partition_reviewは単一Artifactが大きい場合もcanonical JSONの連続fragmentへ分ける。
各fragmentに全体digest・文字範囲・index/count、scopeに入力revisionと工程を固定する。
fragment manifest全体が必要。Artifact IDが一度見えただけでは全体確認にならない。
工程全体の判断はレビュー済み各scopeの結果を踏まえる別のphaseレビューで行う。

## 実行・証拠・完了

同じUIDで別プロセス・別作業コピーを作る。readonlyは物理権限ではなく、実行前後の内容不変検査。
通常のHOME/PATH/認証設定を継承し、管理runのHook再帰だけ抑える。
時刻、命令、入力digest、役割、run、実exit、test件数、出力blobをcollectorで記録する。
新規の認証タグ形式は `sha256-unkeyed-v1`。これは破損確認であり署名ではない。

governedは実CLI資格と観測された判断を要求し、validationはfixtureを許すがdeploy-readyを出さない。
両モードとも単一ユーザーで動く。物理隔離は関係しない。

Jobの起動直前にSQLiteの現在状態を再読し、古いキュー情報からcancelled/terminal Jobを実行しない。
停止は管理対象のprocess groupに限定する。クラッシュした実行はunknownにして、自動成功や盲目的な副作用再実行にしない。

完了報告native.completionではタスクGateのrecheck、または納品の非変更再審査を行う。
失われた証拠は次の定期巡回を待たず、その場の報告にblockerとして現れる。
Hookが全ての自由文を解釈して虚偽を防ぐわけではない。システムが発行したcompletion値が正式状態である。

## 移行・配布

DB schema v4はnative session/turnの追加migration。旧データ・旧HMAC証拠は残して読める。
新規owner tokenやkeyfileは不要。旧版の余ったtokenは新たな権限を与えない。
Skill/Hookを新venvへ更新すると古い登録commandを除去し、ユーザーの別設定を保存する。

依存部品はdev1と同一。Python標準SQLite/JSON/TOML/HTTP/subprocess、外部runtimeはTree-sitterと文法のみ。
実CLIとアカウントは同梱しない。実CLI適合、実案件、独立レビュー、継続運転は未実施。

## dev7: workstream boundary

`workstreams.py` owns delegation and scoped work closure; canonical requirements,
Tasks and the root breakdown stay in their existing modules. The component does
not start a second scheduler. It reads version-bound selected leaf material and
checks actual Task evidence. Root delivery certifies the integrated release.
`workstream_history.py` validates portable cross-record relations. Schema9 is an
additive migration. `workstream.*` routes are available through native actions and
Supervisor commands; ordinary Task/Job gates remain mandatory. See ADR-009.

## dev9: scope return review tree

`scope_returns.py` (D03) fixes current return material, stores complete leaf
fragments and immutable finite-fan-in impact synthesis nodes. `workstreams.py`
still owns assignment state; the return module only calls its history writer
inside one SQLite transaction. D04 Jobs resolves packets and runs the existing
D06 Reviewer runtime. D05 checks latest real receipts, exact coverage and unchanged
inputs; completion is never a prose verdict or a subset of the tree.
`scope_return_history.py` and archive v5 preserve source/child/root relations
without claiming that imported references constitute new evidence. Direct old
small-scope withdrawals now bind current Task and artifact inputs too.

## dev10: root採用前のsubplan（D03、ADR-012）

同一正本の草案/Taskを参照するbottom-up design proposal。subplans→子packetのdesign/trace実run→親packetの実run→通常canonical採用→全projectへのcompose→breakdown提案→rootreview/activate。新しいTask正本・Agent scheduler・独立納品はない。

Breakdownsのvalidatorは標準では全scope/accepted入力のまま。subplanだけ明示的にallow_drafts/allow_externalを指定し、最終composeは標準検査を再実行する。rootはorigin_subplanとimmutablecompositionを保持し、採用後の監査も下位の現在review/trace入力まで辿る。SQLite schema11、chunked archivev6。検証範囲・容量はSUBPLANS.md。
