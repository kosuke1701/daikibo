# 会話からの操作例

以下のIDはAPIの実結果で置き換えます。JSONファイルの使用を推奨します。

```bash
daikibo connect --workspace "$PWD" --name "予約管理"
daikibo call native.input --json @input.json
daikibo call native.context --json '{"session":"返されたsession"}'
daikibo call api.describe --json '{"method":"artifact.propose"}'
daikibo call native.actions --json @actions.json
daikibo call job.submit --json @review-job.json
daikibo wait JOB_ID
```

input.json:
```json
{"session":"SESSION","content":"ユーザーの原文","turn_id":"この会話の一意なturn","origin":"skill-relay"}
```

actions.json:
```json
{"session":"SESSION","source":"SOURCE_ID","actions":[
 {"as":"requirement","method":"artifact.propose","params":{"project":"PROJECT_ID","kind":"requirement","body":{"title":"予約重複防止","statement":"同じ枠を重複確定しない","acceptance":["AC-RESERVE-1"],"source_refs":["SOURCE_ID"]}}},
 {"method":"artifact.get","params":{"artifact":{"$ref":"requirement.id"}}}
]}
```

review-job.json:
```json
{"kind":"review","args":{"subject":"TASK_ID","role":"spec","adapter":"claude-main"}}
```

受付結果のJOB_IDはreceiptではありません。job終了後のreceiptを正式APIへ渡します。
`adapter.qualify`もJobで実行し、fixtureではなく実CLIの結果を確認します。

長い原文はsource.partitionのnext_offsetを追い、source.packetへstart/end/expected_digestを渡します。
複数packetがあっても元sourceは一つの不変原文です。分類とレビューの完了条件は別です。

裁定はnative.present_decision→ユーザー原文→native.respond→実consistency review→decision.apply。
`native.acknowledge`には通知ID、後続source、正確な引用を渡します。product/conflictは汎用ackでは解消できません。

CLI引数が変わった場合は実装のapi.describeを優先します。同じユーザー内の協調APIであり、別の認証・管理者承認端末は存在しません。

## 固定test契約

正式checkはsealed candidateのcopyで実行します。保存されたsource、input、期待値、result、index、shardsは読取専用の証拠として扱い、verify中に再生成・修復・上書きしません。record mode（実装時）はこれらを生成できますが、verify modeはcandidate外のscratchでfresh計算し、保存値と比較して、成功・失敗の双方で保存inputのbytesを保ちます。check固有の収集例外は宣言したpytest/JUnitのreportまたはoriginal_reportだけで、commandにはreport例外がありません。既存のEXCLUDED_DIRSとbuild_inputs/build_outputs契約を維持し、除外へ科学的証拠を隠しません。例: 実装時にexpected.jsonをrecordし、verify時に外部scratchのfresh resultをexpected.jsonと比較します。exit 0やJUnit全case passでもinput_mutatedなら正式checkはfailです。

Formal checks materialize the sealed source snapshot into a fresh work directory; source scanning excludes repository .git files/directories, so the implementation checkout's Git metadata is not automatically available. A git command may fail or discover an ancestor repository; neither result establishes the candidate's content identity. Use the supplied snapshot/content manifests and declared inputs for verification, and keep recorded Git provenance distinct from freshly observed environment metadata. If a check needs Git behavior, declare and construct an explicit isolated fixture in permitted scratch rather than relying on the enclosing checkout's repository.

## system.backup artifact contract

`system.backup` keeps its result shape (`id`, `path`, `blob`, `sha256`, and
`bytes`) and the existing owner/private authorization rules. A new completed
ZIP is `ZIP_STORED` and its result digest is resolved by the private
`backup-recipes/<sha256>.json` flat recipe. Recipe segments reference only
immutable `blobs/<prefix>/<suffix>` leaves; they never reference another
recipe, and the recipe is published create-only after its exact ZIP bytes and
leaf hashes validate. A failed publication leaves the export and prior
recipes untouched.

`blob_read` uses the same bounded range response for physical leaves and
recipe-backed backup artifacts. `blob_path` is a physical-leaf interface and
raises a controlled error for a recipe alias; consumers that read or export
blob bytes use the bounded artifact reader. An existing physical path is
validated first, so a corrupt or symlinked leaf never silently falls back to a
recipe. `system.restore` validates the recipe closure before publishing the
restored home and keeps legacy compressed backups readable. Retention and
chunked-history consumers preserve their prior hash/size/read behavior through
the same artifact reader.
Authorized size and range calls share one Store-owned verified session. It
binds the actual recipe bytes, ordered reassembly hash, and opened physical
leaf identities; authorization runs before session lookup. Namespace changes
evict cached sessions, while unsupported monitoring falls back to complete
validation per request. The 256 GiB limit covers expanded payload files;
manifest bytes and observed ZIP framing are bounded separately.

All committed recipes and their physical dependencies are validated before
garbage collection begins; an invalid recipe makes both dry-run and deletion
fail before any unlink. Backup and GC share the existing store lock, so staged
leaves cannot be collected while a backup is freezing or publishing them.


## dev3: 実行回復と履歴

`job.submit` のoptional `retry`は `{max_attempts,base_delay_seconds,max_delay_seconds,max_elapsed_seconds}`。
reviewとsupervisor.turn以外はmax_attempts=1のみ。defaultは最大3回、基本10秒、最大待機300秒、期限1800秒。
providerが示した待機が上限を超えると、早めて再試行せず停止する。
全試行はjob.getのattemptsへ残り、retry_waitはterminalではない。
版が変われば自動再試行せず、再確認後にjob.retry（read-onlyのみ）で新しいJobを発行する。
実装途中のpartial_work.snapshot_blob/changes_blobは未採用の資料。通常の作業成果を読むときは `run.work_changes(run, offset, limit)` で固定runの変更一覧をページ取得し、返されたsnapshot/receiptと各entryの `repo`/`path` を選ぶ。新規・変更entryで `after.kind == "file"` のときだけ `after.blob` を `expected_digest` として、同じrunへ `run.work_read(run, repo, path, expected_digest, offset, limit)` を呼ぶ。`after` が `null` の削除entryには読み取るfileがないので `work_read` の対象にしない。changes entryの `after.blob` と `work_read` 返答の `sha256`（実際に返されたfile bytesのSHA）を同じ名前のdigestだと混同しない。ページごとにrun、receipt、repo、path、SHA、total_bytes、next_offsetを照合し、全ページをbase64 decodeして保存したbytesのSHA/sizeを再計算する。base64はtransport専用であり、本文をconsoleへ表示・echoしない。`work_*` はproject-scoped readで、project認可済みactorのrunだけを読み、candidate採用、成功判定、Task完了、再実行を行わず、古いPASSや現在のrepo名でentryを補完しない。

