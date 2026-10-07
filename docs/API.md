# 公開API — 1.0.0.dev31

GENERATED — DO NOT EDIT。`tools/generate_api.py`で実コードから生成。

単一ユーザーのローカルRPC。認証/特権なし。read_onlyは通信の再送区分であり、監査記録の付随書込みまで禁止する意味ではありません。

分割uploadの確定は未審査の提案です。TaskやProgramの状態は実証拠と現在版を照合して決まり、API呼出成功だけでは最終受入になりません。

Consumer-Cのbuild_relation_request / build_review_assurance / evaluate_criteriaは内部controllerのin-process helperであり、公開routeではありません。このため生成API一覧には載せません。

run.work_changes / run.work_readはproject-scopedな通常run working-productを、run.recovery / run.recovery_readはtask-scopedなcollector失敗時のfailed-artifacts manifestを読む別契約です。changes entryはafter.kind=fileのときafter.blobをexpected SHAとして選び、after=nullの削除はread対象にしません。これはwork_read返答のsha256とは区別します。指定run・entry(repo/path)・SHA・pageを毎回固定し、全ページのdecoded bytesを保存してsize/SHAを再検証します。base64はtransport専用でconsoleへ本文を流さず、readは採用・成功・承認・再実行を行いません。owner権限へのすり替えやcurrent pathの推測はせず、旧手順は履歴として扱います。

