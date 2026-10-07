---
name: daikibo_dev
description: 大規模開発の仕様相談から、分割設計、途中変更、実装・実検証・レビュー・納品までをdaikibo_dev管理システムで進める。単一ユーザーの通常コンテナ内で動作する。
---
# daikibo_dev

これは実行基盤への入口です。工程や完了の正本はCLI/RPCの返す状態であり、会話の自己申告ではありません。

## Managed worker role

開始時に `DAIKIBO_MANAGED_RUN=1` が設定されている場合は、通常会話の接続手順より先にこの分岐を適用します。`DAIKIBO_RUN_ROLE`、`DAIKIBO_RUN_ID`、`DAIKIBO_TASK_ID`、`DAIKIBO_JOB_ID` は今回の実行文脈です。これは認証資格ではなく、呼出し側が渡した担当識別です。

- `implementer` は既にclaimされたTaskの実workerです。割り当てられたworkspaceで許可された編集と局所検証を行い、成果・検証・未解決事項を必要なJSON objectで返して終了します。自分のrun/jobまたは未採取のcandidateを待たず、candidateを採用せず、Task完了、review、delivery、完了Gateを管理せず、候補採取と後続判定は呼出し側へ返します。
- reviewer role は渡された正本資料と許可されたread-only accessを確認し、要求されたreview JSONを返して終了します。自分のrun/jobをwait・再投入せず、状態変更や完了運営を行いません。不足があれば根拠付きでblockedを返します。
- `supervisor` は既存のmessage/actions/questions JSONを返して終了し、actionsの適用とreceiptの記録は外側へ返します。自分のrun/jobをwaitして次の管理操作を始めません。

managed roleでは必要な追加資料を既存の許可されたread APIで読むことはできますが、通常会話向けの `native.input` / `native.context` / `native.completion`、Taskの再claim、自己jobのwaitを必須手順にしません。未知のroleや不足した文脈を通常会話Agentへ勝手に読み替えず、割当てられたworkerの結果またはblockerを返します。markerがない通常会話では、以下の通常手順を使います。

## 接続

1. 接続済みなら、このSkillの `references/connection.json` を読み、以後はその `command` 配列（Python実行パス・`--home`・`--socket`）でCLIを呼びます。別の既定homeへ接続し直しません。未接続なら `daikibo connect --client codex --workspace "$PWD" --name "プロジェクト名"` を実行します。Claude Codeは `--client claude`、両方は `--client both`。Codex用Skillは `.agents/skills/daikibo_dev` に配置されます。Codexは `skill-relay` で原文を記録し、Claude用Hookが動いているとは仮定しません。
2. Hookの追加Contextにsession/project/sourceがあればそれを使います。なければconnectのsessionを使い、**今回のユーザー原文**を `native.input` で保存します。既にHookが同じ入力を保存したときは二重追加しません。原文を要約で置き換えません。
3. 接続設定のworkspaceと現在の対象を確認し、`native.context` で現在の工程と未確認事項を取得します。サービス停止後は同じcommandで `start` を実行します。重要判断・矛盾・期限付き例外はユーザーに提示します。表示しただけでackしません。
4. APIの引数を想像せず `daikibo call api.describe --json '{"method":"操作名"}'` で確認します。JSONは `--json @file.json` または `--json -` を使えます。`artifact.propose`／`artifact.revise`／`artifact.save` の説明には既存validatorに対応する `body_contract` が含まれ、supervisor promptには同じ情報が `contract_metadata` として含まれます。従来の `contracts` signature文字列は互換性のため維持されます。

## この会話で進める

- 今会話しているAgentが計画・説明を行い、`native.actions` で最大20件ずつAPIへ提案します。内部DBを手編集して状態を合わせません。
- 設定も会話経由で扱えます。repository.register、adapter.register、automation.configure、delivery.configure、document.importはnative.actionsに含められます。既存CLIがあれば `adapter.register` のexecutableにclaude/codexを渡し、標準のログイン・HOMEを使います。provider設定は不要です。
- 不明な技術細部は調査して判断し、製品の意味を変える場合だけ質問します。
- `program.next` の入力と不足に従い、原文→要件→シナリオ→責務/所有権→契約→実現性→設計→Task/テスト計画へ進めます。段階的実装は最終範囲を削る許可ではありません。
- 長い入力は `source.partition` / `source.packet` で読み、各元区間を `source.classify` へ対応付けます。packetを作ったことを要件抽出済みと呼びません。大きな工程は `program.partition_review` で分け、全scopeのレビューと全体phaseレビューを行います。

## 全体設計の前から分業する場合

- 全体計画を一人のAgentで完成する必要はありません。既存artifact経路で必要な要件/設計/契約の草案を作り、実Taskとテスト計画を定義し、`subplan.propose`へ局所unitと正確な(requirement,acceptance)範囲を提出します。参考に読んだ条件を実装担当と数えません。
- 別担当の提案を`children`へIDで渡し、親の未割当unitと組み合わせます。依存順序は実Taskから導出します。境界が複数契約を共有する場合は`boundary_contracts`でTask依存ごとに使用契約を指定します。関係を推測しません。
- `subplan.get`のpacket全ページを辿り、それぞれに実design/trace Reviewerを起動します。`subplan.packet`、canonical artifactと原文を必要時に取得し、情報不足ならblockedを返します。親のレビューは子の未実施レビューを代行できません。
- `subplan.audit`は子まで全件を検査し、結果だけをページで返します。草案の採用は従来のレビュー/変更経路を通します。同じ本文・版のdraft→acceptedは再分割の理由にはなりませんが、全体制約の追加・本文や追跡リンクの変更は再確認します。
- `subplan.coverage`で全体に残る条件とTaskを確認します。採用済みの全体要件・Taskを満たす親を作り、`subplan.compose`で通常のroot提案へ合成します。composeはTask作成、仕様採用、root採用、部分納品の操作ではありません。
- 合成後は下記のrootレビュー/activateと全工程を続けます。現在の採用rootを変更するならexpected_activeを指定します。古い子レビューや合成記録の欠落を古い親PASSで補わず、改訂と再レビューへ戻します。
- 独立した子の10phase実行機関や別のdeploy-ready状態は作りません。採用後の担当実行はworkstreamを使います。

## Local execution preparation

When the approved early local execution extension is available, use the
read-only `local_execution.inventory` pages to collect every exact inventory
ID and digest before calling `local_execution.propose`. Keep the same canonical
root program, subplan, production Task ledger, complete root obligations, and
normal implementation/test/integration/delivery gates. A local certification
does not adopt a subplan, advance the root phase, resolve unrelated blocked
work, or make a delivery deploy-ready.

Stage evidence is typed and current. An accepted scenario/design artifact is
usable with a managed review receipt whose subject is that exact artifact and
whose binding, role, PASS verdict, read-only flag, and judgment validity are
current. Feasibility needs a separate analysis Task limited to
`.daikibo-research/` and its observed review; an implementer receipt or a
selected production Task receipt is not a feasibility judgment. Each local
`feasibility` and `impact` review role must cumulatively cover every stage,
impact, and obligation marker over distinct packet runs. Follow every packet
page and provide Task-specific consumer/boundary dispositions; unrelated
blocked items remain in the inventory and are rejected only when their local
disposition is missing or unresolved.

For a proposal selecting an internal dependency chain, claim and execute the
earlier Task first. The local gate rechecks that particular Task's selected
dependencies and all external prerequisites at ready/claim/execute/candidate
transitions. After the formally adopted root route is current through
implementation, integration, or delivery, later complete/recheck gates may
use root authorization while local claim and certification records remain
historical evidence for their original epoch.

## 分割計画の採用と改訂