collector失敗で通常のwork productが作られず、FailureRetentionが保存したmanifestを調べる場合だけ `run.recovery(run, offset, limit, expected_digest)` と `run.recovery_read(run, repo, path, expected_digest, offset, limit, expected_manifest)` を使う。これは `work_*` と別の failed-artifacts manifest 契約であり、manifest digest、entry SHA、regular-file kind、ページ境界を固定して読む。`recovery_*` はtask-scoped readで、保存manifestのTask scopeを照合し、別Taskのrunへ広げない。どちらのread familyも read-only で、読み取っても採用・承認・成功・再実行は起きない。指定run、entry、SHA、pageを保存してbyte exactに復旧し、owner権限へすり替えず、対象project/taskの既存actorで認可を受ける。旧手順にあるowner昇格、raw base64出力、current path推測は履歴として分離し、現行手順に再利用しない。

execution.configure_limitsはnative.actionsから実行可能。max_invocations、max_reported_tokens、max_estimated_cost_usdは任意で未指定なら上限なし。
原則ユーザー/プロジェクトの既存予算方針に従い、単に実行を続けたい理由で明示予算を勝手に緩めない。
CLI推定額は請求額ではない。利用量不明に対する停止を金額ゼロへの修正で回避しない。

baseline.list/get/verify/export、artifact.historyは履歴照会。baseline.rebuild_gitはexpected_snapshotとreasonを要求する。
inspect-baselineは元DB・サービスなしで使える。全運転の復元は従来のsystem.backup/restoreを使う。

## dev4: 作業分割と複合完了

`breakdown.propose(program,title,rationale,units,expected_active=None,byte_budget=24000)`。
unitはid/title/parent/domain/rationale/obligations/tasks/interfaces/dependenciesを持ち、
obligationsは `{requirement,acceptance}`、dependencyは `{unit,interface}`。
親はdomain=null、obligations/tasks/interfaces/dependenciesは空。葉にはaccepted domainと具体Task。
全accepted要件のAC（親も含む）・全非取消Taskを一度ずつ割当。別要件の同名ACは別の義務。

`breakdown.get(breakdown,offset,limit)`のnext_offsetを追う。`breakdown.packet(packet)`で内容取得。
各packet IDをsubjectにreview Jobをdesign/traceそれぞれ起動する。receiptとrequired_coverageが揃うまで
activateできない。byte_budgetはUTF-8実byte数で4096..100000。意味の読めない断片はblockedとし、関連断片を読む。
旧plan改訂はexpected_active=現在IDで提案し、同じIDをactivateへ渡す。Task実行の進行だけではstaleにしない。

`program.status`は履歴、`program.completion(program,delivery)`は現在の全体再照合。
`program.finish(program,expected_revision,delivery,review_receipt)`が正式close。
`program.reopen(program,expected_revision,target_phase,reason,cause,review_receipt)`で戻す。
causeは同projectのsource/change/finding等、Agentのreopenには実impact reviewが必要。
whole-change実検証とdelivery.certifyの前にfinishしない。validationは正式最終完成不可。

同じ内容のsource.readを繰り返しても自律処理の進捗にならない。必要な別rangeを明示する。
新しい分類・link・他Agentの結果は次の計画turnのきっかけになるが、意味上の判断はAgent自身が行う。


## dev6: exact受入条件と定義変更

Taskのacceptanceは表示ラベル、acceptance_refsは `{requirement,acceptance}` の配列です。
要件はread_artifactsに含め、当該要件とTaskの双方にある条件を指定します。
同じラベルを複数の要件が持つ場合は必須です。単に関連要件を読んだだけでは実装担当に数えません。
既存の曖昧Taskを自動で複数要件へ割り当てることはありません。
明示参照を使うレビューContextにはacceptance_identityがあり、acceptanceに必要なmarkerも含みます。
必須markerを列挙したことと、条件の意味を審査したことを混同しないでください。

`task.propose_revision(task,expected_revision,body,reason)`は未適用提案を保存します。
変更前後、入力全版、依存、後続Task状態、Policyを固定するため、対象資料が変われば再提案です。
`job.submit`のkind=review, subject=提案ID, role=impactで別の実Reviewerを起動します。
最新reviewのreceipt、proposal ID、expected_digestを `task.apply_revision`へ渡します。
指摘・不足・古いPASS・提案違い・未実行では採用しません。owner経由も新定義変更のreviewは必要です。
Task本文を変えず凍結済みtest planの定義だけを訂正する場合は、`task.propose_plan_revision(task,expected_revision,expected_plan_digest,body,reason,evidence_refs)`を使います。`body`は`task.plan_tests`と同じ完全plan、`evidence_refs`は現在Taskに紐づく実receipt IDです。旧FAILのreceiptも欠落や意味の隠蔽なしに固定できます。impact review後の`task.apply_revision`はTask revision/epoch、plan pin、after履歴を同じtransactionで保存し、Task本文・旧candidate・attempts・counterを保持します。結果の`new_test_plan_required=false`は新しいplanが実行済み・承認済みという意味ではなく、通常の再実行と別レビューが続きます。
Taskはplannedへ戻り、旧planとcandidateは履歴へ残ります。新test planと実行・レビューが必要です。
試行/利用量の累積は維持し、無関係なTask・未解決の製品判断を消しません。
提案materialは700000 UTF-8 bytes以下です。上限を超えたらTask分割と正式な計画変更を行い、資料を省略しないでください。

未完了提案はtask.revision_list(project,task=null,status="proposed")で再開できます。
`task.revision_get`は正確なmaterial、`task.revision_history`は版付きページの目録です。
`task.history_record(history,expected_digest)`で前後内容を照合します。
撤回はtask.withdraw_revision(proposal,expected_digest,reason)。適用済みは消さず、次の提案で変更します。
定義を変えない参照版更新だけは従来のtask.replanを使い、同じ前後履歴へ保存します。