| 操作 | 引数 | 読取区分 |
|---|---|---|
| `adapter.list` | `(actor)` | true |
| `adapter.qualification_catalog` | `(actor)` | true |
| `adapter.register` | `(actor, name, kind, executable, extra_args=None, provider=None, model=None)` | false |
| `api.describe` | `(actor, method=None)` | true |
| `architecture.check` | `(actor, project, modules, rules)` | false |
| `artifact.accept` | `(actor, artifact: 'str', expected_revision: 'int', review_receipt: 'str \| None' = None)` | false |
| `artifact.catalog` | `(actor, project, kind=None, status=None, offset=0, limit=50, expected_snapshot=None)` | true |
| `artifact.get` | `(actor, artifact: 'str', revision: 'int \| None' = None)` | true |
| `artifact.history` | `(actor, artifact, limit=100, offset=0)` | true |
| `artifact.list` | `(actor, project: 'str', kind=None, limit=100, offset=0)` | true |
| `artifact.propose` | `(actor, project: 'str', kind: 'str', body: 'dict', owner: 'str' = 'unassigned', artifact_id: 'str \| None' = None)` | false |
| `artifact.read` | `(actor, artifact, expected_digest, offset=0, byte_budget=12000, revision=None)` | true |
| `artifact.revise` | `(actor, artifact: 'str', expected_revision: 'int', body: 'dict', reason: 'str')` | false |
| `artifact.save` | `(actor, artifact: 'str', expected_revision: 'int', body: 'dict', reason: 'str')` | false |
| `assurance.adopt` | `(actor, project: 'str', subject: 'str', expected_digest: 'str', expected_head: 'str \| None', review_refs: 'list[dict[str, Any]]') -> 'dict[str, Any]'` | false |
| `assurance.catalog` | `(actor, contract_digest: 'str \| None' = None) -> 'dict[str, Any]'` | true |
| `assurance.contains` | `(actor, project: 'str', container: 'dict[str, Any]', member: 'dict[str, Any]') -> 'dict[str, Any]'` | true |
| `assurance.edge_propose` | `(actor, project: 'str', body: 'dict[str, Any]', expected_head: 'str \| None' = None) -> 'dict[str, Any]'` | false |
| `assurance.evaluate_current` | `(actor, ref: 'dict[str, Any]', context: 'dict[str, Any] \| None' = None) -> 'dict[str, Any]'` | true |
| `assurance.history` | `(actor, project: 'str', logical_id: 'str \| None' = None, limit: 'int' = 100, offset: 'int' = 0, cursor: 'str \| None' = None) -> 'dict[str, Any]'` | true |
| `assurance.object_get` | `(actor, project: 'str', object_id: 'str') -> 'dict[str, Any]'` | true |
| `assurance.object_list` | `(actor, project: 'str', kind: 'str \| None' = None, limit: 'int' = 100, offset: 'int' = 0) -> 'dict[str, Any]'` | true |
| `assurance.pin` | `(actor, project: 'str', selector: 'dict[str, Any]') -> 'dict[str, Any]'` | false |
| `assurance.profile_propose` | `(actor, project: 'str', program: 'str \| None', body: 'dict[str, Any]', expected_head: 'str \| None' = None) -> 'dict[str, Any]'` | false |
| `assurance.refs` | `(actor, project: 'str', ref_kind: 'str \| None' = None, ref_id: 'str \| None' = None, limit: 'int' = 100, offset: 'int' = 0) -> 'dict[str, Any]'` | true |
| `assurance.report` | `(actor, project: 'str', program: 'str \| None' = None, stage: 'str \| None' = None, cursor: 'str \| None' = None, limit: 'int' = 500, checkpoint: 'str \| None' = None, task: 'dict[str, Any] \| None' = None, delivery: 'dict[str, Any] \| None' = None, proposed_breakdown: 'str \| None' = None, local_execution: 'str \| None' = None) -> 'dict[str, Any]'` | true |
| `assurance.resolve` | `(actor, project: 'str', ref: 'dict[str, Any]', context: 'dict[str, Any] \| None' = None) -> 'dict[str, Any]'` | true |
| `assurance.resolve_pinned` | `(actor, ref: 'dict[str, Any]') -> 'dict[str, Any]'` | true |
| `assurance.review_subject` | `(actor, project: 'str', subject: 'str', cursor: 'str \| None' = None, limit: 'int' = 500) -> 'dict[str, Any]'` | true |
| `assurance.scope_propose` | `(actor, project: 'str', body: 'dict[str, Any]', expected_head: 'str \| None' = None) -> 'dict[str, Any]'` | false |
| `assurance.set_propose` | `(actor, project: 'str', body: 'dict[str, Any]', expected_head: 'str \| None' = None) -> 'dict[str, Any]'` | false |
| `assurance.withdraw_propose` | `(actor, project: 'str', subject: 'str', reason: 'str', authority_refs: 'list[dict[str, Any]]', expected_head: 'str \| None' = None) -> 'dict[str, Any]'` | false |
| `automation.configure` | `(actor, project, adapter, reviewer, budget_seconds=14400, concurrency=4, enabled=True)` | false |
| `automation.status` | `(actor, project)` | true |
| `baseline.create` | `(actor, project, layout='auto', chunk_bytes=1048576)` | false |
| `baseline.export` | `(actor, baseline)` | false |
| `baseline.get` | `(actor, baseline)` | true |
| `baseline.inspect_archive` | `(actor, path, expected_sha256)` | true |
| `baseline.list` | `(actor, project, limit=100, offset=0)` | true |
| `baseline.rebuild_git` | `(actor, baseline, expected_snapshot, reason)` | false |
| `baseline.verify` | `(actor, baseline)` | true |
| `blob.read` | `(actor, blob, project=None, offset=0, limit=65536)` | true |
| `breakdown.activate` | `(actor, breakdown, expected_active=None)` | false |
| `breakdown.audit` | `(actor, breakdown, reviews=True, readonly=False)` | true |
| `breakdown.get` | `(actor, breakdown, offset=0, limit=100, include_structure=True)` | true |
| `breakdown.packet` | `(actor, packet)` | true |
| `breakdown.propose` | `(actor, program, title, rationale, units, expected_active=None, byte_budget=24000, origin_subplan=None)` | false |
| `breakdown.units` | `(actor, breakdown, offset=0, limit=50, byte_budget=1048576)` | true |
| `breakdown.upload_abandon` | `(actor, upload, expected_revision, reason)` | false |
| `breakdown.upload_begin` | `(actor, program, title, rationale, expected_active=None, byte_budget=24000)` | false |
| `breakdown.upload_finalize` | `(actor, upload, expected_revision)` | false |
| `breakdown.upload_list` | `(actor, project, program=None, status=None, offset=0, limit=50)` | true |
| `breakdown.upload_put` | `(actor, upload, expected_revision, units)` | false |
| `breakdown.upload_status` | `(actor, upload, offset=0, limit=100)` | true |
| `change.apply` | `(actor, change, review_receipt)` | false |
| `change.attempt` | `(actor, change, level, body)` | false |
| `change.delta` | `(actor, change, expected_revision, deltas, reason, force_revision=False)` | false |
| `change.propose` | `(actor, project, body)` | false |
| `change.withdraw` | `(actor, change, reason, compensation)` | false |
| `code.consumers` | `(actor, project, symbol, limit=100, offset=0, expected_snapshot=None)` | true |
| `code.inventory` | `(actor, project)` | true |
| `code.read` | `(actor, repo, path, start_line=1, line_count=100, expected_digest=None)` | true |
| `code.search` | `(actor, project, query, limit=20)` | true |
| `conflict.report` | `(actor, project, refs, explanation, options)` | false |
| `context.build` | `(actor, task, byte_budget=200000, query=None, persist=True)` | false |
| `context.fresh` | `(actor, context)` | true |
| `contract.check_instance` | `(actor, document, expected_digest, instance_document, instance_digest, entry='#', instance_entry='#', dialect=None)` | true |
| `contract.check_schema` | `(actor, document, expected_digest, entry='#', dialect=None)` | true |
| `contract.compare` | `(actor, interface, proposed)` | false |
| `contract.compare_documents` | `(actor, before, before_digest, after, after_digest, standard='auto', offset=0, limit=100)` | true |
| `contract.inspect_document` | `(actor, document, expected_digest, standard='auto', section='entries', offset=0, limit=50)` | true |
| `contract.propose_document` | `(actor, document, expected_digest, entry, semantics, standard='auto', artifact_id=None)` | false |
| `contract.read_entry` | `(actor, document, expected_digest, entry, standard='auto', start=0, limit=16000)` | true |
| `contract.schema_capabilities` | `(actor)` | true |
| `contract.validate` | `(actor, schema, value)` | true |
| `decision.apply` | `(actor, decision, review_receipt)` | false |
| `decision.get` | `(actor, decision)` | true |
| `decision.propose` | `(actor, project, body)` | false |
| `decision.recent` | `(actor, project, since=0)` | true |
| `decision.respond` | `(actor, decision, expected_digest, choice, utterance, source=None)` | false |
| `delivery.certify` | `(actor, delivery, check_only=False)` | false |
| `delivery.commit` | `(actor, delivery, message)` | false |
| `delivery.configure` | `(actor, project, body, expected_digest=None, review_receipt=None)` | false |
| `delivery.export` | `(actor, delivery, repo)` | false |
| `delivery.get` | `(actor, delivery)` | true |
| `delivery.prepare` | `(actor, project)` | false |
| `delivery.profile_current` | `(actor, project)` | true |
| `dialogue.input` | `(actor, content, project=None, name='New project', bounded=False, start_program=False)` | false |
| `document.attach_text` | `(actor, document, raw_digest, content, reason)` | false |
| `document.get` | `(actor, document)` | true |
| `document.import` | `(actor, project, content_base64, locator, media_type='application/octet-stream')` | false |
| `evidence.get` | `(actor, evidence)` | true |
| `execution.configure_limits` | `(actor, project, limits, reason)` | false |
| `execution.usage` | `(actor, project)` | true |
| `execution_control.apply` | `(actor, proposal, expected_digest, review_receipt)` | false |
| `execution_control.get` | `(actor, proposal)` | true |
| `execution_control.history` | `(actor, task, offset=0, limit=100, expected_snapshot=None)` | true |
| `execution_control.history_detail` | `(actor, task, attempt_epoch, kind, offset=0, limit=16, expected_snapshot=None)` | true |
| `execution_control.inventory` | `(actor, task=None, project=None, offset=0, limit=100, expected_snapshot=None)` | true |
| `execution_control.packet` | `(actor, proposal, offset=0, limit=1, expected_snapshot=None)` | true |
| `execution_control.policy_apply` | `(actor, proposal, expected_digest, review_receipt)` | false |
| `execution_control.policy_get` | `(actor, project)` | true |
| `execution_control.policy_propose` | `(actor, project, body)` | false |
| `execution_control.propose` | `(actor, task, expected_revision, body)` | false |
| `execution_control.withdraw` | `(actor, proposal, expected_digest, reason)` | false |
| `gate.evaluate` | `(actor, task, gate='complete')` | false |
| `inbox.acknowledge` | `(actor, item, utterance, source=None, expected_digest=None)` | false |
| `inbox.catalog` | `(actor, project, offset=0, limit=50, expected_snapshot=None)` | true |
| `inbox.get` | `(actor, project)` | false |
| `inbox.read` | `(actor, item, expected_digest, offset=0, byte_budget=12000)` | true |
| `job.cancel` | `(actor, job, reason)` | false |
| `job.get` | `(actor, job)` | true |
| `job.list` | `(actor, project, limit=100, offset=0, kind=None, status=None, task=None, subject=None, since=0, until=None, expected_snapshot=None)` | true |
| `job.retry` | `(actor, job, reason)` | false |
| `job.submit` | `(actor, kind, args, dedup=None, retry=None)` | false |
| `local_execution.audit` | `(actor, local_execution, reviews=True, offset=0, limit=100)` | true |
| `local_execution.certify` | `(actor, local_execution, expected_digest, request_id=None)` | false |
| `local_execution.get` | `(actor, local_execution, offset=0, limit=50)` | true |
| `local_execution.inventory` | `(actor, program, subplan, tasks, offset=0, limit=100)` | true |
| `local_execution.list` | `(actor, program, offset=0, limit=50)` | true |
| `local_execution.packet` | `(actor, packet)` | true |
| `local_execution.propose` | `(actor, program, subplan, tasks, rationale, stage_evidence, dispositions, byte_budget=24000, request_id=None)` | false |
| `local_execution.withdraw` | `(actor, local_execution, expected_digest, reason, request_id=None)` | false |
| `native.acknowledge` | `(actor, session, item, source, quote, expected_digest=None)` | false |
| `native.actions` | `(actor, session, actions, source=None)` | false |
| `native.attach` | `(actor, session, cwd, project=None, name='New project', client='claude', register_repository=True)` | false |
| `native.completion` | `(actor, session, subject)` | false |
| `native.context` | `(actor, session)` | false |
| `native.input` | `(actor, session, content, turn_id=None, origin='skill-relay', start_program=False)` | false |
| `native.lookup` | `(actor, cwd)` | true |
| `native.present_decision` | `(actor, session, decision)` | false |
| `native.respond` | `(actor, session, decision, expected_digest, source, choice, quote)` | false |
| `native.stop_feedback` | `(actor, session, stop_hook_active=False)` | false |
| `policy.get` | `(actor, project)` | true |
| `policy.propose` | `(actor, project, body)` | false |
| `program.advance` | `(actor, program, expected_revision, review_receipt)` | false |
| `program.begin` | `(actor, project, source, mode='auto', compact=False)` | false |
| `program.blockers` | `(actor, program, offset=0, limit=50, expected_snapshot=None, byte_budget=24000)` | true |
| `program.breakdown_status` | `(actor, program, reviews=True, readonly=False)` | true |
| `program.catalog` | `(actor, project, offset=0, limit=50, expected_snapshot=None)` | true |
| `program.completion` | `(actor, program, delivery=None)` | true |
| `program.finish` | `(actor, program, expected_revision, delivery, review_receipt)` | false |
| `program.next` | `(actor, program)` | true |
| `program.partition_review` | `(actor, program, byte_budget=100000)` | false |
| `program.reopen` | `(actor, program, expected_revision, target_phase, reason, cause, review_receipt=None)` | false |
| `program.review_summary` | `(actor, program)` | true |
| `program.status` | `(actor, program)` | true |
| `project.create` | `(actor: 'Actor', name: 'str', config=None)` | false |
| `project.export` | `(actor, project)` | true |
| `project.get` | `(actor: 'Actor', project: 'str')` | true |
| `project.progress` | `(actor, project, offset=0, limit=100, expected_snapshot=None)` | true |
| `provider.configure` | `(actor, name, kind, base_url, secret, models=None, max_requests=256)` | false |
| `provider.list` | `(actor)` | true |
| `provider.remove` | `(actor, name)` | false |
| `remote.configure` | `(actor, project, repo, body, token=None)` | false |
| `remote.publish` | `(actor, delivery, title, body)` | false |
| `remote.status` | `(actor, delivery)` | true |
| `repository.list` | `(actor, project)` | true |
| `repository.register` | `(actor, project, name, path)` | false |
| `request.result` | `(actor, request_id, expected_digest, offset=0, limit=65536)` | true |
| `research.fetch` | `(actor, project, url, purpose)` | false |
| `run.get` | `(actor, run)` | true |
| `run.recovery` | `(actor, run, offset=0, limit=100, expected_digest=None)` | true |
| `run.recovery_read` | `(actor, run, repo, path, expected_digest, offset=0, limit=65536, expected_manifest=None)` | true |
| `run.work_changes` | `(actor, run, offset=0, limit=100)` | true |
| `run.work_read` | `(actor, run, repo, path, expected_digest, offset=0, limit=65536)` | true |
| `source.add` | `(actor: 'Actor', project: 'str', content: 'str', locator: 'str' = 'conversation')` | false |
| `source.classify` | `(actor, source: 'str', start: 'int', end: 'int', category: 'str', refs: 'list[str]', reason: 'str')` | false |
| `source.coverage` | `(actor, project: 'str')` | true |
| `source.packet` | `(actor, source, start, end, expected_digest)` | true |
| `source.partition` | `(actor, source, byte_budget=20000, offset=0, limit=100)` | false |
| `source.partition_status` | `(actor, source, byte_budget=20000)` | true |
| `source.read` | `(actor, source: 'str', start=0, limit=12000)` | true |
| `subplan.audit` | `(actor, subplan, reviews=True, offset=0, limit=100, readonly=False)` | true |
| `subplan.compose` | `(actor, subplan, expected_active=None, byte_budget=24000)` | false |
| `subplan.coverage` | `(actor, subplan, offset=0, limit=100)` | true |
| `subplan.get` | `(actor, subplan, offset=0, limit=50)` | true |
| `subplan.list` | `(actor, program, offset=0, limit=50)` | true |
| `subplan.packet` | `(actor, packet)` | true |
| `subplan.propose` | `(actor, program, title, rationale, obligations, units, children=None, context_artifacts=None, byte_budget=24000, boundary_contracts=None)` | false |
| `system.audit` | `(actor, all_blobs=False)` | false |
| `system.backup` | `(actor)` | false |
| `system.doctor` | `(actor)` | false |
| `system.gc` | `(actor, dry_run=True, minimum_age=86400)` | false |
| `system.shutdown` | `(actor)` | false |
| `task.apply_revision` | `(actor, proposal, expected_digest, review_receipt)` | false |
| `task.artifacts_collect` | `(actor, task, expected_revision, candidate, repository, path)` | false |
| `task.cancel` | `(actor, task, reason)` | false |
| `task.claim` | `(actor, project, task=None, after_task=None, scan_limit=1000, adapter=None)` | false |
| `task.complete` | `(actor, task, expected_revision)` | false |
| `task.create` | `(actor, project, body)` | false |
| `task.get` | `(actor, task)` | true |
| `task.heartbeat` | `(actor, task, epoch)` | false |
| `task.history_record` | `(actor, history, expected_digest=None)` | true |
| `task.list` | `(actor, project, limit=100, offset=0)` | true |
| `task.parallel_candidates` | `(actor, project, limit=100, offset=0)` | true |
| `task.plan_tests` | `(actor, task, body, review_receipt=None)` | false |
| `task.preflight` | `(actor, task, adapter)` | true |
| `task.progress` | `(actor, task)` | true |
| `task.propose_plan_revision` | `(actor, task, expected_revision, expected_plan_digest, body, reason, evidence_refs)` | false |
| `task.propose_revision` | `(actor, task, expected_revision, body, reason)` | false |
| `task.ready` | `(actor, task)` | false |
| `task.replan` | `(actor, task, expected_revision, reason, review_receipt=None)` | false |
| `task.revision_get` | `(actor, proposal)` | true |
| `task.revision_history` | `(actor, task, offset=0, limit=50, expected_snapshot=None)` | true |
| `task.revision_list` | `(actor, project, task=None, status='proposed', offset=0, limit=50, expected_snapshot=None)` | true |
| `task.test_evidence` | `(actor, task, offset=0, limit=100, expected_selection_digest=None)` | true |
| `task.withdraw_revision` | `(actor, proposal, expected_digest, reason)` | false |
| `trace.audit` | `(actor, project)` | true |
| `trace.impact` | `(actor, project, artifacts)` | true |
| `trace.link` | `(actor, source: 'str', target: 'str', relation: 'str', confidence='inferred', basis='', review_receipt=None)` | false |
| `traceability.adopt` | `(actor, project, revision=None, expected_digest=None, review_refs=None, subject=None, expected_active=None, expected_head_record=None)` | false |
| `traceability.closure_propose` | `(actor, project, revision, stage, task=None, delivery=None, expected_material_digest=None, expected_head_record=None)` | false |
| `traceability.closure_subject` | `(actor, project, revision, packet=0, limit=500, proposal=None)` | true |
| `traceability.coverage` | `(actor, project, revision=None, limit=100, cursor=None)` | true |
| `traceability.decide_propose` | `(actor, project, revision, decisions, expected_digest=None, expected_head_record=None)` | false |
| `traceability.diff` | `(actor, revision, other_revision, limit=100, cursor=None)` | true |
| `traceability.export` | `(actor, project, path=None)` | false |
| `traceability.extract` | `(actor, proposal, expected_digest=None, **options)` | false |
| `traceability.get` | `(actor, revision)` | true |
| `traceability.history` | `(actor, project, limit=100, cursor=None)` | true |
| `traceability.import` | `(actor, path, expected_sha256=None, project=None)` | false |
| `traceability.inspect_archive` | `(actor, path, expected_sha256=None)` | true |
| `traceability.items` | `(actor, revision, limit=100, cursor=None, offset=0, path=None, item_kind=None, status=None)` | true |
| `traceability.list` | `(actor, project, set_id=None, kind=None, limit=100, cursor=None, offset=0)` | true |
| `traceability.map_adopt` | `(actor, project, mapping, expected_digest=None, review_refs=None, expected_head_record=None)` | false |
| `traceability.map_propose` | `(actor, project, revision, mappings, expected_digest=None, expected_head_record=None)` | false |
| `traceability.propose` | `(actor, project, scope=None, adapter='python-ast-v1', expected_active=None, kind='population', name=None, roots=None, include=None, repository=None, commit=None, source=None, blob=None, **extra)` | false |
| `traceability.read` | `(actor, revision, item=None, path=None, offset=0, limit=65536)` | true |
| `traceability.restore` | `(actor, path, expected_sha256=None, project=None)` | false |
| `traceability.review_subject` | `(actor, proposal, limit=500, packet=0)` | true |
| `traceability.scope_propose` | `(actor, project, revision, program, scope_requirement, applicable_from='plan', mandatory=True, expected_head_record=None)` | false |
| `waiver.close` | `(actor, waiver)` | false |
| `waiver.request` | `(actor, project, subject, criterion, reason, expires, remediation_task, controls, decision=None)` | false |
| `workflow.pause` | `(actor, project, task=None, paused=True, fence=False)` | false |
| `workflow.reconcile` | `(actor, project=None)` | false |
| `workflow.status` | `(actor, project)` | true |
| `workflow.summary` | `(actor, project)` | true |
| `workstream.activate` | `(actor, scope)` | false |
| `workstream.completion` | `(actor, scope)` | true |
| `workstream.finish` | `(actor, scope)` | false |
| `workstream.get` | `(actor, scope, offset=0, limit=100)` | true |
| `workstream.list` | `(actor, program, offset=0, limit=50)` | true |
| `workstream.packet` | `(actor, packet)` | true |
| `workstream.program_audit` | `(actor, program)` | true |
| `workstream.propose` | `(actor, program, title, rationale, unit_ids, parent=None, previous=None, byte_budget=24000)` | false |
| `workstream.return_abandon` | `(actor, proposal, reason)` | false |
| `workstream.return_advance` | `(actor, proposal, offset=0, limit=100)` | false |
| `workstream.return_apply` | `(actor, proposal, expected_digest, root_packet, review_receipt)` | false |
| `workstream.return_get` | `(actor, proposal, offset=0, limit=100)` | true |
| `workstream.return_list` | `(actor, scope, offset=0, limit=50)` | true |
| `workstream.return_packet` | `(actor, packet, historical=False)` | true |
| `workstream.return_propose` | `(actor, scope, reason, byte_budget=24000)` | false |
| `workstream.selection` | `(actor, scope, kind='tasks', offset=0, limit=100)` | true |
| `workstream.status` | `(actor, scope)` | true |
| `workstream.withdraw` | `(actor, scope, reason, review_receipt)` | false |