- Taskとテスト計画ができたら `breakdown.propose` へcanonical domainとinterfaceを参照するunit階層を提案します。親も含む全REQ/ACと全live Taskを葉unitへ割り当てます。親unitは集約であり、要件を消す手段ではありません。
- 一度に送れない計画は `breakdown.upload_begin` → `upload_put` → `upload_finalize` を使います。putは1MiB/200unit以下、expected_revision付き。同じ版への同じ再送だけ再利用できます。途中からresumeでき、欠落や上限超過を省略で解決しません。finalizeは提出であり採用ではありません。
- 大容量計画の一覧は `breakdown.units` と `breakdown.get(include_structure=false)` のページを辿ります。対象全体を改めて丸ごとコンテキストへ貼り付ける必要はありません。
- Taskは自分のdomainと、依存境界で使うcontractをread_artifactsに持たせます。unit依存はTask依存と一致させます。analysis/experimentを本番の受入達成に数えません。
- `task.plan_tests` の各checkは独立workspaceで実行されます。`delivery` の `produces`／`uses` 宣言だけでcheck間のbuild生成物共有を仮定せず、configure/build/testを一つのcommand/scriptにまとめるか、内容に束縛した外部cacheを明示します。
- 固定test契約: 正式checkはsealed candidateのcopyで実行します。保存されたsource、input、期待値、result、index、shardsは読取専用の証拠として扱い、verify中に再生成・修復・上書きしません。record mode（実装時）はこれらを生成できますが、verify modeはcandidate外のscratchでfresh計算し、保存値と比較して、成功・失敗の双方で保存inputのbytesを保ちます。check固有の収集例外は宣言したpytest/JUnitのreportまたはoriginal_reportだけで、commandにはreport例外がありません。既存のEXCLUDED_DIRSとbuild_inputs/build_outputs契約を維持し、除外へ科学的証拠を隠しません。例: 実装時にexpected.jsonをrecordし、verify時に外部scratchのfresh resultをexpected.jsonと比較します。exit 0やJUnit全case passでもinput_mutatedなら正式checkはfailです。

### Taskの正式test evidence

実装Taskの `spec`、`quality`、`test_adequacy` review contextには、同じTask binding・epoch・候補snapshot・凍結test planから選んだ `test_evidence` を含めます。公開read APIの `task.test_evidence(task, offset, limit, expected_selection_digest)` も同じ選択を返します。結果は全checkの正確な `total` と `next_offset`、`selection_digest`、checkごとの `check_digest`、選択receipt/run、`observed_summary`、`evidence.get` と bounded `blob.read` の参照を持ちます。大きなreportやlogをpromptへ埋めず、参照を必要な範囲で読みます。

選択statusは `unobserved`、`executed`、`failed`、`invalid`、`unknown` を区別します。通常test receiptはreviewの `judgment_valid` を必要としません。最新の同一check観測がFAIL、timeout、input mutation、checksum不整合、別candidate/binding/epoch、別check定義なら、それを明示し、古いPASSで補完しません。未知の履歴は履歴として残り、未実行をPASSにしません。`expected_selection_digest` が変わったページやreviewは `stale_evidence` になり、review自身のreceipt追加ではtest選択が変わりません。`test_plan` の提案reviewは正式test実行を前提にしません。

`task.plan_tests` は公開引数を増やさず、Control が Runtime 所有の既存
`VerificationMaterialCoordinator` を内部注入します。writer transaction 内で
Task の現在の revision・認可・状態を再読し、plan を保存してから保存済み Task/plan
を再読し、Task kind に依存する production の pytest/JUnit inventory 条件を含む
全ての Task 依存条件をこの current 正本で検証してから、既存 `pin_test_plan` で
Task 定義と plan/check 定義を固定します。初回 read 後に正規 Task revision が適用されても、
古い Task の条件で command-only plan を保存・pin しません。
`test_plan_frozen` event も同じ transaction に置くため、pin の失敗は plan、event、
material object の部分成功を残しません。candidate/run/check result はこの準備経路で
作らず、Runtime の実行時 producer は保持します。再capture は exact pin と来歴を増やせますが、
同じ Task revision/定義 digest と plan digest の意味identityは変えません。plan または
Task revision が変われば旧pinは current ではなくなり、履歴pinは read-only で保持されます。
- Formal checks materialize the sealed source snapshot into a fresh work directory; source scanning excludes repository .git files/directories, so the implementation checkout's Git metadata is not automatically available. A git command may fail or discover an ancestor repository; neither result establishes the candidate's content identity. Use the supplied snapshot/content manifests and declared inputs for verification, and keep recorded Git provenance distinct from freshly observed environment metadata. If a check needs Git behavior, declare and construct an explicit isolated fixture in permitted scratch rather than relying on the enclosing checkout's repository.

## system.backup artifact contract

`system.backup` keeps its existing result fields (`id`, `path`, `blob`,
`sha256`, `bytes`) and owner/private access rules. New completed `ZIP_STORED`
exports resolve through a private flat recipe whose segments reference only
immutable physical blob leaves. Recipes are create-only, never recursive, and
are published only after exact ZIP-byte and leaf-hash validation. `blob_read`
keeps its bounded range response; `blob_path` remains physical-only and gives
a controlled error for a recipe alias. Existing physical corruption never
falls through silently to a recipe. Restore validates recipe closure before
publishing, legacy compressed backups remain readable, and retention/history
consumers use the same bounded artifact reader. GC validates every recipe
closure before any unlink; backup and GC share the existing store lock.
Authorized size and range calls share one Store-owned verified session. It
binds the actual recipe bytes, ordered reassembly hash, and opened physical
leaf identities; authorization runs before session lookup. Namespace changes
evict cached sessions, while unsupported monitoring falls back to complete
validation per request. The 256 GiB limit covers expanded payload files;
manifest bytes and observed ZIP framing are bounded separately.

- `breakdown.get` の全ページを辿り、各packetに `design` と `trace` の別review Jobを起動します。packetのrequired_coverageを全て扱った実receiptが必要です。fragmentを読めない・意味を判断できないときはblockedを返します。
- `breakdown.audit` が通ったら `breakdown.activate`。改訂時はexpected_activeを指定し、変更のないpacketだけ既存レビューを再利用します。仕様・入力版が変わったpacketは再レビューします。
- 全体やモジュール設計へ戻るときは `program.reopen` に原因IDと現在revisionを渡します。Agent判断には実impactレビューを添えます。戻っただけで全Taskを取消にしません。
- delivery.configureにprogramを指定します。新計画を採用した後、旧計画で作ったdeliveryを流用しません。

## Task定義の修正

- 同名のACを別要件に二重計上しません。`acceptance_refs`で実装する `{requirement,acceptance}` を明示し、読取りだけの要件と区別します。曖昧な旧Taskは正式に改訂します。
- 目的・入出力・受入・依存を変える場合は `task.propose_revision` で前後を固定し、提案IDへの別の実impact Reviewerを起動します。最新のreceiptとdigestで `task.apply_revision`。レビューの起動宣言や他の提案のPASSは代用できません。
- Task本文を変えず、同じ定義の凍結test planだけを修正する場合は `task.propose_plan_revision(task,expected_revision,expected_plan_digest,body,reason,evidence_refs)` を使います。完全な新planと現在Taskの実receipt IDを固定し、旧FAILも観測履歴として保持します。impact review後に `task.apply_revision` を行うとTask revision/epochとplan materialを同一transactionで更新し、Task本文・旧candidate・attempts・counterを変えず、真のafter履歴を保存します。返る `new_test_plan_required=false` はplanが承認済みという意味ではなく、再実行・再レビューが必要です。
- 改訂後は新しいテスト計画・実行・レビューが必要です。旧candidate/planは履歴、過去試行は累積のままです。製品要件やリスクを勝手に緩和せず、必要なら既存の裁定へ戻ります。
- `task.revision_list` で未完了提案を再発見します。適用済みを繰り返し照会しても新たな完了証拠とは数えません。
- 定義を変えずに入力版を更新する場合だけ `task.replan`。後続の影響部分を再確認し、無関係なTaskや未解決裁定を消しません。