baseline.createは履歴があればv3 chunk形式を選び、過去のv2もread/verify/exportできます。
新記録があるのにlegacy形式を指定した場合は拒否します。履歴を捨てて旧形式へ合わせません。


### dev7: 委譲された担当範囲

workstreamはrootの採用breakdownの葉unitを参照する担当範囲で、独立releaseではありません。
proposeのunit_idsを元に正本のTask・要件/ACを全て含め、兄弟の重複は拒否します。
parentで階層化できますが、親のsubset以外は渡せません。境界の外部依存を明示します。
getのpacket一覧はoffset/limit、selectionの種類はunits/tasks/obligations/external_dependenciesです。
実design/trace runのrequired_coverageを満たしてactivateします。fixtureは実LLMと同等に扱いません。
finishは作業/依存/子の現時点の証拠を再検査するだけでdeploy_ready=falseです。
native.completionもcompletion_kind=delegated_work_onlyと報告します。全体はrootのGateへ戻します。
担当の差替えはpreviousを付けた新提案、返却は同じreasonへの実impactレビューのreceiptでwithdrawします。
先に子を明示的に返却し、親の置き換えで子やTaskを自動取消しないようにします。
workstream履歴を含むarchiveはv4になり、v2/v3の読取も維持します。
SQLite schema9へ追加移行します。古い実行版で新DBを開かず、先に完全backupを保持してください。

## 大きな担当範囲の返却・再計画

- 通常は `workstream.return_propose(scope,reason,byte_budget)` を使います。理由・現在入力・親/rootを固定した未適用提案で、Task/要件はそのままです。
- `workstream.return_get` の全leafページを読み、各IDへ `job.submit(kind=review,args={subject:...,role:impact,adapter:...})` を要求します。各結果には実行記録とexact required_coverage、具体的なrationale/observationsが必要です。
- `workstream.return_advance` で未完了ノードと次段の集約ノードを取得します。次段には子の結果全文が渡されます。子がPASSという理由だけで合格にせず、境界・返却先・残存責任を判断します。判断できなければblockedです。
- 全段の審査後、ready=trueのroot_packet/root_review_receiptと元proposal digestで `workstream.return_apply` を要求します。断片だけの完了や古い集約は代用しません。
- 最新の下位結果が変われば集約も新しく必要です。新旧履歴を消さず、無制限なpass探しをしません。上位要件が変わった場合は再提案へ戻します。
- 一つの結果が大きすぎる場合、システムは切り捨てず停止します。情報を隠さず明確な判断へ再整理するか、許された範囲で大きいbudgetの別提案を作ります。
- セッション喪失後はreturn_list/get/advance、読むだけの過去資料はreturn_packet(historical=true)。過去資料の表示は新しいレビュー実施ではありません。
- 取消ではなく提案を捨てる場合はreturn_abandon。割当・Task・要件はそのままです。子が有効なら先に子の扱いを解決します。


## 標準Schemaの実データ検査（dev11）

- `contract.schema_capabilities`で対応/非対応を確認し、`contract.check_schema`と`contract.check_instance`へ同じprojectの原文DOC/hashと選択位置を渡す。
- status=`unsupported`/`invalid_schema`/`limit_exceeded`等のvalid=nullを合格にも正常な否定例にも数えない。元の規則を削除して通さない。
- 診断はrun/receipt/reviewを作らない。実Taskの検証では`python -m daikibo.schema_cli`を既存のkind=junitコマンドとして計画し、管理Runtime、test adequacy、spec/quality/統合Gateを通す。
- API原文のinventory/Schemaの1サンプル成功は、HTTP/認証/consumer互換性や全規格検証ではない。必要な正式検証と意味判断を残す。

## dev18 execution controls

Dev18 adds schema-13 execution-control records while retaining every older
attempt and receipt. The old `tasks.attempts` value is cumulative telemetry;
admission uses the independently reviewed `tasks.no_progress_count` and the
fixed threshold `3`. Only one conclusive assessment can be finalized for an
observed attempt. `inconclusive` remains a receipt/proposal result and consumes
no slot, adds no count, and grants no permission.

The bounded calls are:

```text
execution_control.inventory(task, offset=0, limit=100, expected_snapshot=null)
execution_control.get(proposal)
execution_control.propose(task, expected_revision, body)
execution_control.packet(proposal, offset=0, limit=1, expected_snapshot=null)
execution_control.apply(proposal, expected_digest, review_receipt)
execution_control.withdraw(proposal, expected_digest, reason)
execution_control.history(task, offset=0, limit=100, expected_snapshot=null)
execution_control.history_detail(task, attempt_epoch, kind, offset=0, limit=16, expected_snapshot=null)
task.progress(task)
project.progress(project, offset=0, limit=100, expected_snapshot=null)
```

The exact proposal body is:

```json
{
  "target_attempt_epoch": 1,
  "target_attempt_ordinal": 1,
  "target_implementer_run": "RUN-ID",
  "control_type": "assessment|recovery|timeout",
  "requested_seconds": null,
  "old_effective_seconds": null,
  "cause_analysis": "Observed cause and bounded reason.",
  "experiment_estimate": {"seconds": 14400},
  "evidence": ["EVIDENCE-ID"],
  "intended_next_action": "One ordinary reviewed next action.",
  "scope": {"task": "TASK-ID"},
  "recovery_action": null
}
```

`requested_seconds` is required and finite-positive only for `timeout`; it is
null for `assessment` and `recovery`. `recovery_action` is required only for
`recovery`. Evidence references are canonical IDs with their stored digest and
project checked by the controller. `target_attempt_ordinal` and historical
revision/binding may be null only when retained legacy evidence does not prove
them; callers must not infer a rank. A legacy target with several retained
runs must select its exact `target_implementer_run`.

The default task timeout is `14400` seconds. Independently reviewed finite
timeouts can exceed four hours or 24 hours when the cause, retained observed
run, and semantic material justify them; there is no arbitrary 24-hour cap.
`check` operations remain separately defaulted to `300` seconds. The
`old_effective_seconds` value is checked against the canonical timeout in the
observed target run. A timeout authorization applies only to a subsequent
execution with the same semantic Task revision, inputs, dependencies, policy,
accepted constraints/invariants, and baseline; lease/epoch/candidate changes
made by that control itself are not self-invalidating.