## 裁定と変更

1. 新仕様と既存仕様・裁定に矛盾があれば `conflict.report` と `decision.propose` へ記録します。
2. 質問前に `native.present_decision` を呼び、その提案と選択肢をユーザーへ説明します。
3. 次のユーザー回答のsourceと正確な引用、提示したdigestを `native.respond` へ渡します。別端末へ移動させず、本人の暗号学的認証もしません。曖昧な返事を同意と解釈しません。
4. 回答を受けたら `consistency` Reviewerを実際に起動し、そのreceiptで `decision.apply` を行います。ユーザーの新裁定も、残る仕様との矛盾を再確認してから適用します。
5. 技術的な問題はコード→モジュール→システム設計の順に根拠付きで戻します。勝手な仕様緩和・予算切れを不能証明にすること・無関係作業の全停止をしません。

## 実行・検証・報告

- `job.submit` で実装/テスト/Reviewer/探索を起動し、`job.get` または `daikibo wait` で実際の結果を読みます。提出受付や文章上の宣言は実行済みではありません。
- Taskをclaimしてからexecuteします。テスト計画、実行、spec/quality/test_adequacy等の**別run**のレビューを経て `task.complete` へ要求します。OS権限分離は不要ですが、実行分離・証拠照合は必要です。
- `retry_wait` は未完了です。`daikibo wait` またはjob.getで予定・過去試行を確認します。read-onlyレビュー等の一時障害だけ自動retryされます。実装の失敗にpartial_workがあれば保存snapshotと変更一覧を調べ、再計画します。勝手に採用やexecuteの再送をしません。
- レビューの意味上のfailと接続エラーを区別します。合格が出るまでreviewを繰り返さず、指摘へ対応します。`execution.usage` のunknownをゼロ扱いせず、制限変更で計数をリセットしません。権限内で設定を変えても、要件の達成不能を意味しません。
- 原文と履歴を外へ持ち出す場合はbaseline.create(layout="auto")とbaseline.exportを使い、破損・欠落を検査します。大きな記録や提出途中の計画は分割アーカイブとなり、全ファイルをセットで保持します。運転の復元は別のsystem.backupを使います。archiveが読めることをレビューやテストの再実行済みとは扱いません。
- 完了と言う直前に `native.completion` で対象Task/Delivery/Programを再確認します。`completed:false` ならblockerまたはチェックポイントとして報告します。
- 全体納品には `delivery.certify` が必要です。全programの正式完了は現在版の `program.completion` を確認し、実phaseレビューのreceiptで `program.finish` を要求します。Task単体の合格やfixtureの成功を配備可能な完成と呼びません。
- validation、未登録/未資格の実CLI、未実施のレビュー、古い証拠を実行済みと表示しません。
- 接続エラー時は工程が確認できないと報告します。通常の仕様議論を終えることは許されますが、未完の作業を完成と呼びません。

詳細は `references/operations.md`。管理システムを使う協調動作が前提で、同権限による故意の改ざん防止は対象外です。

## dev18 execution controls

Dev18 uses schema 13 and keeps the existing `tasks.attempts` field as
telemetry. The separate `tasks.no_progress_count` is incremented only once
for each conclusive, independently reviewed `no_progress` assessment of an
observed attempt; it does not reset on `progress`, and the fixed admission
threshold is 3. Claim, run, receipt, assessment, proposal, packet,
authorization, and event history remain queryable. A claim-only or interrupted
attempt is retained as unobserved evidence and never treated as progress.

Use the bounded `execution_control.inventory`, `get`, `propose`, `packet`,
`apply`, `withdraw`, and `history` APIs, plus `task.progress` and
`project.progress`. A proposal body names `target_attempt_epoch`, optional
`target_attempt_ordinal` and `target_implementer_run`, one `control_type`
(`assessment`, `recovery`, or `timeout`), optional finite
`requested_seconds`/`old_effective_seconds`, `cause_analysis`,
`experiment_estimate`, canonical `evidence`, `intended_next_action`, `scope`,
and `recovery_action` for recovery. Timeout requests use the exact observed
run timeout for `old_effective_seconds`; do not invent a prior authorization.
The apply route first validates the latest observed `execution_control` review
through the normal governance review gate. A failed, timed-out, mutated,
schema-invalid, or unqualified review cannot create an authorization or alter
the counter.

The ordinary task timeout defaults to 14,400 seconds (four hours). A finite
duration longer than four hours or 24 hours is allowed only after a separate
observed review approves that exact timeout and its cause/evidence. There is
no arbitrary 24-hour cap. The check timeout remains independently defaulted
to 300 seconds. Timeout approval, recovery approval, and progress
classification are typed decisions: one does not imply another. An
inconclusive marker remains nonfinal; a conclusive attempt classification is
retained even when an independent timeout or recovery decision is still
inconclusive.

Execution-control review dispositions use exact vocabulary. For
`attempt:<epoch>`, `resolution` is `progress`, `no_progress`, or
`inconclusive`; for `recovery:<proposal>` and `timeout:<proposal>`, it is
`approved`, `rejected`, or `inconclusive`. `approve` and other aliases are
invalid and are never normalized. A schema-invalid receipt retains its raw
evidence but cannot authorize an apply or alter a counter. Other review roles
keep the general disposition contract.

Across review roles, `pass` requires `findings=[]`; a remaining unresolved
finding requires `fail` or `blocked`. Findings describe unresolved problems in
the subject or decision under review, while observations carry evidence with
exact references. For `execution_control`, observations may record existing
implementation, quality, or test defects; include such a defect in findings
only when the evidence explains why it invalidates the assessment or requested
authorization. Do not mechanically move defects or force a progress conclusion.

Recovery is an independently reviewed admission decision for the latest
durable failed, unknown, or claim-only lease/run evidence. It never performs
a blind retry, creates a progress classification for a claim-only target, or
bypasses stale inputs, dependencies, review, resource, or other existing
gates. A normal reviewed replan cannot erase an unresolved recovery blocker.
When a later claim consumes an approved recovery, the new claim body records
the authorization ID and the operational blocker is cleared only for that
consumed target.

Policy v2 adoption is source-backed. Call
`execution_control.policy_propose` with the raw human source `{id,digest}`,
the accepted requirement `{id,revision,digest}`, the current policy
`{revision,digest}`, any exact pending policy decisions to supersede, and a
reason. The route verifies the retained observed requirements review, stores
the immutable proposal in `decisions` with `response: null`, then requires a
distinct observed `consistency` review over the exact source, accepted
requirement, policy diff, and superseded records before
`execution_control.policy_apply`. Lexical agreement or an owner assertion is
not approval; old pending records and their adoption event remain history.

The source and installed/assets copies of this section must stay byte-for-byte
identical. Validation fixtures, source checks, package installation, live DB
cutover, and final wheel observer results are separate evidence and must not
be implied by these instructions.

Execution-control mutations resolve the stored Task or proposal first and then
check the caller's actual project and Task capability before material checks,
writes, or idempotent replay. A project-scoped native agent may operate on
that project's target Tasks; a Task-scoped agent must match the exact target
Task. Owner and agent roles remain the only mutation roles, and native routing
does not elevate a caller or bypass currentness, independent review, or replay
checks.

## dev19 read-only progress reporting

`task.progress` and each `project.progress` item retain every dev18 numeric
counter, telemetry field, history snapshot, and exact `admission` payload.
They add `reporting.format=daikibo.task-progress.v1`, whose context states
that admission covers `prior_epochs_of_current_task_epoch` and is not a
next-claim authorization. The reporting snapshot is a SHA-256 of the factual
projection without its own snapshot field. These routes do not create policy,
review, assessment, archive, or schema records.

Read `reporting.current_claim` for the exact current task epoch and
`reporting.latest_attempt` for the maximum retained observed epoch, including
the current epoch. The latter separates implementer observation, verified
completion-review roles and bindings, and a finalized progress assessment.
Missing evidence is `null`/`unknown`; ambiguous legacy ordinals, revisions,
or multiple implementer runs stay explicit and are never inferred from row
order or cumulative `attempts`. Receipt verification and the normal review
gate determine whether a role is `pass`, `fail`, `blocked`,
`execution_failed`, or `invalid_or_unknown`; PASS receipts from different
candidate/input bindings are never combined.

`reporting.next_claim` is an explanation for the existing lifecycle state, not
a permission or scheduler instruction. Submitted work is
`reassess_current_result`; an active nonterminal claim is
`wait_for_current_execution`; ready and planned tasks explain their existing
gates; completed and cancelled tasks are `not_applicable`. A paused task keeps
its lifecycle-derived state and adds a paused explanation. A
successful implementation is never treated as completion. A project page
digest includes each compact reporting snapshot and evidence identity, so a
new review receipt invalidates an old pagination snapshot even when the task
row is unchanged. Project pages construct detailed reporting only for the
requested task page; their global snapshot is compact and still covers
evidence outside that page. Ambiguous run/receipt references are bounded with
exact totals, truncation flags, and a detail pointer. Use the existing
history/evidence APIs for full details. A no-profile baseline read uses the
same digest-equivalent capture and dependency assembly with blob storage
disabled; ordinary execution/admission callers retain their storing behavior.
If a bounded ambiguous reference includes `run_detail` or `receipt_detail`,
follow its `execution_control.history_detail` route through the public
dispatcher with `kind`, `attempt_epoch`, `offset`, `limit`, and the returned
detail snapshot for later pages. This reader exposes retained raw identities
without legacy reconciliation or admission changes; changed evidence rejects
an old page as stale.

## Claim admission diagnostics

Before assigning independent workers, call
`task.parallel_candidates(project, limit=100, offset=0)`. Use
`available_capacity`, each Task's `can_claim_now`, `unmet_dependencies`,
`running_conflicts`, and `candidate_conflicts` to choose a non-conflicting
batch. Candidate-to-candidate conflicts cover only the returned page; follow
`next_offset` before treating the response as a complete inventory. This read
does not reserve work. Claim every selected Task normally and accept a later
rejection if dependencies, policy, capacity, or resources changed after the
read.

`task.claim(project, task=null)` keeps the existing claim ordering and gates.
When no candidate is claimable it returns the existing `no_work` error with a
list-shaped `details` value when the scheduler examined candidate-specific
failures. Each entry has the selected candidate Task ID, a `stage`, and the
canonical `failures` reported by that check. Typical stages are
`task_state`, `execution_admission`, `ready_gate`, `local_authorization`,
`dependency`, and `resource_conflict`; existing early errors such as
`forbidden`, `paused`, `capacity`, and an actual admission exception keep
their existing behavior.

Automatic selection examines up to `scan_limit` candidates (default 1000).
If unsearched candidates remain, it returns `search_incomplete` with
`next_after_task`; continue with `after_task`. `no_work` means the remaining
candidate range was exhausted. It is a bounded diagnostic, not a complete Task
inventory. Details contain only candidates authorized by the caller's project
and Task scope. They describe checks already performed and never grant claim,
recovery, replan, counter, or execution authority. A successful claim keeps
the existing Task result and does not include diagnostics from skipped
candidates.

## Bounded discovery for large specifications and pending decisions

Use `artifact.catalog` and `inbox.catalog` before requesting bodies. Follow every `next_offset` with the returned `expected_snapshot`; restart if a catalog is stale. Read `artifact.read` / `inbox.read` using the exact catalog digest and concatenate the exact character ranges when necessary. A label or partial fragment is not the specification. Use `program.catalog`, `program.blockers` and `workflow.summary` for bounded planning views.

Native context notifications are indexes, not acknowledged/read decision bodies. Present pending user-facing notices, fetch their exact content, and use the existing decision/acknowledgement workflow. Never clear them because an index appeared in the context.

Resume staged plans with `breakdown.upload_list` after a reset. Finalizing an upload creates a proposal, not an adopted plan or a completed development program.


## 担当範囲を委譲する

- rootのbreakdownを採用してから `workstream.propose(program,title,rationale,unit_ids,parent=null,previous=null)` を使います。unit_idsは葉unitです。Task/要件のコピーや受入条件の省略はしません。
- `workstream.get`の全packetを実design/trace Reviewerへ渡し、`workstream.activate`を要求します。子の担当範囲は親のsubsetです。兄弟の重複は不可、未割当部分は親/rootの責任のままです。
- `workstream.selection(kind=tasks|obligations|units|external_dependencies)` のbounded pageで担当を確認します。実行は既存Task APIを使い、外部依存や現在入力を省略しません。
- `workstream.completion`を再確認し、`workstream.finish`で担当作業だけを正式記録します。`deploy_ready:false`です。全体納品にはroot program/deliveryの全Gateが必要です。
- 担当返却は以下の分割・集約手順を標準とします。旧小容量API `workstream.withdraw` も残りますが、資料の上限を回避するために切り捨てて使いません。
- root計画の変更・範囲差替え後は旧scopeを流用せず、子から順に返却して再提案します。無関係な兄弟のTask変更で担当内容が同じなら継続できます。
- 全体integration/goal_validation reviewはcontextのrequired_coverageを満たします。同名ACの要件別markerを表示名だけで代用しません。意味を確認できなければblockedです。

## 標準契約資料とリモート納品

- 原文をdocument.importで保持した後、`contract.inspect_document(document,expected_digest)` を使います。標準契約は現時点でJSON表現です。YAML等を勝手に緩い型へ読み替えません。
- catalogのentries/issues/referencesをページで確認し、`contract.read_entry`の範囲を辿ります。`contract.propose_document`は意味・consumer・検証方法を明記した草案を作るだけです。取り込み、索引、比較は規格妥当性・互換性・採用の証明ではありません。
- 元資料と結び付いたmaterialを都合よく編集しません。文書変更は新資料と差分/影響レビューへ戻します。未解決参照を隠さず、通常の採用Gateを実行します。
- `remote.configure` はGitHub/GitLabの送信先を固定し、`remote.publish` は現在のdelivery Gateを再確認した管理Jobを作ります。送信意図が不明ならユーザーへ確認し、診断だけで外部PR/MRを作りません。
- 送信結果が不明なら `remote.status` とJobの履歴を調べ、同じintentで外部を照合します。文面/宛先を変えてblind retryしません。POSTの成功を完成と呼ばず、読み返したcommit/branch/repo/状態と記録された証拠を確認します。自動merge/deploy/force-pushは行いません。
- adapterの登録や`--version`成功だけを実モデル資格・品質の証拠にせず、実接続で資格試験を行います。

## 大きな担当範囲の返却・再計画

- 通常は `workstream.return_propose(scope,reason,byte_budget)` を使います。理由・現在入力・親/rootを固定した未適用提案で、Task/要件はそのままです。
- `workstream.return_get` の全leafページを読み、各IDへ `job.submit(kind=review,args={subject:...,role:impact,adapter:...})` を要求します。各結果には実行記録とexact required_coverage、具体的なrationale/observationsが必要です。
- `workstream.return_advance` で未完了ノードと次段の集約ノードを取得します。次段には子の結果全文が渡されます。子がPASSという理由だけで合格にせず、境界・返却先・残存責任を判断します。判断できなければblockedです。
- 全段の審査後、ready=trueのroot_packet/root_review_receiptと元proposal digestで `workstream.return_apply` を要求します。断片だけの完了や古い集約は代用しません。
- 最新の下位結果が変われば集約も新しく必要です。新旧履歴を消さず、無制限なpass探しをしません。上位要件が変わった場合は再提案へ戻します。
- 一つの結果が大きすぎる場合、システムは切り捨てず停止します。情報を隠さず明確な判断へ再整理するか、許された範囲で大きいbudgetの別提案を作ります。
- セッション喪失後はreturn_list/get/advance、読むだけの過去資料はreturn_packet(historical=true)。過去資料の表示は新しいレビュー実施ではありません。
- 取消ではなく提案を捨てる場合はreturn_abandon。割当・Task・要件はそのままです。子が有効なら先に子の扱いを解決します。