`execution_control.apply` first calls the ordinary observed-review gate for
the latest `execution_control` receipt. A receipt with a nonzero exit, timeout,
cancel, output overflow, input mutation, invalid review schema, unresolved
findings, or unqualified governed execution cannot create an authorization or
increment `no_progress_count`. Its typed markers are still interpreted only
after that gate. Assessment, timeout, and recovery decisions remain
independent: a timeout approval does not approve recovery, and recovery
approval does not approve a longer timeout. A conclusive `no_progress` marker
is retained even if the separate recovery/timeout marker is still
`inconclusive`.

Execution-control disposition resolutions use the exact typed vocabulary:
`attempt:<epoch>` accepts `progress`, `no_progress`, or `inconclusive`, while
`recovery:<proposal>` and `timeout:<proposal>` accept `approved`, `rejected`,
or `inconclusive`. `approve` and other aliases are rejected without
normalization. Raw output from an invalid receipt remains retained as evidence,
but it cannot authorize an apply or alter a counter; other review roles retain
the general disposition contract.

Across review roles, `pass` requires `findings=[]`; an unresolved finding
requires `fail` or `blocked`. Findings are unresolved problems in the reviewed
subject or decision, while observations are evidence-bearing facts with exact
references. For `execution_control`, observations may include existing
implementation, quality, or test defects; place a defect in findings only when
the evidence explains why it invalidates the assessment or requested
authorization. Do not mechanically move defects or force a progress conclusion.

Recovery is a reviewed admission decision for the latest failed, unknown, or
claim-only durable claim/run/lease evidence. Claim-only recovery has null
duration and null assessment and must never be classified as progress or
`no_progress`. Recovery does not enqueue or retry work and does not bypass
normal currentness, dependency, resource, test, review, or completion gates.
A normal replan preserves an unresolved `run_unknown` blocker. After a valid
recovery is followed by a new claim, that claim records the consumed
authorization ID; only that matching operational blocker is cleared.

Policy v2 uses the existing immutable `decisions` table. Its proposal carries
the exact raw human source `{id,digest}`, accepted requirement
`{id,revision,digest}`, current policy `{revision,digest}`, exact pending
policy decisions to supersede, and a reason. The route verifies the retained
observed requirements acceptance review and records the instruction with
`response: null`. `execution_control.policy_apply` then requires a distinct
observed `consistency` review over the exact source text, accepted requirement,
old/new policy diff, and superseded records. Lexical source agreement is not
approval. Previous pending decisions and any retired legacy attempt blocks are
retained in the adoption event.

These calls expose durable history and gate decisions. They do not claim a
live migration, final wheel installation, installed observer result, or
cutover until those are independently executed and recorded.

## dev19 read-only progress reporting

The existing `task.progress(task)` and
`project.progress(project, offset=0, limit=100, expected_snapshot=null)`
response fields remain unchanged. Each adds the following compact projection:

```json
{
  "format": "daikibo.task-progress.v1",
  "context": {
    "task_revision": 1, "task_epoch": 1, "task_status": "submitted",
    "validity": "current", "paused": false, "project_paused": false,
    "admission_scope": "prior_epochs_of_current_task_epoch",
    "admission_is_next_claim_authorization": false
  },
  "current_claim": null,
  "latest_attempt": null,
  "next_claim": {
    "state": "reassess_current_result",
    "reference_attempt_epoch": null,
    "projected_authorization": false,
    "explanation_code": "submitted_result_before_next_claim"
  },
  "snapshot": "SHA-256"
}
```

`current_claim` uses only durable evidence for the exact current epoch and
identifies whether it came from a claim record, retained event, or retained
run. `latest_attempt` independently selects the maximum retained actual epoch,
including the current epoch. It reports implementer run/receipt IDs and
outcome, one latest validated review per required role and actual binding,
and a finalized `progress`/`no_progress` assessment when present. Missing or
invalid evidence is explicit (`null`, `unknown`, `pending_or_incomplete`,
`execution_failed`, or `invalid_or_unknown`); old ordinal/revision values and
multiple runs are never guessed. Existing receipt verification and
`Governance.require_review` determine review observations. Receipts from
different candidate/input bindings cannot be combined.

`next_claim` explains lifecycle handling and never authorizes a claim, retry,
recovery, timeout, completion, or scheduler action. A running nonterminal
claim reports `wait_for_current_execution`; submitted reports
`reassess_current_result`; ready and planned report their existing gate work;
completed and cancelled tasks report `not_applicable`. Paused tasks preserve
their lifecycle-derived state and add a paused explanation. A successful
implementation remains separate from completion. The reporting read path is
additive and does not write policy, receipt, assessment, archive, schema, or
counter records.

The task reporting snapshot excludes its own `snapshot` field. A project page
digest covers every projected item and its reporting/evidence identities, so
a new review receipt invalidates `expected_snapshot` even if the `tasks` row
is unchanged. Repeated unchanged reads are stable. Use `execution_control.history`
and the existing evidence readers for unbounded detail; reporting does not
alter admission's strict prior-epoch selector or its preserved payload. A
project page computes a compact global evidence stamp and performs detailed
projection only for the requested page; claim events are indexed once per
project request. `run_ids` and `receipt_ids` are bounded and carry exact
`run_total`/`receipt_total`, truncation flags, and a detail pointer when
needed. A no-profile or dependency baseline read uses a digest-equivalent
non-storing capture/assembly path; normal execution paths keep the default
blob-storing behavior.

When a progress record contains a `run_detail` or `receipt_detail` pointer,
call its `execution_control.history_detail` route through the public dispatcher.
The accepted `kind` is `implementer_runs` or `implementer_receipts`; the reader
returns at most the requested bounded page, an exact `total`, `next_offset`, and
a detail snapshot. Pass that snapshot with the next page request. It reads
retained raw identities even when the legacy epoch is ambiguous, so it never
selects an implementation, creates an attempt, or changes admission. A changed
retained record rejects an old page with `stale_history_detail`.