## dev28 traceability Unit A

固定Git commitのコード母集団は、登録済みrepositoryと完全commit OIDを指定して
`traceability.propose` → durable jobとしての`traceability.extract`を実行し、返されたjobを
`job.get`で確認します。worker再起動後もproposalのstaging checkpointから再開できます。
`traceability.list` と
`traceability.items` は既定100、最大500のopaque cursorページで、続きは同じsnapshotの
`next_cursor`を渡します。完全な1 itemの本文は `traceability.read` のbyte rangeで読みます。
working treeやbranchのHEADを元版として扱わず、`unknown`（unsupported、parseerror、
invalid UTF-8、symlink等）を削除・成功扱いにしません。空ファイルはunknownではなく、
0-byteの明示的な既知対象として保持します。

UTF-8仕様文書は登録済みsource blobをdocument populationとして同じflowに渡します。
行のBOM/CRLF、Unicode座標、raw byte座標を保持します。抽出済みrevisionはUnit Aでは
`ready`/historyに留まり、`traceability.coverage`がUnit Bのplanning/closureを代行せず、
`traceability.adopt`もactual review/CAS gateなしには成功しません。履歴を移す場合は
`traceability.export`、`traceability.inspect_archive`、`traceability.import`を使いますが、
archiveを読めたことや過去のreview IDを新しいPASS・test evidenceへ昇格させません。

## Edge assurance E2

構造保証を提案するときは、`assurance.scope_propose` で exact な roots、selection、除外案、authority、discovery unknowns を固定し、`assurance.profile_propose` で適用stage・relation selector・obligationsを固定します。続けて `assurance.edge_propose` と `assurance.set_propose` を使います。edgeの両端は13関係registryのtyped refでなければならず、setはcallerのsubsetを受け取らず、指定されたcenter・relation・direction・scopeから現行edge分母を導出します。source全体とspan、populationとitem、test planとcheckのようなcontainer/member関係は完全なrefと保存された本文を照合します。

`assurance.set_propose` の `criteria` はregistryで宣言された要求条件であり、達成観測の `criteria` mapとは分離して `criteria_requirements` とdigestへ固定されます。未知条件やfalseによる義務解除は拒否され、markerのwire型も厳密に検査されます。adopt、current resolve、reportはCAS headだけでなくtyped依存閉包を再評価し、pinned readは歴史identityとして保持します。standard archiveはimmutable assurance object全体のtyped endpoint閉包を検査し、旧opaque identityは同一digestの保存根拠がある履歴だけを読めます。

Supervisorの構造progress projectionも同じreadonly typed-reference境界を使います。`current=False` の歴史読取はproject、revision、body shape、exact digest、CAS/acceptance pointerを検証しますが、後続withdrawalやsupersedeで変わったartifact status、adoption、lease、資格を過去正本の存在条件にしません。`current=True` と `evaluate_current` は引き続き現行accepted/current identityを要求し、欠損、foreign、破損body、digest不一致は空の投影へ変換せずFaultとして停止します。

各proposalは不変object、CAS material、最大500 leafのbounded packetを作ります。setには個別edge用trace packetと集合全体の相互作用を審査するimpact synthesis packetが別に存在します。packetを読んだだけ、ownerのaccepted、個別edgeのPASS、fixtureの結果は意味上のPASSではありません。`assurance.review_subject` のcursorを使って全packetを読み、実review receiptをpacket digest・role・required coverageへ束縛してから `assurance.adopt` を呼びます。scope、obligations、profile、edge、setのcurrentnessとCAS headは再検査され、receipt不足・古い分母・別subjectのreceiptは拒否されます。

`assurance.resolve` は完全なtyped refを受け、contextを省略したときはpinned identityだけを返します。current評価には採用済みprofileとstageに応じたtask/delivery refを指定します。`assurance.report` は `missing`、`stale`、`unverified`、`unknown`、`failed`、`current` をbounded pageで返します。実Runtime reviewはpacket subjectを既存のgoverned review経路へ渡しますが、E2のfixture subprocess結果やarchive inspectionを実LLMレビュー・実行証拠へ昇格させません。

E3単位1では `assurance.profile.v2` のclosed bodyだけを受け付けます。`project`、実在する同一`program`、exactな`scope_ref`/`obligations_ref`、`previous_selection_ref`、`application_mode`、4段階すべての厳密`stage_rules`、`node_review_rules`、有限`relation_selectors`、実checkへ束縛した`test_definition_bindings`、`change_reason`、`authority_refs`が必要です。`assurance.catalog`のdenominator、execution result、relation center、node selector/roleだけを使い、未知key、欠落、null、bool/int混同、重複、最低義務の削除は拒否されます。旧`profile.v1`は`migration_pending`の履歴として読み取り可能ですが、自動canonical化しません。

E3 Unit2cの明示分母を宣言する場合、artifactの`structural_obligations`は`daikibo.structural-obligations.v1`のclosed objectとして、domain/design/component/interfaceだけに指定します。各項は`statement`または厳密な歴史`domain_reference`（artifact pin、boolではない責務index、責務digest）で、未知key・foreign pin・別版digestは拒否されます。既存domainの`responsibilities`は必ず全件が正本で、宣言が無い`legacy_unavailable`と明示空の`explicit_empty`を同一視しません。キーが存在する明示`null`は欠損扱いせず`invalid_input`として拒否し、保存済みの不正nullはdenominatorで`invalid`/`unverified`として保持します。

Taskの任意`structural_obligations`は`daikibo.task-structural-obligations.v1`で、`required_outputs`と`required_exercises`を明示します。各artifact_refはTaskの実`read_artifacts`に含まれる既存pinでなければならず、義務を`write_paths`、未来candidate、producer discoveryやDelivery outputから推測しません。`required_outputs[*].realization_kind`は厳密な文字列`candidate_member`または`artifact`だけを受け付け、配列・object・bool・null・整数は構造化`Fault`で拒否します。宣言はTask定義digestに含まれるため意味変更は新しい分母入力digestになり、status・telemetryだけの変更は入力digestを変えません。欠損・明示空・未実装のoutput_artifact/registry・stage gateはそれぞれ区別して表示し、未実装をPASSへ変換しません。

### Consumer-P artifact producer

`task.artifacts_collect(actor, task, expected_revision, candidate, repository, path)` は、現在Taskに採用されたsealed candidateの実subprocess出力だけを、固定された`required_outputs`の`realization_kind=artifact`へ変換します。callerはcandidate/repository/pathのselectorだけを渡し、artifact body、run、receipt、epoch、producer actor、manifest digestを自己申告できません。manifestは`daikibo.artifact-output.v1`のUTF-8 JSONで、1MiB・1 packet 200項のtransport上限を持ちますが、Task全体のartifact数を制限するものではありません。

controllerがrun登録時に保存したproducer actor、Task revision、epochと、candidate provenanceのrun/receipt/snapshot/CAS/Task履歴を同じresolverで検査します。成功した収集は既存Knowledgeの`draft` artifactとimmutable `artifact_production` materialを作り、意味review・accept・completionを代用しません。同じcollection keyの再送だけが同じ結果を返し、別bytesや別revisionを古い来歴へ上書きしません。standard archive/GCはTask、candidate、run、receipt、repository、artifact revision、manifest CASの閉包を保存し、archive readもliveと同じvalidatorを使います。旧候補のproducer record欠損は後からcallerで補完せず、unknown/invalidとして保持します。

v2のcanonical logical IDは `profile:program:<program>` です。bootstrapの`expected_head`と`previous_selection_ref`はnull、二回目以降は現在head eventと完全typedな前選択refを同時に指定します。`assurance.adopt`は既存の実review receiptと同じwriter CASで選択を進め、競合を`stale_head`として保持します。replacement/`disabled`にはsource-backed authorityとreviewが必要です。提案だけではstage gateを有効化しません。採用済みprofileを読むread-only evaluatorと、Runtimeの実preadoption observationをcandidate INSERT前に渡すprivate接続は有限実装されていますが、全writer enforcementと完成gateは後続です。

profile.v2のauthorityは既存の3系統だけを解決します。sourceはblob digest付きsource ref、changeはrevision・body digest・material pinと保存されたimpact/target/source関係を持つchange ref、decisionはacceptedな`artifacts.kind=decision`のartifact refと保存されたsource/target関係です。scope/profileなどのassurance objectや別kind artifactを権限として扱わず、根拠を機械解決できない形はunknownとして拒否します。changeの`baseline_refs`は現在のartifact行ではなく不変`revisions`履歴へ照合し、`reconciling`では対象がそのbaselineのまま、`ready_for_reimplementation`では保存deltaの適用後revision/bodyと`withdraw`が示すcanonical status（acceptedまたはwithdrawn）がartifact行とcurrent revisionの双方で一致することを別に検査します。適用後の無関係な再改訂はauthorityを失効させますが、pinned changeの歴史読取は保持します。profile replacementのchange/decision authority対象は、候補profileとその`previous_selection_ref`が指す不変predecessor profileのscopeの和集合から評価し、adopt後の同一profile・同一receipt再送でも同じtransition意味を再検査します。`event.previous`、`event.expected_head`、event bodyの`selection.previous_selection_ref`、profile bodyの`previous_selection_ref`は同じ前選択の完全typed identity（bootstrapでは全てnull）でなければなりません。live replayとarchive検査は同じ規則を使います。`test_definition_bindings`のartifact kindはbody内の重複フィールドではなくcanonical artifact rowの`kind`で検査します。

## Managed output contract

Real managed implementers and reviewers keep exploratory output bounded: redirect stdout and stderr to separate files in the currently authorized owned scratch area, preserve the original exit status, and report only purpose, status, IDs, per-stream bytes/SHA256, failure location, and a byte-bounded excerpt. A successful final pipeline or summary does not establish the original command's success. Formal CLI stdout keeps its exact machine JSON/schema and is redirected by the caller; a summary or truncated log never substitutes for a complete scientific check. Do not print secrets/raw environment, modify sealed candidates or evidence, add log paths outside the existing Task contract, or call a worker home/tmp path saved evidence. Required failure evidence uses an already authorized durable path and is retained until existence and SHA are confirmed; otherwise report it missing. Output-limit observations remain failures, and partial work is never an automatic candidate.

Runtime/Delivery の実行結果を assurance の `observed_result` typed ref として読むときは、receipt/runのproject、subject、role、task/epoch、binding、snapshot、check、resultを同一の実行記録として照合します。controllerが保存したverification materialが無い旧runは `legacy_unverified` のまま保持し、失敗receiptを成功へ変換しません。execution materialの `test_artifact_refs` はcaller入力ではなく、current adopted profileの `test_definition_bindings` と実artifact/check本文から導出されます。artifactのtest判定はrevision bodyの重複フィールドではなくcanonical artifacts row.kindを使います。snapshot、実行argv、Popen environment、timeout、nested CAS childはmaterialの不変依存として保存し、standard archiveはliveと同じreadonly execution-material relation（run/receipt pin、capture run、subject/candidate、check、artifact、snapshot、launch、environment、timeout、typed dependencies）をcontext/CAS付きで検査します。

Delivery の非Git build outputは `output_artifact` の exact ref として扱います。ref は pinned Delivery snapshot、Delivery check、observed receipt の三つを束ね、6項目（id/repo/path/blob/bytes/mode）の canonical digest と実CAS bytesを照合します。`assurance.pin` は保存済みの `delivery_output` material からのみ解決し、失敗実行・別check・別receipt・宣言外producer・古いcurrent行を成功outputへ補完しません。`contains` の `delivery_build_output` は納品bundle所属を表し、Git tree所属を表しません。relation registryは省略時に旧v1を維持し、v2 digestを明示したときだけ output endpoint を有効にします。archive/GCもmaterialのpayloadとoutput CAS childを同じ閉包として保持します。

observed_result の task 実行は、保存された候補refを `resolve_candidate_identity` と同じ read-only `PinnedContext` で解決します。Taskの現行行だけで旧candidateを代用せず、保持された before/after history、implementation run/receipt、repositoryとsnapshot/CASの閉包を検査します。standard archiveも同じ保存context adapterを使い、context省略やID/shapeだけの定義callbackを強い検証に昇格させません。test_plan_check と delivery_check はそれぞれ不変plan/delivery materialとnested check body/digestを保存CASから解決し、実行時に調整されたcheckのidentityと原定義のidentityを混同しません。

さらに共有execution-material validatorは、test planのproject/task/revisionとTask execution subject、候補の歴史定義digest、候補snapshot、実行input snapshotを同時に照合します。delivery checkもdelivery ID、binding、sealed snapshotを実行subjectとinputへ結びます。同じcheck本文でも別Task・別Task revision・別Deliveryのmaterialは受理されず、正当なhistorical pinはその固定identityのまま読めます。


## Consumer-C profile.v3（有限実装）

`assurance.profile.v3` は v2 の closed body に
`required_relation_contract_digest` を一つ加えた厳密な形式です。この値は
`REGISTRY_V2_DIGEST` と完全一致し、v2/v3 は同じ
`profile:program:<program>` canonical head を共有します。v2 の保存bytesと
旧 v1/v2 history は変更せず、v1 は `migration_pending` の履歴として扱います。
v3 の提案・採用は選択を更新しますが、stage evaluator や gate completion を
有効にしません。

read-only Unit3 の plan/Task 経路は canonical selected profile の wire と選択metadataを
同時に検証します。profile.v2 は `REGISTRY_V1_DIGEST`（旧 v1 relation wire）、profile.v3 は
`REGISTRY_V2_DIGEST`へ固定し、callerが渡す registry文字列を authorityにしません。effective
registryは `registry_entry`、adopted setの照合、sealed `build_relation_request`、criteria結果、
capability、semantic fingerprintへ同じ値で伝播します。unknown、型不正、selection metadataと
bodyの不一致、stale profileは `invalid_registry`/`integrity_error`/`stale` の診断として保持し、
評価は保存を書きません。

v3 の plan/Task context は Delivery material が無い場合も明示的な unavailable inventory として
導出でき、nullを成功分母へ変換しません。実 Runtime の Task/plan review、M/R edge/set、N/E/S
が揃った正例だけが read-only relation result を満たします。missing N/E/S、missing set、stale
dependencyは blockingのままです。integration/delivery producer、全writer enforcement、
完成stage gate、全13 relation、installed/live、公式ZIP、実LLM受入はこの有限経路の後続です。

公開 `format` discriminator は型を先に検査します。明示された null、bool、list、
object、未知の文字列は構造化 `invalid_profile` Fault で拒否し、format を省略した
旧 v1 入力と正規 v2/v3 の分岐を維持します。