Execution-control `propose`, `apply`, and `withdraw` resolve their saved target
before checking the caller's project and exact Task capability. Native sessions
remain project-scoped and do not elevate to owner; Task-scoped agents must
match the target Task. Replays repeat the same scope check before returning the
recorded result, while existing role, currentness, independent-review, and
digest checks remain in force. Recovery review instructions require only the
recovery marker for claim-only evidence and both independent recovery and
attempt markers when an observed implementer run exists.

## Claim admission diagnostics

`task.parallel_candidates(project, limit=100, offset=0)` is the read-only
scheduling view for a coordinating Agent. It reports current project capacity,
claimability blockers, unmet dependency IDs, conflicts with running Tasks, and
pairwise conflicts among Tasks on the returned page. Follow `next_offset` for
additional candidates; `candidate_conflicts` is deliberately page-scoped. The
view neither claims nor reserves a Task, so every selected Task must still pass
the normal `task.claim` transaction against current state.

`task.claim(project, task=null)` preserves the existing admission, readiness,
dependency, conflict, lease, and counter semantics. If no candidate can be
claimed, its existing `no_work` fault may include a list-shaped `details`
value. Each diagnostic entry is scoped to an already-authorized candidate and
has this shape:

```json
{"task":"TASK_ID","stage":"execution_admission","failures":["recovery_required:9"]}
```

The `stage` identifies the existing check that skipped the candidate. Stable
stages include `task_state`, `execution_admission`, `ready_gate`,
`local_authorization`, `dependency`, and `resource_conflict`; the failure
values are the canonical codes returned by that check. Existing local
`task`/`failures` fields remain present, with the stage added. A detail proves
only that the current claim attempt observed a reason; it grants no claim,
recovery, replan, counter, or execution authority.

Automatic selection reports only the up-to-100 ready/current candidates the
existing scheduler examined. It is bounded diagnostic evidence rather than a
complete project inventory. A successful claim returns the normal Task row
and does not carry diagnostics from candidates that were skipped. Early
errors and genuine admission exceptions retain their established error codes.

## Traceability Unit A examples

For a code population, first register the repository and resolve a complete
commit OID from the fixture repository. Submit a bounded proposal and extract
it from that pinned commit:

```json
{"method":"traceability.propose","params":{"project":"PROJ","kind":"code","adapter":"python-ast-v1","scope":{"repository":"REPO","commit":"40-OR-64-HEX-OID","roots":["src"],"include":["*.py"]}}}
{"method":"traceability.extract","params":{"proposal":"TPROP-ID","expected_digest":"PROPOSAL-DIGEST"}}
```

The public extract call returns a durable `job` record. Poll `job.get` for the
terminal result; an interrupted worker leaves staging checkpoints and CAS pins
for a later extraction request to resume.

Read the resulting population with `traceability.list`, then page exact items
with `traceability.items(revision,limit=100,cursor=...)`; use its opaque
`next_cursor` only while the returned snapshot remains current. Use
`traceability.read` for complete bytes. The source is the pinned Git object and
CAS closure, so a later branch or working-tree change cannot substitute for the
selected commit. Unsupported files, parse errors, invalid UTF-8, and symlinks
remain explicit `unknown` items. Empty files are explicit known zero-byte
targets and do not invent a zero-length interval atom.

For a document population, register the immutable source blob and propose it
with `kind=document` (or `scope.source`), then extract and page the physical
lines. Each line retains raw BOM/CRLF bytes plus Unicode and byte coordinates:

```json
{"method":"traceability.propose","params":{"project":"PROJ","kind":"document","adapter":"utf8-lines-v1","scope":{"source":"SRC-ID"}}}
{"method":"traceability.extract","params":{"proposal":"TPROP-ID","expected_digest":"PROPOSAL-DIGEST"}}
```

This extract is also queued as a durable job and retains staging pins on an
abrupt worker stop. Source Unicode offsets follow the registered
`Knowledge.source` text coordinate, including a leading U+FEFF; raw byte
offsets and BOM/CRLF bytes remain available for exact reconstruction.

Unit A leaves revisions `ready` and historical. `traceability.coverage` reports
mechanical population facts while planning coverage and execution closure stay
false. `traceability.adopt` and mapping adoption require Unit B's actual review
and CAS gate; an archive inspection or historical review ID is never fresh
acceptance evidence. `traceability.export` and `traceability.import` preserve
historical/failed staging rows and their CAS pins.

## Edge assurance E2

Use the bounded proposal flow below. Each reference is an exact typed object;
do not replace a pinned plan/check, source span, population item, or delivery
member with an ID or a display label.

```json
{"method":"assurance.scope_propose","params":{"project":"PROJ","body":{"roots":[{"kind":"artifact","project":"PROJ","artifact":"REQ","revision":1,"body_digest":"..."}],"selection_rules":{},"exclusion_proposals":[],"authority_refs":[],"discovery_unknowns":[]}}}
{"method":"assurance.profile_propose","params":{"project":"PROJ","program":null,"body":{"scope_ref":{"kind":"assurance_object","project":"PROJ","object":"SCOPE","object_kind":"scope","object_digest":"..."},"stage_rules":{"plan":{}},"relation_selectors":["realizes"],"test_definition_bindings":[]}}}
{"method":"assurance.edge_propose","params":{"project":"PROJ","body":{"source_ref":{},"target_ref":{},"relation":"realizes","scope_ref":{},"claim":"...","obligation_ids":[],"required_evidence_refs":[],"authority_refs":[]}}}
{"method":"assurance.set_propose","params":{"project":"PROJ","body":{"center_ref":{},"relation":"realizes","direction":"outgoing","scope_ref":{},"criteria":{},"required_evidence_refs":[]}}}
```

`scope_propose` derives the independent obligation denominator. `set_propose`
derives every current edge matching center, relation, direction, and scope; a
caller supplied subset is not accepted. It stores a sorted full manifest,
coverage assignment, and partition manifest. Every packet has at most 500
leaf markers. A relation-set receives trace packets for individual edges and
an additional impact packet for the set synthesis, so edge PASS does not imply
set PASS.

Read every packet with `assurance.review_subject(project,subject,cursor,limit)`.
Adopt with `assurance.adopt` only after actual governed review receipts name the
exact packet ID, packet digest, role, and all required coverage markers. The
adoption gate rechecks immutable dependencies, currentness, manifest closure,
CAS identity, and the compare-and-swap head. Owner acceptance, an archive
inspection, or a fixture subprocess is not an assurance review receipt.
`assurance.report` returns bounded status categories and continuation cursors;
`assurance.resolve` returns pinned identity without currentness when no context
is supplied, and checks the selected adopted profile when a stage context is
provided. Runtime packet reviews use the existing governed review path.

## Delivery typed identity

`delivery_snapshot` and `delivery_check` are resolved from controller-owned Delivery material. A snapshot pin validates the authoritative Delivery binding, the sealed snapshot digest, every source file CAS leaf, and the exact check body. A check is a member only when its complete pinned snapshot reference and check digest match.

After `Delivery.commit` records a repository result, the controller may pin an `actual_delivery_commit`. Its material retains the exact object format, commit, tree, ref, canonical Git object closure, and the Delivery snapshot dependency. Resolution verifies Git object hashes and the complete tree against the snapshot; an OID-shaped string, branch name, or same-path file is not sufficient. `assurance.resolve` currentness rechecks the live Delivery, while `assurance.resolve_pinned` keeps retained historical material readable. Missing CAS, cross-project refs, changed snapshots, wrong commit/tree, and nonmember checks remain unresolved or stale. Fixture Git/subprocess checks are implementation evidence and do not certify live delivery or LLM review.

The commit path persists every repository's observed `body.git` and outbox
result before it captures verification material. Actual commit pins are then
completed through the existing producer in separate idempotent transactions; a
retry reuses valid saved material for the same observed OID and returns refs for
all repositories. A retry may capture a fresh Delivery snapshot observation;
that observation does not repeat Git or actual-commit pin production. Delivered
mapping observations commit before the final
delivery gate, so a pending capture or rejected gate retains the Git, outbox,
material, and mapping evidence for reconciliation. A pending or verified result
is not a delivered result, and local Git fixture checks remain separate from
live CLI or LLM qualification.

The public `Control.request` wrapper keeps ordinary mutating requests atomic.
`delivery.commit` has a bounded request lifecycle because its external local
Git observation must commit before later capture and gate work: the controller
lock serializes the complete request, stores an internal pending marker, and
replaces it with the normal idempotency result only after final delivery. An
exact retry resumes the same request after restart; a changed payload under the
same request ID remains an idempotency conflict.

## Managed output contract

The Runtime collector records `daikibo.output-capture.v1` for every new execution with the existing per-stream policy limit, actual bytes returned by `os.read`, raw bytes retained before decode/redaction, and a typed `truncated` flag for stdout and stderr. It does not estimate unread bytes after a kill, change the limit, or infer telemetry from worker JSON. An output overflow remains a failed receipt with a bounded diagnostic containing only the safe limit, stream name, and counts; cancellation and timeout precedence remains unchanged. The same collector-owned capture object is exposed through receipt and run status, while legacy receipts remain unchanged and missing telemetry remains unknown.

Managed implementers/reviewers should redirect exploratory stdout/stderr to separate files in authorized owned scratch, retain the original exit code, and keep console output byte-bounded. Formal CLI machine output and parser schemas remain unchanged. Scratch/home/tmp paths and hashes are not durable evidence; required failure evidence must use an existing authorized durable route and be retained until existence and SHA are checked. Automatic owned-log persistence is a later bounded component, and general home/tmp collection or an unlimited spool is not implied.


## Consumer-C profile.v3 and declared Delivery denominator

The finite Consumer-C extension uses `assurance.profile.v3`, whose closed body is
the v2 profile plus the exact `required_relation_contract_digest` value
`REGISTRY_V2_DIGEST`. v2 and v3 share one `profile:program:<program>` CAS head;
v1/v2 history remains readable and a proposal/adoption does not enable the E3
stage evaluator or completion gate.

For v3, the delivery denominator is read-only controller material. It resolves
the pinned Delivery snapshot's exact `build_definitions` and the unique pinned
check whose `produces` contains each definition. Missing, foreign, duplicate, or
ambiguous declarations stay unresolved/invalid; they are never converted into an
empty successful denominator. The `delivery_declared_output` obligations retain
the exact definition and snapshot pointer even when no output has been observed.
The read-only Consumer-C matcher can consume that same denominator through a
live or standard-archive resolver. It checks the exact producer receipt, six
output fields, output CAS, snapshot/check identity, and
`delivery_build_output` membership. A real producer failure, missing
observation, missing material, or historical pin remains `failed`, `missing`,
`unverified`, or `stale`; it never shrinks the denominator. Task
`required_output` and Delivery `delivery_declared_output` keep separate owners
and categories. The v3 relation adapter is connected to the accepted M/R
owner, center, direction, and set boundaries. A mechanical match still reports
`semantic_status=unverified` until actual E/S receipts and synthesis are
bound; for Delivery-only typed refs the sealed N population is zero and no N
receipt is fabricated. The finite one-output local and snapshot-global
two-output `produced_by`/`contains` fixtures pass actual E/S Runtime receipts
and synthesis adoption, while all thirteen relations, Unit3/stage gates,
installed/live, official ZIP, and real LLM acceptance remain follow-on scope.

Producer status is classified only after the shared observed-result resolver
checks the receipt/run project, subject, role, epoch, binding, snapshot, and
result together with the execution-material check, Delivery subject, and full
CAS closure. Foreign, malformed, or material-missing observations remain
`unverified`; a genuine exit-1 observation bound to the same check remains
`failed`. A failed producer is not required to have success output material,
and only an absent observation is `missing`.

For a v2 Consumer-C edge, every currentness, adoption, set, and replay reader
uses the stored `relation_contract_digest`; an unknown digest is rejected and
only an omitted digest selects the retained v1 registry. A `produced_by` or
`contains` set derives its immutable `delivery_declared_output` obligations
from the controller-pinned Delivery declaration material and stores the exact
`consumer_binding`. Coverage is resolved from that same snapshot, declaration,
and producer/check identity, so an empty edge claim list cannot self-assert a
PASS. Existing v1 obligation, edge, and set history remains byte/digest stable.
Delivery typed refs are execution material rather than Unit2b artifact/Task
nodes, so this C boundary treats an empty N population explicitly while real
E/S receipts, meaning/synthesis adoption, and Unit3/stage gates remain required
follow-on conditions.