v3 の delivery 分母は、controller が pin した Delivery snapshot の
`build_definitions` と対応する `produces` check からのみ導出します。定義は
exact な id/repo/path、重複しない producer、sealed snapshot を必要とし、欠損・
foreign・曖昧な producer は unresolved/invalid のまま保持します。実成果の
成功数で分母を縮小せず、`delivery_declared_output` の obligation として
保持します。read-only Consumer-C matcher は同じ分母を live または標準
archive の resolver へ渡し、宣言定義、producer receipt、6項目 output/CAS、
同一 snapshot/check、`delivery_build_output` membership を共通 validator で
照合します。実 producer failure、観測欠損、material 欠損、historical pin は
それぞれ `failed`、`missing`、`unverified`、`stale` として保持し、Task
`required_output` とは別の owner/category を使います。機械的な対応が全て
揃っても `semantic_status=unverified` であり、Consumer-M/R の owner・center・
direction・set alignment、N/E/S、synthesis、review adoption、stage gate の
完了を意味しません。受理済み M/R の owner・center・set 境界へ接続した後も、実 E/S
receipt と synthesis が揃うまで semantic completion を主張せず、Delivery typed refs
だけの空N人口にはN receiptを捏造せず、mock consumer や自己申告で接続を補いません。

有限C fixtureでは、one-output localとsnapshot-global two-outputの
`produced_by`/`contains`が実E/S Runtime receiptとsynthesis adoptionまで通過します。
この範囲のN人口は0であり、N receiptを得たとは扱いません。全13関係、Unit3/stage、
installed/live、公式ZIP、実LLMの受入は後続です。

producer status は failure flag を解釈する前に shared observed-result resolver
を通します。receipt/run の project、subject、role、epoch、binding、snapshot、
result と execution material の check、Delivery subject、CAS closure を同一の
execution record として照合します。foreign、malformed、または material 欠損は
`unverified` に保持し、同じ check に束縛された正当な exit 1 は `failed` として
保持します。失敗 producer に成功 output material は要求せず、観測自体が無い
場合だけ `missing` を返します。

v2 の C relation edge は保存された `relation_contract_digest` を currentness、
adoption、set reader、replay の全てへ渡します。未知の digest は拒否し、digest
省略時だけ旧 v1 registry を選びます。`produced_by` と `contains` の C set は
controller が pin した同じ Delivery 宣言から immutable な
`delivery_declared_output` obligations を作り、caller の edge claim ID を分母の
根拠にしません。set の `consumer_binding`、pinned snapshot、宣言 identity、
producer/check identity を再読して coverage を決めるため、edge が空 claim でも
自己申告の PASS にはなりません。v1 の保存 obligations/edge/set history はその
body と digest のまま読み続けます。Delivery typed refs は Unit2b artifact/Task
node population ではないので C の N 境界は空集合を明示的に扱い、実 E/S receipt、
meaning/synthesis adoption、Unit3/stage gate は別の必須後続条件として残します。

## E3 Consumer-M/R

Unit2aのsealed context/denominatorからrelation populationを読むときは、内部controller API
`build_relation_request(control, actor, *, context, denominator, relation, center_ref,
direction, scope_ref, registry_digest, projection=None)`を使います。requestはglobal denominator
digest、必要obligation、全contributor owner、current center、scope、任意のTask projectionを
固定します。Task projectionはglobal obligation/contributor集合の局所投影であり、callerがIDを
削ったsubsetや別ownerを渡すことはできません。JSON copy、別project、古いcenter/scope、nullや
unknown fieldはauthorityになりません。

`evaluate_criteria(..., relation_request=request, relation_reviews=reviews)`の新しい8カテゴリは
mechanical eligibilityだけで`satisfied`になりません。source span、parent/child requirement、
artifact責務、Task required output/exercise、assignment、impactをcanonical endpoint、version、
pointer、CAS、membershipへ照合します。共同contributorは全Taskを必要とし、artifactからTaskの
producer来歴が未保存の範囲は`unverified: artifact_producer_material_missing`として残します。
旧4カテゴリの互換入口は維持しますが、新カテゴリを旧raw edge claimからPASSへ昇格させません。

実意味レビューは`build_review_assurance(control, actor, *, relation_request, set_ref,
edge_refs=None)`で、採用済みcurrent setのimmutable manifest、edge/set packet、Governance
`require_review` receipt、packet coverage、独立run、set denominatorを再読します。N=node
review、E=edge receipt、S=set synthesis receiptを別々に満たすAND条件で、leaf receipt、個別edge
PASS、owner acceptance、fixture結果、adoption準備状態だけでは完了しません。公開RPCやUnit3
stage gateはこのconsumer部品では追加せず、生成APIの一覧にも内部helperは載せません。

Consumer-M/R修理では、各relation requestに固定したcenter・scope・ownerを全criterionへ適用します。必須IDはsealed scopeのmembershipと厳密なcontributor ownerから導出し、global・Task-local・各criterionで同じ集合を使います。不変expected-obligations本文に保存された義務recordが無い場合、現在scopeから再導出したaliasで補完しません。旧E2 IDは保存済みsource/pointer/value材料が一意に一致したときだけ現行IDへ対応付けます。`meaning_review`はrequestとedge endpointから得た必要node identityだけを評価し、無関係なN reviewを充足に使いません。set producerは実edge rowを`(id, revision, digest)`順に並べてからmanifest、partition、assignment、stream digestを作り、readerの厳密な順序検査を維持します。

Consumer-M/R 003 の center/owner selection は13 relation × incoming/outgoing の閉じたdispatch表で評価します。scope membershipだけでcenterを代用せず、artifact AC/source spanなどのleafは完全typed identity、containerは既存`contains` membership、assigned/produced/migratedは保存contributor、decomposesは保存親子link、Task exercise/outputは保存宣言、impactはchange inventoryへ照合します。Task projectionでは`owner_mapping`、必要node、mechanical contributor ANDを同じlocal Task owner集合へ投影し、global requestでは全contributorsを保持します。`realizes` incomingのparent/child混入、local Aへのglobal B contributor混入、foreign center/directionは共通規則で拒否またはmissingとして残し、対応済みrelationを一律unsupportedへ変換しません。

MR808のcandidate/Task centerは、scope rootへcandidateやTaskを追加して認定しません。内部
`resolve_center_owners`はsealed contextのcanonical Breakdown assignmentを選択scopeのexact
artifact anchorへ結び、実Assurance provenanceからcurrent candidate/candidate_symbolをexact
Task revisionへ正規化してから、scope admission・required output・contributor・projectionへ同じ
結果を渡します。実Runtime producer、foreign/unassigned/stale/forged centerは別の実materialで
検査し、read_artifactsやcaller supplied owner値を割当証拠にしません。C の v3 registry adapter は
受理済み M/R の owner・center・set 境界へ接続済みですが、実 N/E/S receipt、synthesis、stage gate、
installed/live/LLM acceptanceは明示的に後続範囲です。

P→M/R `produced_by` のartifact→Task edgeは、draft Knowledge artifactを受け入れ済みartifact
として扱わず、`task.artifacts_collect`が保存した完全な`artifact_production` materialだけで
検証します。materialのenvelope/payload/CAS、Task revision、candidateのrun/receipt/epoch、producer
actor、manifest declaration/output body、Task/candidate/observed/artifact dependenciesを同時に
照合し、missing・foreign producer・old candidate・別revisionは`unverified`へ残します。draftを
許すresolverはこのrelationのmaterial boundaryだけで、普通のartifact currentnessはaccepted-only
です。NはTask/declaration context、Eはedge receipt、Sはset synthesis/独立Runtime receiptを
別々に満たす必要があり、C profile-v3 の機械接続後も stage gate・installed/live・official archive・
real LLM acceptanceは後続範囲です。

## Unit4-O origin boundary

Program origin metadata is immutable schema16 history. Migration backfills real
legacy programs atomically, while every `program.begin` writes an
`e3-required` origin in the same transaction as the program and start event.
Use the private read-only resolver described in `references/operations.md` for
identity checks; it never supplies an allow/gate result. Current exports use
spec/planning-history v5/v2 and chunk/archive v12 with the origin section, while
full stage enforcement remains a later component.