## E3 Consumer-M/R

The denominator consumer uses the sealed internal `build_relation_request(...)` boundary. It binds
the global denominator digest, all required obligations and contributor owners, the current center and
scope, and an optional Task projection. A projection is a local view of the global population; callers
cannot supply a reduced obligation set or replace an owner. JSON copies, stale endpoints, nulls, unknown
keys, and cross-project references are not authority.

`evaluate_criteria(..., relation_request=request, relation_reviews=reviews)` checks the eight additive
categories against canonical endpoint/version/pointer/CAS membership. Mechanical eligibility alone does
not satisfy a relation. Missing artifact-to-Task producer material remains `unverified`, and the old
four-category compatibility path never promotes a raw edge claim into a new-category PASS.

`build_review_assurance(...)` reads the adopted current set's immutable edge manifest, edge/set packets,
Governance `require_review` receipts, packet coverage, independent runs, and set denominator. Node,
edge, and synthesis evidence are separate AND requirements. A leaf receipt, an edge PASS, owner
acceptance, fixture output, or adoption preparation cannot stand in for semantic set review. There is
intentionally no new public RPC route or generated API entry for these internal helpers; Unit3 gates and
the C output producer remain later components.

The Consumer-M/R repair applies the sealed request's center, scope, and owner selection to every
criterion. Required IDs are derived from strict scope membership and exact contributor owners, then
reused for global, Task-local, and criterion results. An immutable expected-obligations object must
contain the saved obligation record; a current scope re-derivation cannot fill a missing record.
Historical E2 IDs require unique saved source/pointer/value material matching. `meaning_review` checks
only the exact node identities selected by the request and retained edge endpoints, so an unrelated N
review cannot satisfy it. The set producer sorts actual edge rows by `(id, revision, digest)` before
creating the manifest, partitions, assignments, and stream digest; readers retain strict order checks.

Consumer-M/R 003 closes center ownership with an explicit dispatch table for all 13 relations and
both directions. Scope membership is only the opposite-side population: typed leaf centers use exact
identity, containers use the existing `contains` resolver, assignment/producer/migration uses saved
contributors, decomposition uses the saved parent link, Task exercises/outputs use retained
 declarations, and impact uses the change inventory. A Task projection projects `owner_mapping`,
required node identities, and mechanical contributor AND checks to the same local Task owner set;
global requests retain every contributor. Incoming `realizes` cannot mix parent and child ACs, and
local A cannot inherit global B's contributor. Foreign centers and direction mismatches remain
unverified/missing under the shared contract; supported relations are not collapsed to unsupported.

For MR808 candidate/Task centers, keep candidate and Task refs out of scope roots. The internal
`resolve_center_owners` boundary must use the sealed context's canonical Breakdown assignment and
the selected scope's exact artifact anchor, then resolve a current candidate through the actual
Assurance provenance chain to its exact Task revision. Reuse that result for scope admission,
required output/exercise selection, contributor matching, and Task projections. A valid material from
another Task or project, an unassigned Task, a stale definition, a forged owner field, or a helper
only/mock scope must remain rejected. Do not infer assignment from `read_artifacts`, future outputs,
or caller-supplied owner values; the C/P producer adapters are mechanically connected while stage
gates, installed/live, and actual LLM acceptance remain explicitly unimplemented.

## Consumer-P to Consumer-M/R produced-by closure

For the v1 `produced_by` artifact-to-Task path, M resolves the draft Knowledge artifact only through
the immutable `artifact_production` material written by `task.artifacts_collect`. The matcher requires
the exact envelope and payload/CAS digest, Task revision, candidate run/receipt/epoch and producer
actor, manifest declaration and output body, plus the ordered Task/candidate/observed-execution/artifact
dependency refs. A foreign producer, an old candidate, a missing material row, or a changed artifact
revision remains `unverified`; caller-supplied producer fields and draft acceptance are not substitutes.

Live M/R readers first use the immutable `assurance_refs` dependency index to select material rows
whose saved artifact and Task identities exactly match the requested edge. They then validate only
that selected material's envelope, payload/CAS, P provenance, and dependency closure; a selected row
whose payload identity differs is an integrity failure. An unrelated Task's damaged payload therefore
does not poison a Task-local proof, while missing or ambiguous material for the requested pair still
rejects the proof. Global denominator reads, standard archive validation, and GC retain their global
closure and continue to detect the damaged unrelated material.

The draft endpoint exception is scoped to this relation and material boundary. Ordinary artifact
currentness remains accepted-only. M maps the material's declaration to the saved Task
`required_output` obligation, while the historical E2 acceptance claim is retained only when its exact
source/pointer/value identity maps uniquely. N reviews the retained Task/declaration context; E reviews
the sealed edge packets; S reviews the adopted set synthesis and independent Runtime receipts. All
three are required, and the material matcher still runs when E/S receipts are absent. Historical P
revision pins remain readable through the historical resolver, but every live produced-by edge/set/
criteria draft exception additionally compares the pinned artifact revision and body digest with the
current Knowledge head. A same-Control Runtime subprocess fixture covers the positive path, missing/
foreign/old/meaning-review negatives, artifact-head revision, local CAS isolation, global GC/archive,
and a real historical candidate/epoch replan. C profile-v3 is mechanically connected to this accepted
M/R path; stage gates, installed/live, official archives, and real LLM acceptance remain later work.

## Unit4-O program origin records

`program_origins` is immutable program introduction history, not a stage gate.
Schema 16 creates the table and its update/delete barriers. A true v15-to-v16
migration backfills one `legacy-preserved` / `schema-migration` record per
existing program in the same transaction as the schema update; the digest
covers the complete pre-migration program row. Every public `program.begin`
mode writes one `e3-required` / `program.begin` record together with the program
row and `program_started` event. New programs are never stamped as legacy and
missing origins are never inferred from time, mode, or current profile.

The internal read-only origin resolver checks exact program/project identity,
body keys, format, schema, legal policy/origin pair, and digest. It raises a
structured fault for missing, unknown, corrupt, illegal, or cross-project
history and returns only verified origin identity/body data. It does not return
`allow`, `gate`, or `strong` decisions. Full program binding and stage/Task/
local/Delivery enforcement remain separate later components.

Current read-only specification exports use `daikibo.spec.v5` when the project
has no assurance history and `daikibo.spec.v6` when assurance rows are present.
Both use `daikibo.planning-history.v2` and retain one-to-one
`program_origins`; v6 also carries a historical-only `observed_context` with
the project-scoped Task/candidate/run/receipt/repository rows and their exact
typed CAS closure. That lets the exported history validate after its original
controller home is unavailable. It does not restore runtime state or create
fresh review/test evidence, and it does not export controller credentials or
signing keys. The complete direct JSON export is limited to 256 MiB; an export
over that bound fails as `snapshot_too_large`, so use the explicit chunked
snapshot/archive path for larger history. Existing v2-v5 specification
readers remain available. Current chunked snapshots/archives use v12 and carry
an origin section, including an empty section for a project without programs.
Old v1-v11 archives are read as their historical format. Origin rows are
database history rather than CAS leaves, so backup restore and GC retain them
without creating or collecting an origin blob.

The schema16 origin validator is shared by current specification export,
chunked baseline generation, operational backup, restore of schema16 state,
and GC. It checks the schema/table metadata and validates every program's
one-to-one origin record, including body type, legal policy/origin pair, and
digest. Missing metadata or rows raises a structured Fault and cannot select
an older output format. Older backup state may still restore first and then
run the normal schema migration. The v16 migration rejects a mixed pre-v16
database that already contains origin metadata; it only backfills a genuine
old-schema database. Policy and origin values are required to be strings
before enum matching so malformed JSON remains a structured origin Fault.

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

## Task definition pin at plan_tests (Unit 1/A)

`task.plan_tests` keeps its public arguments and receives the existing
Runtime-owned `VerificationMaterialCoordinator` through the private Control
composition. Inside one writer transaction it re-reads the authorized current
Task revision and lifecycle state, stores the plan, re-reads the saved Task and
plan rows, and calls `pin_test_plan`. The existing `test_plan_frozen` event is
written before that transaction commits. A pin failure therefore rolls back the
plan, event, and material object together; any CAS leaf left by a failed write
remains subject to the existing blob/material GC rules. The route creates no
candidate, run, or check result, and the Runtime execution producer remains in
place.

Task-dependent validation is performed from the current canonical Task inside
that transaction. This includes the production requirement for a measured
pytest/JUnit inventory, current authorization and lifecycle state, and the
applicable review constraint. If a reviewed Task revision changes from analysis
to production after the preliminary read, a command-only plan is rejected
before plan storage or material pinning; the check is general for all Task
revisions and is not an analysis-to-production case patch.

Each capture retains exact Task revision/definition and plan/check identity.
Re-capture may retain a new exact pin while its semantic plan payload remains
stable. A changed plan or Task revision makes the old pin non-current; retained
history and archive material remains readable through the read-only resolver.
The earlier Unit 1/A pin unit did not implement checkpoint scheduling, private
R4 candidate connection, or all-writer enforcement. The bounded Unit 2/B
classifier and Unit 3 private R4-to-stage connection are documented below; the
full writer enforcement boundary remains later work.

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

## Unit3 plan/Task and P/MR read paths

The read-only Unit3 evaluator uses the adopted profile identity as the exact
relation-set scope and calls the sealed `build_relation_request`,
`build_review_assurance`, and `evaluate_criteria` boundaries. Plan and Task
relation consumers enumerate controller-derived centers and immutable set
manifests. A missing set owner remains `missing`; N, E, and S evidence are
all required. Integration and delivery relation producers remain pending.

The selected profile wire fixes the effective relation registry for this
read path. `assurance.profile.v2` uses `REGISTRY_V1_DIGEST` and
`assurance.profile.v3` uses `REGISTRY_V2_DIGEST`; the selected event's format
and effective digest must agree with the validated profile body. The same
digest is passed to `registry_entry`, adopted-set matching, sealed relation
requests, criteria, capability, and semantic-fingerprint readers. Unknown,
malformed, stale, or mismatched selection metadata remains an unverified
read-only diagnostic and cannot be supplied by a caller field.

Profile v3 plan/Task contexts may have no Delivery snapshot. Their denominator
keeps an explicit unavailable Delivery inventory and never calls a null value a
successful declared-output population. A real Runtime Task/plan review and
actual M/R edge/set review must still provide N, E, and S; missing evidence and
stale dependencies remain blocking. This finite route does not add Delivery or
integration producers, writer enforcement, completion stage gates, all-13
coverage, installed/live acceptance, official ZIP, or LLM acceptance.

For a `produced_by` artifact-to-Task edge, resolve the current draft endpoint
only through its exact `artifact_production` material. Validate the saved
envelope/payload/CAS, Task and candidate identity, Runtime receipt/run/epoch,
producer actor, declaration, output body, and dependency order. Historical
pins remain readable while a live edge also checks the artifact's current
head. Missing or foreign material cannot be replaced by a caller field or a
draft status.

Task execution results are a separate resolver. At ready/claim/execute,
candidate and formal checks are future diagnostics. At candidate, current
preadoption evidence is required while formal checks remain future. At
complete/recheck, resolve every frozen check and require a current candidate,
receipt/run, verification material, adjusted runtime check identity, and
successful result.

The private pre-adoption observation adapter is connected to the read-only Task
candidate evaluator in the finite Runtime boundary. It consumes only the
controller/actor-sealed identity after durable run/receipt/CAS observation and
before the Task UPDATE/candidate INSERT. Deferred future obligations remain
visible and `strong_complete` remains false; stage rejection retains the
observation for a fresh Control. Full writer enforcement remains later work.

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

DOMAIN v5 public contract: `assurance.catalog.profiles.v5` publishes the exact
fields and contract digest. Use scope v2 → obligations v2, then profile v5 with
`required_scope_contract` / `required_node_contract`. `review.run` accepts the
new `domain_responsibility` role for current accepted DOMAIN subjects. Review
coverage is the exact `context.domain_review.required_coverage` list; generic
acceptance strings are not substitutes. New-format portable history requires
spec7/history2/archive13. Migration is explicit source-backed review and CAS,
followed by new relation and node evidence; no current read performs migration.