For schema 16, current export, chunked baseline, operational backup, restore,
and GC first use the shared origin validator. A missing origin table or a
program/origin cardinality, identity, body, or digest mismatch raises a
structured Fault; a schema 16 database never silently downgrades to a legacy
output format. Restoring a genuinely older schema remains allowed so the
normal migration can create its origins. Malformed policy and origin values
are type-checked before enum membership and therefore cannot escape as a
Python type error. A pre-v16 database that already contains schema16 origin
metadata is rejected as an invalid mixed schema; migration backfill only
operates on a real old schema without that table.

## Unit4-P plan writer connection (finite)

The finite Unit4-P connection binds the existing read-only Unit3 `plan`
evaluator to the current immutable program origin and canonical profile head.
`unit4_enforcement.inspect_plan_gate` is the one shared reader used by
`Planning.phase_blockers`, `Breakdowns.audit`, and `LocalExecutions._audit`;
`advance`, `activate`, and `certify` call its mutation-boundary companion
again inside their existing transactions before updating or replaying a row.
The existing service gate and this plan proof are ANDed. A new
`program.begin` origin with no current canonical mandatory profile is blocked;
a genuinely migrated `legacy-preserved` program with no selected profile keeps
its historical route. A selected legacy profile still needs the same current
mandatory plan proof, and an invalid or disabled selection never falls back to
legacy.

The reader passes only the proposed/root breakdown ID to the common evaluator
and does not pass a local execution, so the Unit3 local primitive cannot
recurse through the writer. No candidate, future check, synthetic profile, or
caller-supplied allow value is created. Adoption/certification records retain
the origin, selection, stage/checkpoint, semantic fingerprint, proof snapshot,
and structured failures used at the boundary. Full ready/claim/execute/
complete enforcement, Delivery, Unit5, and the separate system-enforcement
capability remain later components.

## Unit4-R readonly Task admission (finite first unit)

The private `unit4_enforcement.inspect_task_admission` reader accepts only the
fixed `ready`, `claim`, `complete`, and `recheck` checkpoints. It resolves the
current Task revision, immutable program origin, canonical profile selection,
active Breakdown memberships, and the current local authorization/claim owner.
The optional `workflow_id` is a compatibility field and cannot create a writer
membership. Every canonical mandatory branch is evaluated through the shared
Unit3 Task evaluator and combined with the existing gate using AND; a failed,
withdrawn, foreign, or stale local choice remains visible as a structured
failure. An adopted root with a current source-backed disabled selection keeps
its historical route with `strong_complete=false`; an unadopted or new disabled
route is rejected.

The reader is transaction-bound and emits no gate, claim, candidate, CAS, or
other durable row. `Governance.evaluate_task` consumes its current projection,
and `Workflow.ready` and specified/automatic `Workflow.claim` run the matching
checkpoint before mutation. Runtime/queue execute enforcement, Delivery,
Unit5, and the separate system-enforcement capability remain later units.

## Consumer-P artifact production and Unit3 plan/Task

`task.artifacts_collect` は、current Task の sealed candidate、Runtime
provenance、宣言済み `required_outputs[*].realization_kind=artifact` だけから
draft Knowledge artifact と immutable `artifact_production` material を作ります。
material は Task revision、candidate の run/receipt/epoch、producer actor、manifest
declaration、output body、ordered typed dependencies を束ねます。draft status や
caller の producer fields は意味 review/acceptance の代替になりません。

v1 `produced_by` の Consumer-M は保存 dependency index からその artifact/Task の
material だけを選び、envelope、payload/CAS、candidate provenance、declaration、output
body、current artifact head を検査します。missing、foreign、old、ambiguous、changed
material は unresolved のままです。N の Task/declaration review、E の edge receipts、
S の set synthesis/independent receipts は個別に満たす必要があります。

Unit3 read-only evaluator は accepted M/R adapter を plan/Task で使い、Task の frozen
formal check を current candidate、observed receipt/run、saved verification material、
definition identity から独立に解決します。ready/claim/execute では candidate/check を
future として扱い、complete/recheck では全 check を要求します。integration/delivery
producer と Unit4 writer enforcement は後続範囲です。

Unit2/B checkpoint scheduling is a read-only controller boundary. A required
output whose saved producer owner is the current Task revision remains
`deferred_future` until the declared `complete`/`recheck` checkpoint; its
`artifact_refs` are source/responsibility inputs and do not prove that the new
artifact already exists. A fixed external artifact owner remains
`required_now`. The Task and global checkpoint projection factories accept only
the opaque plan produced by the sealed context/denominator/selected-request
classifier. They never seal caller-supplied population, partition, owner, or
schedule values. M/R recomputes the stage/checkpoint, relation, direction,
center, canonical population, owner refs, and classifier partition before
reading `required_now_ids`.

The private pre-adoption observation adapter is connected to the read-only Task
candidate evaluator in the finite Runtime boundary. It consumes only the
controller/actor-sealed identity after the durable run/receipt/CAS observation
and before the Task UPDATE/candidate INSERT; deferred future obligations remain
visible and `strong_complete` remains false. Stage rejection leaves the durable
observation available to a fresh Control. This does not claim full writer
enforcement, Delivery/integration producers, installed/live, ZIP, or LLM
acceptance.

`workflow_id` remains optional Task input. Private candidate admission derives
its target from the union of active Breakdown membership and the current
certified local proposal/claim membership. The local branch is reread through
the shared readonly authorization primitive at the finite `candidate`
checkpoint, so a selected local proposal can be evaluated before root
Breakdown adoption while stale/withdrawn authorization remains a concrete
failure. The evaluator applies the existing all-program AND to that complete
union; the workflow field cannot create a private membership or narrow it. A
missing membership or unselected profile retains the existing optional-stage
boundary, while Unit4 origin and all-writer enforcement remain later work.

### DOMAIN responsibility assurance: explicit version pair

`assurance.scope.v2` is selected by adding `format: "assurance.scope.v2"` to
`scope_propose`'s five historical fields. Its exact `assurance.obligations.v2`
pair retains all acceptance/population leaves and canonical/supplemental
responsibility Q records, complete enumeration, and the transitive pinned
artifact/source closure. An empty population is `[]`; it is never the v1
`empty_scope` placeholder. Q owners are their source artifact, including
structural `domain_reference` declarations; the referenced DOMAIN does not
replace the declaring owner.

`assurance.profile.v4` adds `realization_sources` only for plan/task
realizes/outgoing (design/component/interface). `assurance.profile.v5` retains
that vocabulary and registry v2 while requiring `required_scope_contract:
"assurance.scope.v2"`, `required_node_contract: "assurance.node-contract.v2"`
and the exact new pair. Old scope/profile/node grammars remain historical
contracts. Never use DOMAIN as a realizes source.

Node contract v2 adds `domain`/`accepted_domain`, with mandatory
`domain_responsibility`. Run the dedicated role against the current accepted
DOMAIN. Runtime retains an `assurance.domain-node-review.v1` packet in its
immutable prompt: full DOMAIN body, resolved sources, invariants, all Q,
dependency pins and exact sorted coverage. Cover every Q and the four boundary
field markers, even with zero Q; supplemental declarations add their own field
marker. Other role receipts cannot replace the dedicated review. Missing,
extra, duplicate, stale or foreign material is not current proof. Oversized
mandatory material blocks rather than being silently truncated. This node
review approves meaning; implementation needs separate implements E/S proof.

Migration retains old bytes and history. Save and classify/partition a real
transition source, independently review/adopt the v2 scope and obligations,
then propose/review/CAS-adopt v5 using nonempty source-backed authority and the
exact predecessor. Build new edges/sets and DOMAIN review under the new pair.
Old proof is not re-labelled. Reads do not migrate or manufacture evidence.

New contract history uses spec v7 / assurance-history v2 and snapshot/archive
v13 with an exact `required_contracts` list. Scope-only and DOMAIN-review-only
history also require the new format. Legacy down-export is refused. Portable
inspection verifies retained revisions and source/prompt/blob closure; history
is not fresh review authority. Full backup restoration validates the new
closure in its private candidate home before publishing that home.
