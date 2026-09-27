BEGIN TRANSACTION;
CREATE TABLE adapters (
 name TEXT PRIMARY KEY, body TEXT NOT NULL CHECK(json_valid(body)), qualified INTEGER NOT NULL DEFAULT 0, receipt TEXT
);
CREATE TABLE artifacts (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), kind TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision>0), status TEXT NOT NULL CHECK(status IN ('draft','accepted','superseded','withdrawn')),
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, owner TEXT NOT NULL, created REAL NOT NULL
);
INSERT INTO "artifacts" VALUES('REQUIREMENT-21c38e93425826220bf2521c','PRJ-cab2f7a692c564e9c2313e05','requirement',1,'accepted','{"acceptance":["AC-ORIGIN"],"source_refs":["SRC-d5c5b95580d9be3ef284e8d0"],"statement":"Retain the original program origin","title":"Origin migration fixture"}','c68e6ad0edc4d08be0e4f5e6f5a523f4348be25d1f1824902531d0f63a8bfdb5','unassigned',1.7896127899750266e+09);
CREATE TABLE assurance_events (
 id TEXT PRIMARY KEY,
 project TEXT NOT NULL REFERENCES projects(id),
 subject_id TEXT NOT NULL REFERENCES assurance_objects(id),
 subject_digest TEXT NOT NULL,
 event_kind TEXT NOT NULL CHECK(event_kind IN ('adopt','withdraw','supersede')),
 expected_head TEXT,
 previous TEXT,
 body TEXT NOT NULL CHECK(json_valid(body)),
 created REAL NOT NULL
);
CREATE TABLE assurance_heads (
 project TEXT NOT NULL REFERENCES projects(id),
 logical_id TEXT NOT NULL,
 head_event TEXT NOT NULL REFERENCES assurance_events(id),
 PRIMARY KEY(project,logical_id)
);
CREATE TABLE assurance_objects (
 id TEXT PRIMARY KEY,
 project TEXT NOT NULL REFERENCES projects(id),
 kind TEXT NOT NULL CHECK(kind IN ('edge','set','packet','profile','obligations','material','scope')),
 logical_id TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision>0),
 body TEXT NOT NULL CHECK(json_valid(body)),
 digest TEXT NOT NULL,
 created REAL NOT NULL,
 UNIQUE(project,kind,logical_id,revision)
);
CREATE TABLE assurance_refs (
 object_id TEXT NOT NULL REFERENCES assurance_objects(id),
 ordinal INTEGER NOT NULL CHECK(ordinal>=0),
 purpose TEXT NOT NULL,
 ref_kind TEXT NOT NULL,
 ref_id TEXT NOT NULL,
 ref_revision TEXT NOT NULL,
 ref_digest TEXT NOT NULL,
 PRIMARY KEY(object_id,ordinal),
 UNIQUE(object_id,purpose,ref_kind,ref_id,ref_revision,ref_digest)
);
CREATE TABLE attempt_assessments(
 id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id), project TEXT NOT NULL REFERENCES projects(id),
 attempt_epoch INTEGER NOT NULL, attempt_ordinal INTEGER, task_revision INTEGER,
 task_binding TEXT NOT NULL, proposal TEXT NOT NULL REFERENCES execution_control_proposals(id),
 proposal_digest TEXT NOT NULL, implementer_run TEXT NOT NULL, implementer_receipt TEXT NOT NULL,
 reviewer_run TEXT NOT NULL, reviewer_receipt TEXT NOT NULL,
 judgment TEXT NOT NULL CHECK(judgment IN ('progress','no_progress')),
 rationale TEXT NOT NULL, evidence TEXT NOT NULL CHECK(json_valid(evidence)),
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, created REAL NOT NULL,
 UNIQUE(task,attempt_epoch)
);
CREATE TABLE attempts (
 id TEXT PRIMARY KEY, change_id TEXT NOT NULL REFERENCES changes(id), level TEXT NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), created REAL NOT NULL
);
CREATE TABLE automation(project TEXT PRIMARY KEY REFERENCES projects(id),enabled INTEGER NOT NULL,actor TEXT NOT NULL,adapter TEXT NOT NULL,reviewer TEXT NOT NULL,budget REAL NOT NULL,spent REAL NOT NULL DEFAULT 0,concurrency INTEGER NOT NULL,failures INTEGER NOT NULL DEFAULT 0,last_progress REAL NOT NULL,body TEXT NOT NULL);
CREATE TABLE baselines (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), body TEXT NOT NULL CHECK(json_valid(body)),
 digest TEXT NOT NULL, git_commit TEXT, created REAL NOT NULL
);
CREATE TABLE blocks (
 task TEXT NOT NULL REFERENCES tasks(id), kind TEXT NOT NULL, ref TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(task,kind,ref)
);
CREATE TABLE breakdown_adoptions (
 breakdown TEXT PRIMARY KEY REFERENCES breakdowns(id), body TEXT NOT NULL CHECK(json_valid(body)), created REAL NOT NULL
);
CREATE TABLE breakdown_members (
 breakdown TEXT NOT NULL REFERENCES breakdowns(id), packet TEXT NOT NULL REFERENCES breakdown_packets(id), ordinal INTEGER NOT NULL,
 PRIMARY KEY(breakdown,packet), UNIQUE(breakdown,ordinal)
);
CREATE TABLE breakdown_packets (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE breakdown_upload_batches(
 upload TEXT NOT NULL REFERENCES breakdown_uploads(id), revision INTEGER NOT NULL,
 digest TEXT NOT NULL, result TEXT NOT NULL CHECK(json_valid(result)),
 PRIMARY KEY(upload,revision)
);
CREATE TABLE breakdown_upload_units(
 upload TEXT NOT NULL REFERENCES breakdown_uploads(id), unit TEXT NOT NULL,
 ordinal INTEGER NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), bytes INTEGER NOT NULL,
 PRIMARY KEY(upload,unit), UNIQUE(upload,ordinal)
);
CREATE TABLE breakdown_uploads(
 id TEXT PRIMARY KEY, program TEXT NOT NULL REFERENCES programs(id),
 project TEXT NOT NULL REFERENCES projects(id), body TEXT NOT NULL CHECK(json_valid(body)),
 scope_digest TEXT NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL
 CHECK(status IN ('open','finalized','abandoned')), result TEXT, created REAL NOT NULL
);
CREATE TABLE breakdowns (
 id TEXT PRIMARY KEY, program TEXT NOT NULL REFERENCES programs(id), project TEXT NOT NULL REFERENCES projects(id),
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('proposed','active','superseded')), previous TEXT REFERENCES breakdowns(id), created REAL NOT NULL
);
CREATE TABLE candidates (
 id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id), epoch INTEGER NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, implementation_run TEXT NOT NULL REFERENCES runs(id), created REAL NOT NULL
);
CREATE TABLE changes (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), stage TEXT NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), revision INTEGER NOT NULL, created REAL NOT NULL
);
CREATE TABLE conflicts (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), body TEXT NOT NULL CHECK(json_valid(body)),
 status TEXT NOT NULL, decision TEXT, created REAL NOT NULL
);
CREATE TABLE contexts (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), subject TEXT NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE decisions (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), revision INTEGER NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, status TEXT NOT NULL,
 response TEXT, source TEXT REFERENCES sources(id), consistency_receipt TEXT, created REAL NOT NULL
);
CREATE TABLE deliveries (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), body TEXT NOT NULL CHECK(json_valid(body)),
 digest TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE dispositions (
 id TEXT PRIMARY KEY, source TEXT NOT NULL REFERENCES sources(id), start INTEGER NOT NULL,
 end INTEGER NOT NULL CHECK(end>start), category TEXT NOT NULL,
 refs TEXT NOT NULL CHECK(json_valid(refs)), reason TEXT NOT NULL, actor TEXT NOT NULL
);
INSERT INTO "dispositions" VALUES('DISP-cae55ee35c32f243073febdb','SRC-d5c5b95580d9be3ef284e8d0',0,54,'requirement','["REQUIREMENT-21c38e93425826220bf2521c"]','Historical fixture classification','local-user');
CREATE TABLE documents(id TEXT PRIMARY KEY,project TEXT NOT NULL REFERENCES projects(id),body TEXT NOT NULL CHECK(json_valid(body)),status TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, project TEXT, kind TEXT NOT NULL,
 actor TEXT NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), created REAL NOT NULL,
 previous TEXT NOT NULL, key_id TEXT NOT NULL, mac TEXT NOT NULL
);
INSERT INTO "events" VALUES(1,'EVT-cc26dbf61a9981ed1d667b9f',NULL,'cooperative_control_initialized','local-user','{"authentication":false,"execution_model":"cooperative-single-user"}',1.78961278978795218e+09,'0000000000000000000000000000000000000000000000000000000000000000','sha256-unkeyed-v1','382719abaa01a978998cea88c39204b51adfc2e1cd24c6af34d982859f1e6780');
INSERT INTO "events" VALUES(2,'EVT-73dcca0233ae3c64c8c9161a','PRJ-cab2f7a692c564e9c2313e05','project_created','local-user','{"name":"U4-O legacy source fixture"}',1.7896127899631586e+09,'382719abaa01a978998cea88c39204b51adfc2e1cd24c6af34d982859f1e6780','sha256-unkeyed-v1','2c81eda00c14249bacae26eb4cd9d1a2df501d196e0675c84ca8a29bb1d39a32');
INSERT INTO "events" VALUES(3,'EVT-aa67a601db38cd263bf78e8b','PRJ-cab2f7a692c564e9c2313e05','source_registered','local-user','{"digest":"81564d280f95bb0569ea8aa05979c7e819c20d1daeef81872fedd23544d7f7ee","source":"SRC-d5c5b95580d9be3ef284e8d0","trust":"human"}',1.78961278997101354e+09,'2c81eda00c14249bacae26eb4cd9d1a2df501d196e0675c84ca8a29bb1d39a32','sha256-unkeyed-v1','35ea165d04d17938bcc7d602a50d8149268ef9c45e6212ef8d8a14636d469f5c');
INSERT INTO "events" VALUES(4,'EVT-739a39962272cbfd1a0da652','PRJ-cab2f7a692c564e9c2313e05','artifact_proposed','local-user','{"digest":"c68e6ad0edc4d08be0e4f5e6f5a523f4348be25d1f1824902531d0f63a8bfdb5","id":"REQUIREMENT-21c38e93425826220bf2521c","kind":"requirement","revision":1}',1.78961278997524476e+09,'35ea165d04d17938bcc7d602a50d8149268ef9c45e6212ef8d8a14636d469f5c','sha256-unkeyed-v1','2b623ebbc7d4b57b801a286923be3110a28836c5f634ffbb0434d290dd93fb05');
INSERT INTO "events" VALUES(5,'EVT-85a388aee87f21c75b51546a','PRJ-cab2f7a692c564e9c2313e05','artifact_accepted','local-user','{"digest":"c68e6ad0edc4d08be0e4f5e6f5a523f4348be25d1f1824902531d0f63a8bfdb5","id":"REQUIREMENT-21c38e93425826220bf2521c","review":null,"revision":1}',1.78961278997906184e+09,'2b623ebbc7d4b57b801a286923be3110a28836c5f634ffbb0434d290dd93fb05','sha256-unkeyed-v1','350a999facff3ffea731999ddc55d18b3d190f29d32df74349a722f25f0a8e70');
INSERT INTO "events" VALUES(6,'EVT-367567c3ebd82fe05dfc95ef','PRJ-cab2f7a692c564e9c2313e05','source_classified','local-user','{"category":"requirement","range":[0,54],"source":"SRC-d5c5b95580d9be3ef284e8d0"}',1.78961278998310279e+09,'350a999facff3ffea731999ddc55d18b3d190f29d32df74349a722f25f0a8e70','sha256-unkeyed-v1','188b2224e2775249c16764e7aa0ed9aa8c8ad108e145054d123ee96d7ea1acd1');
INSERT INTO "events" VALUES(7,'EVT-97a486db5fd408dc87e0a3b9','PRJ-cab2f7a692c564e9c2313e05','program_started','local-user','{"mode":"greenfield","program":"FLOW-abb5b36ede2630efca467d92"}',1.78961278998675942e+09,'188b2224e2775249c16764e7aa0ed9aa8c8ad108e145054d123ee96d7ea1acd1','sha256-unkeyed-v1','91eeec2a409359a1848b292b3d7989e2e4341495cbd1c3582dadbe7e89e506d2');
INSERT INTO "events" VALUES(8,'EVT-51bcad53658c2b3d5807007c','PRJ-cab2f7a692c564e9c2313e05','source_registered','local-user','{"digest":"446ab594408d6a0443995d56a5023db77960733ac33fb5f493535a51614217fc","source":"SRC-4a54381b1edb5a268eeac8a3","trust":"human"}',1.78961278999336981e+09,'91eeec2a409359a1848b292b3d7989e2e4341495cbd1c3582dadbe7e89e506d2','sha256-unkeyed-v1','aff0b391b5a906a9b61701531d75e6b818db3d54ef6f262bde9076a1be6116a7');
CREATE TABLE execution_attempts(
 id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id), project TEXT NOT NULL REFERENCES projects(id),
 attempt_epoch INTEGER NOT NULL, attempt_ordinal INTEGER NOT NULL, task_revision INTEGER NOT NULL,
 task_binding TEXT NOT NULL, status TEXT NOT NULL,
 implementer_run TEXT REFERENCES runs(id), implementer_receipt TEXT REFERENCES receipts(id),
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL,
 created REAL NOT NULL, updated REAL NOT NULL,
 UNIQUE(task,attempt_epoch), UNIQUE(task,attempt_ordinal)
);
CREATE TABLE execution_control_authorizations(
 id TEXT PRIMARY KEY, proposal TEXT NOT NULL REFERENCES execution_control_proposals(id),
 project TEXT NOT NULL REFERENCES projects(id), task TEXT NOT NULL REFERENCES tasks(id),
 task_revision INTEGER NOT NULL, control_revision INTEGER NOT NULL, proposal_digest TEXT NOT NULL,
 requested_seconds REAL, effective_seconds REAL, assessment TEXT
 CHECK(assessment IS NULL OR assessment IN ('progress','no_progress')),
 reviewer_run TEXT NOT NULL, reviewer_receipt TEXT NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE execution_control_events(
 id TEXT PRIMARY KEY, proposal TEXT NOT NULL REFERENCES execution_control_proposals(id),
 project TEXT NOT NULL REFERENCES projects(id), kind TEXT NOT NULL
 CHECK(kind IN ('applied','withdrawn','superseded')),
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE execution_control_packets(
 id TEXT PRIMARY KEY, proposal TEXT NOT NULL REFERENCES execution_control_proposals(id),
 project TEXT NOT NULL REFERENCES projects(id), ordinal INTEGER NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, created REAL NOT NULL,
 UNIQUE(proposal,ordinal)
);
CREATE TABLE execution_control_proposals(
 id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id), project TEXT NOT NULL REFERENCES projects(id),
 task_revision INTEGER NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL,
 binding TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('proposed','applied','withdrawn','superseded')),
 result TEXT CHECK(result IS NULL OR json_valid(result)), created REAL NOT NULL
);
CREATE TABLE execution_limits(project TEXT PRIMARY KEY REFERENCES projects(id),revision INTEGER NOT NULL,body TEXT NOT NULL CHECK(json_valid(body)),reason TEXT NOT NULL,updated REAL NOT NULL);
CREATE TABLE execution_usage(run TEXT PRIMARY KEY REFERENCES runs(id),project TEXT NOT NULL REFERENCES projects(id),adapter TEXT NOT NULL,status TEXT NOT NULL,tokens INTEGER,cost_microusd INTEGER,body TEXT NOT NULL CHECK(json_valid(body)),created REAL NOT NULL,updated REAL);
CREATE TABLE gate_results (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), subject TEXT NOT NULL, gate TEXT NOT NULL,
 binding TEXT NOT NULL, policy_digest TEXT NOT NULL, verdict TEXT NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), created REAL NOT NULL
);
CREATE TABLE inbox (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), kind TEXT NOT NULL, ref TEXT NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), severity TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
 displayed INTEGER NOT NULL DEFAULT 0, due REAL, created REAL NOT NULL, UNIQUE(project,kind,ref)
);
CREATE TABLE job_attempts(job TEXT NOT NULL REFERENCES jobs(id),attempt INTEGER NOT NULL,status TEXT NOT NULL,started REAL NOT NULL,ended REAL,result TEXT,error TEXT,PRIMARY KEY(job,attempt));
CREATE TABLE jobs(id TEXT PRIMARY KEY,project TEXT,kind TEXT NOT NULL,actor TEXT NOT NULL,args TEXT NOT NULL,status TEXT NOT NULL,dedup TEXT UNIQUE,created REAL NOT NULL,started REAL,ended REAL,result TEXT,error TEXT,cancelled INTEGER NOT NULL DEFAULT 0,attempt_count INTEGER NOT NULL DEFAULT 0,retry_due REAL,retry_deadline REAL,retry_policy TEXT NOT NULL DEFAULT '{}',retry_fingerprint TEXT);
CREATE TABLE knowledge_snapshots(baseline TEXT PRIMARY KEY REFERENCES baselines(id),project TEXT NOT NULL REFERENCES projects(id),blob TEXT NOT NULL,git_commit TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE links (
 source TEXT NOT NULL REFERENCES artifacts(id), target TEXT NOT NULL REFERENCES artifacts(id),
 relation TEXT NOT NULL, confidence TEXT NOT NULL CHECK(confidence IN ('asserted','inferred')),
 basis TEXT NOT NULL, PRIMARY KEY(source,target,relation)
);
CREATE TABLE local_execution_packets(
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id),
 proposal TEXT NOT NULL REFERENCES local_execution_proposals(id), ordinal INTEGER NOT NULL,
 digest TEXT NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)),
 UNIQUE(proposal,ordinal)
);
CREATE TABLE local_execution_proposals(
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id),
 program TEXT NOT NULL REFERENCES programs(id), subplan TEXT NOT NULL REFERENCES subplans(id),
 digest TEXT NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), created REAL NOT NULL
);
CREATE TABLE local_execution_records(
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id),
 proposal TEXT NOT NULL REFERENCES local_execution_proposals(id),
 task TEXT REFERENCES tasks(id), epoch INTEGER, kind TEXT NOT NULL
 CHECK(kind IN ('certified','withdrawn','claimed','invalidated')),
 digest TEXT NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), created REAL NOT NULL
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE native_sessions(id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), client TEXT NOT NULL, cwd TEXT NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), updated REAL NOT NULL);
CREATE TABLE native_turns(session TEXT NOT NULL REFERENCES native_sessions(id), turn_id TEXT NOT NULL, source TEXT NOT NULL REFERENCES sources(id), digest TEXT NOT NULL, origin TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(session,turn_id));
CREATE TABLE outbox (
 id TEXT PRIMARY KEY, project TEXT REFERENCES projects(id), kind TEXT NOT NULL, dedup TEXT NOT NULL UNIQUE,
 body TEXT NOT NULL CHECK(json_valid(body)), status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 due REAL NOT NULL, result TEXT CHECK(result IS NULL OR json_valid(result)), created REAL NOT NULL
);
CREATE TABLE plans (
 task TEXT PRIMARY KEY REFERENCES tasks(id), body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, approved TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE policies (
 project TEXT PRIMARY KEY REFERENCES projects(id), revision INTEGER NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL
);
CREATE TABLE profiles (
 project TEXT PRIMARY KEY REFERENCES projects(id), body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL,
 scope TEXT NOT NULL CHECK(json_valid(scope)), created REAL NOT NULL
);
CREATE TABLE program_closures (
 id TEXT PRIMARY KEY, program TEXT NOT NULL REFERENCES programs(id), project TEXT NOT NULL REFERENCES projects(id),
 delivery TEXT NOT NULL REFERENCES deliveries(id), binding TEXT NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), created REAL NOT NULL
);
CREATE TABLE programs (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), phase TEXT NOT NULL,
 revision INTEGER NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), created REAL NOT NULL
);
INSERT INTO "programs" VALUES('FLOW-abb5b36ede2630efca467d92','PRJ-cab2f7a692c564e9c2313e05','requirements',1,'{"history":[],"mode":"greenfield","scope_rule":"all accepted requirements remain in release scope","source":"SRC-d5c5b95580d9be3ef284e8d0"}',1.7896127899866507e+09);
CREATE TABLE projects (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, config TEXT NOT NULL CHECK(json_valid(config)),
 paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0,1)), created REAL NOT NULL
);
INSERT INTO "projects" VALUES('PRJ-cab2f7a692c564e9c2313e05','U4-O legacy source fixture','{}',0,1.78961278996302413e+09);
CREATE TABLE providers(name TEXT PRIMARY KEY,body TEXT NOT NULL,secret_path TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE receipts (
 id TEXT PRIMARY KEY, run TEXT NOT NULL REFERENCES runs(id), project TEXT NOT NULL REFERENCES projects(id), subject TEXT NOT NULL,
 role TEXT NOT NULL, binding TEXT NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), key_id TEXT NOT NULL,
 mac TEXT NOT NULL, created REAL NOT NULL, UNIQUE(run)
);
CREATE TABLE remote_receipts(id TEXT PRIMARY KEY,project TEXT NOT NULL,delivery TEXT NOT NULL,repo TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE remote_targets(project TEXT NOT NULL,repo TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(project,repo));
CREATE TABLE repos (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), name TEXT NOT NULL,
 path TEXT NOT NULL, head TEXT, UNIQUE(project,name)
);
CREATE TABLE requests (
 actor TEXT NOT NULL, id TEXT NOT NULL, request_digest TEXT NOT NULL,
 result TEXT NOT NULL CHECK(json_valid(result)), created REAL NOT NULL, PRIMARY KEY(actor,id)
);
CREATE TABLE review_scopes(id TEXT PRIMARY KEY,program TEXT NOT NULL REFERENCES programs(id),project TEXT NOT NULL REFERENCES projects(id),phase TEXT NOT NULL,body TEXT NOT NULL CHECK(json_valid(body)),digest TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE revisions (
 artifact TEXT NOT NULL REFERENCES artifacts(id), revision INTEGER NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, status TEXT NOT NULL,
 reason TEXT NOT NULL, actor TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(artifact,revision)
);
INSERT INTO "revisions" VALUES('REQUIREMENT-21c38e93425826220bf2521c',1,'{"acceptance":["AC-ORIGIN"],"source_refs":["SRC-d5c5b95580d9be3ef284e8d0"],"statement":"Retain the original program origin","title":"Origin migration fixture"}','c68e6ad0edc4d08be0e4f5e6f5a523f4348be25d1f1824902531d0f63a8bfdb5','draft','initial proposal','local-user',1.7896127899750266e+09);
CREATE TABLE runs (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), task TEXT REFERENCES tasks(id), subject TEXT NOT NULL,
 role TEXT NOT NULL, adapter TEXT NOT NULL, status TEXT NOT NULL, binding TEXT NOT NULL,
 epoch INTEGER, worker_uid INTEGER, pid INTEGER, start REAL NOT NULL, end REAL,
 body TEXT NOT NULL CHECK(json_valid(body)), result TEXT CHECK(result IS NULL OR json_valid(result))
);
CREATE TABLE scope_return_packets(
 id TEXT PRIMARY KEY, proposal TEXT NOT NULL REFERENCES scope_returns(id), project TEXT NOT NULL REFERENCES projects(id),
 level INTEGER NOT NULL, ordinal INTEGER NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, created REAL NOT NULL,
 UNIQUE(proposal,digest)
);
CREATE TABLE scope_returns(
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), scope TEXT NOT NULL REFERENCES workstreams(id),
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('proposed','applied','abandoned')), result TEXT CHECK(result IS NULL OR json_valid(result)), created REAL NOT NULL
);
CREATE TABLE sources (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), blob TEXT NOT NULL,
 locator TEXT NOT NULL, characters INTEGER NOT NULL, actor TEXT NOT NULL,
 trust TEXT NOT NULL CHECK(trust IN ('human','agent','external')), created REAL NOT NULL
);
INSERT INTO "sources" VALUES('SRC-d5c5b95580d9be3ef284e8d0','PRJ-cab2f7a692c564e9c2313e05','81564d280f95bb0569ea8aa05979c7e819c20d1daeef81872fedd23544d7f7ee','conversation',54,'local-user','human',1.78961278997087907e+09);
INSERT INTO "sources" VALUES('SRC-4a54381b1edb5a268eeac8a3','PRJ-cab2f7a692c564e9c2313e05','446ab594408d6a0443995d56a5023db77960733ac33fb5f493535a51614217fc','conversation',50,'local-user','human',1.78961278999326777e+09);
CREATE TABLE subplan_compositions(
 id TEXT PRIMARY KEY, subplan TEXT NOT NULL REFERENCES subplans(id), project TEXT NOT NULL REFERENCES projects(id),
 breakdown TEXT NOT NULL REFERENCES breakdowns(id), request_digest TEXT NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, created REAL NOT NULL,
 UNIQUE(subplan,request_digest)
);
CREATE TABLE subplan_packets(
 id TEXT PRIMARY KEY, subplan TEXT NOT NULL REFERENCES subplans(id), project TEXT NOT NULL REFERENCES projects(id),
 ordinal INTEGER NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL,
 UNIQUE(subplan,ordinal)
);
CREATE TABLE subplans(
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id),
 program TEXT NOT NULL REFERENCES programs(id), body TEXT NOT NULL CHECK(json_valid(body)),
 digest TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE supervisor_views (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL REFERENCES projects(id), method TEXT NOT NULL,
 request_digest TEXT NOT NULL, response_digest TEXT NOT NULL, receipt TEXT NOT NULL REFERENCES receipts(id), created REAL NOT NULL,
 UNIQUE(project,method,request_digest,response_digest)
);
CREATE TABLE task_deps (
 task TEXT NOT NULL REFERENCES tasks(id), dependency TEXT NOT NULL REFERENCES tasks(id), PRIMARY KEY(task,dependency), CHECK(task!=dependency)
);
CREATE TABLE task_reads (
 task TEXT NOT NULL REFERENCES tasks(id), artifact TEXT NOT NULL REFERENCES artifacts(id), revision INTEGER NOT NULL,
 digest TEXT NOT NULL, PRIMARY KEY(task,artifact)
);
CREATE TABLE task_revision_history (
 id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id),
 project TEXT NOT NULL REFERENCES projects(id), from_revision INTEGER NOT NULL,
 to_revision INTEGER NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)),
 digest TEXT NOT NULL, created REAL NOT NULL, UNIQUE(task,to_revision),
 CHECK(to_revision=from_revision+1)
);
CREATE TABLE task_revision_proposals (
 id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id),
 project TEXT NOT NULL REFERENCES projects(id), from_revision INTEGER NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL,
 binding TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('proposed','applied','withdrawn')),
 result TEXT CHECK(result IS NULL OR json_valid(result)), created REAL NOT NULL
);
CREATE TABLE tasks (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), body TEXT NOT NULL CHECK(json_valid(body)),
 revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL CHECK(status IN ('planned','ready','running','submitted','completed','cancelled')),
 validity TEXT NOT NULL CHECK(validity IN ('current','needs_review','invalid')), epoch INTEGER NOT NULL DEFAULT 0,
 lease_owner TEXT, lease_until REAL, candidate TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 no_progress_count INTEGER NOT NULL DEFAULT 0,
 paused INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL
);
CREATE TABLE timers (
 id TEXT PRIMARY KEY, project TEXT REFERENCES projects(id), kind TEXT NOT NULL, ref TEXT NOT NULL,
 due REAL NOT NULL, fired REAL, UNIQUE(kind,ref)
);
CREATE TABLE tokens (
 id TEXT PRIMARY KEY, hash TEXT NOT NULL UNIQUE, actor TEXT NOT NULL, role TEXT NOT NULL,
 project TEXT REFERENCES projects(id), task TEXT REFERENCES tasks(id), expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE traceability_bindings (
 id TEXT PRIMARY KEY,
 project TEXT NOT NULL REFERENCES projects(id),
 revision TEXT NOT NULL REFERENCES traceability_revisions(id),
 body TEXT NOT NULL CHECK(json_valid(body)),
 digest TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','mandatory','stale','withdrawn')),
 created REAL NOT NULL
);
CREATE TABLE traceability_decisions (
 id TEXT PRIMARY KEY,
 revision TEXT NOT NULL REFERENCES traceability_revisions(id),
 project TEXT NOT NULL REFERENCES projects(id),
 body TEXT NOT NULL CHECK(json_valid(body)),
 digest TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('proposed','accepted','rejected','stale')),
 created REAL NOT NULL
);
CREATE TABLE traceability_items (
 id TEXT PRIMARY KEY,
 revision TEXT NOT NULL REFERENCES traceability_revisions(id),
 project TEXT NOT NULL REFERENCES projects(id),
 ordinal INTEGER NOT NULL CHECK(ordinal>=0),
 item_kind TEXT NOT NULL CHECK(item_kind IN ('file','atom','symbol','line','group')),
 path TEXT,
 status TEXT NOT NULL CHECK(status IN ('known','unknown','tombstone')),
 start_byte INTEGER NOT NULL CHECK(start_byte>=0),
 end_byte INTEGER NOT NULL CHECK(end_byte>=start_byte),
 body TEXT NOT NULL CHECK(json_valid(body)),
 digest TEXT NOT NULL,
 leaf INTEGER NOT NULL DEFAULT 0 CHECK(leaf IN (0,1)),
 UNIQUE(revision,ordinal), UNIQUE(revision,digest)
);
CREATE TABLE traceability_mappings (
 id TEXT PRIMARY KEY,
 revision TEXT NOT NULL REFERENCES traceability_revisions(id),
 project TEXT NOT NULL REFERENCES projects(id),
 body TEXT NOT NULL CHECK(json_valid(body)),
 digest TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('proposed','accepted','stale')),
 created REAL NOT NULL
);
CREATE TABLE traceability_proposals (
 id TEXT PRIMARY KEY,
 set_id TEXT NOT NULL REFERENCES traceability_sets(id),
 project TEXT NOT NULL REFERENCES projects(id),
 kind TEXT NOT NULL CHECK(kind IN ('population','code','document','decision','mapping','scope')),
 status TEXT NOT NULL CHECK(status IN ('proposed','staging','ready','failed','adopted','withdrawn')),
 body TEXT NOT NULL CHECK(json_valid(body)),
 digest TEXT NOT NULL,
 expected_active TEXT,
 semantic_material_digest TEXT NOT NULL,
 result TEXT CHECK(result IS NULL OR json_valid(result)),
 created REAL NOT NULL
);
CREATE TABLE traceability_records (
 id TEXT PRIMARY KEY,
 project TEXT NOT NULL REFERENCES projects(id),
 revision TEXT REFERENCES traceability_revisions(id),
 proposal TEXT REFERENCES traceability_proposals(id),
 kind TEXT NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)),
 digest TEXT NOT NULL,
 created REAL NOT NULL
);
CREATE TABLE traceability_revisions (
 id TEXT PRIMARY KEY,
 set_id TEXT NOT NULL REFERENCES traceability_sets(id),
 project TEXT NOT NULL REFERENCES projects(id),
 revision INTEGER NOT NULL CHECK(revision>0),
 status TEXT NOT NULL CHECK(status IN ('staging','failed','ready','active','superseded')),
 body TEXT NOT NULL CHECK(json_valid(body)),
 digest TEXT NOT NULL,
 population_digest TEXT NOT NULL,
 adapter TEXT NOT NULL,
 created REAL NOT NULL,
 UNIQUE(set_id,revision), UNIQUE(set_id,digest)
);
CREATE TABLE traceability_sets (
 id TEXT PRIMARY KEY,
 project TEXT NOT NULL REFERENCES projects(id),
 name TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('population','code','document')),
 active_revision TEXT REFERENCES traceability_revisions(id),
 active_digest TEXT,
 created REAL NOT NULL,
 UNIQUE(project,name)
);
CREATE TABLE waivers (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), subject TEXT NOT NULL, criterion TEXT NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), status TEXT NOT NULL, expires REAL NOT NULL, created REAL NOT NULL
);
CREATE TABLE workstream_packets(
 id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES workstreams(id), ordinal INTEGER NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, UNIQUE(scope,ordinal)
);
CREATE TABLE workstream_records(
 id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES workstreams(id), project TEXT NOT NULL REFERENCES projects(id),
 kind TEXT NOT NULL CHECK(kind IN ('adopt','finish','withdraw')),
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE workstreams(
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id),
 program TEXT NOT NULL REFERENCES programs(id), parent TEXT REFERENCES workstreams(id),
 previous TEXT REFERENCES workstreams(id), breakdown TEXT NOT NULL REFERENCES breakdowns(id),
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('proposed','active','superseded','withdrawn')), created REAL NOT NULL
);
CREATE INDEX links_target ON links(target,relation);
CREATE INDEX tasks_queue ON tasks(project,status,validity,paused,created);
CREATE INDEX task_reads_artifact ON task_reads(artifact);
CREATE INDEX task_deps_dependency ON task_deps(dependency);
CREATE INDEX runs_subject ON runs(subject,role,status);
CREATE INDEX receipts_subject ON receipts(subject,role,binding);
CREATE TRIGGER revisions_no_update BEFORE UPDATE ON revisions BEGIN SELECT RAISE(ABORT,'immutable revision'); END;
CREATE TRIGGER revisions_no_delete BEFORE DELETE ON revisions BEGIN SELECT RAISE(ABORT,'immutable revision'); END;
CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'immutable event'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'immutable event'); END;
CREATE TRIGGER receipts_no_update BEFORE UPDATE ON receipts BEGIN SELECT RAISE(ABORT,'immutable receipt'); END;
CREATE TRIGGER receipts_no_delete BEFORE DELETE ON receipts BEGIN SELECT RAISE(ABORT,'immutable receipt'); END;
CREATE INDEX jobs_ready ON jobs(status,created);
CREATE INDEX review_scopes_program ON review_scopes(program,phase,status);
CREATE INDEX native_sessions_workspace ON native_sessions(cwd,updated);
CREATE INDEX native_turns_source ON native_turns(session,source);
CREATE INDEX execution_usage_project ON execution_usage(project,status);
CREATE UNIQUE INDEX breakdowns_active ON breakdowns(program) WHERE status='active';
CREATE INDEX breakdown_members_packet ON breakdown_members(packet);
CREATE INDEX program_closures_program ON program_closures(program,created);
CREATE TRIGGER breakdowns_no_rewrite BEFORE UPDATE OF body,digest,program,project,previous ON breakdowns BEGIN SELECT RAISE(ABORT,'immutable breakdown proposal'); END;
CREATE TRIGGER breakdown_packets_no_rewrite BEFORE UPDATE ON breakdown_packets BEGIN SELECT RAISE(ABORT,'immutable breakdown packet'); END;
CREATE TRIGGER program_closures_no_rewrite BEFORE UPDATE ON program_closures BEGIN SELECT RAISE(ABORT,'immutable workflow closure'); END;
CREATE INDEX supervisor_views_project ON supervisor_views(project,seq);
CREATE INDEX traceability_revisions_project ON traceability_revisions(project,set_id,revision);
CREATE INDEX traceability_items_page ON traceability_items(revision,ordinal,id);
CREATE INDEX traceability_items_path ON traceability_items(revision,path,ordinal);
CREATE INDEX traceability_items_leaf ON traceability_items(revision,leaf,status);
CREATE INDEX traceability_proposals_project ON traceability_proposals(project,set_id,created,id);
CREATE INDEX traceability_records_project ON traceability_records(project,created,id);
CREATE TRIGGER traceability_sets_immutable
 BEFORE UPDATE OF project,name,kind,created ON traceability_sets
 BEGIN SELECT RAISE(ABORT,'immutable traceability set'); END;
CREATE TRIGGER traceability_sets_no_delete
 BEFORE DELETE ON traceability_sets BEGIN SELECT RAISE(ABORT,'retain traceability sets'); END;
CREATE TRIGGER traceability_revisions_immutable
 BEFORE UPDATE OF set_id,project,revision,body,digest,population_digest,adapter,created ON traceability_revisions
 BEGIN SELECT RAISE(ABORT,'immutable traceability revision'); END;
CREATE TRIGGER traceability_revisions_no_delete
 BEFORE DELETE ON traceability_revisions BEGIN SELECT RAISE(ABORT,'retain traceability revisions'); END;
CREATE TRIGGER traceability_items_immutable
 BEFORE UPDATE ON traceability_items BEGIN SELECT RAISE(ABORT,'immutable traceability item'); END;
CREATE TRIGGER traceability_items_no_delete
 BEFORE DELETE ON traceability_items BEGIN SELECT RAISE(ABORT,'retain traceability items'); END;
CREATE TRIGGER traceability_proposals_immutable
 BEFORE UPDATE OF set_id,project,kind,body,digest,expected_active,semantic_material_digest,created ON traceability_proposals
 BEGIN SELECT RAISE(ABORT,'immutable traceability proposal'); END;
CREATE TRIGGER traceability_proposals_no_delete
 BEFORE DELETE ON traceability_proposals BEGIN SELECT RAISE(ABORT,'retain traceability proposals'); END;
CREATE TRIGGER traceability_decisions_immutable
 BEFORE UPDATE ON traceability_decisions BEGIN SELECT RAISE(ABORT,'immutable traceability decision'); END;
CREATE TRIGGER traceability_decisions_no_delete
 BEFORE DELETE ON traceability_decisions BEGIN SELECT RAISE(ABORT,'retain traceability decisions'); END;
CREATE TRIGGER traceability_mappings_immutable
 BEFORE UPDATE ON traceability_mappings BEGIN SELECT RAISE(ABORT,'immutable traceability mapping'); END;
CREATE TRIGGER traceability_mappings_no_delete
 BEFORE DELETE ON traceability_mappings BEGIN SELECT RAISE(ABORT,'retain traceability mappings'); END;
CREATE TRIGGER traceability_bindings_immutable
 BEFORE UPDATE ON traceability_bindings BEGIN SELECT RAISE(ABORT,'immutable traceability binding'); END;
CREATE TRIGGER traceability_bindings_no_delete
 BEFORE DELETE ON traceability_bindings BEGIN SELECT RAISE(ABORT,'retain traceability bindings'); END;
CREATE TRIGGER traceability_records_immutable
 BEFORE UPDATE ON traceability_records BEGIN SELECT RAISE(ABORT,'immutable traceability record'); END;
CREATE TRIGGER traceability_records_no_delete
 BEFORE DELETE ON traceability_records BEGIN SELECT RAISE(ABORT,'retain traceability records'); END;
CREATE INDEX task_revision_proposals_task ON task_revision_proposals(task,created);
CREATE INDEX task_revision_history_task ON task_revision_history(task,to_revision);
CREATE TRIGGER task_revision_proposals_immutable
 BEFORE UPDATE OF task,project,from_revision,body,digest,binding,created ON task_revision_proposals
 BEGIN SELECT RAISE(ABORT,'immutable task revision proposal'); END;
CREATE TRIGGER task_revision_history_no_update BEFORE UPDATE ON task_revision_history
 BEGIN SELECT RAISE(ABORT,'immutable task definition history'); END;
CREATE TRIGGER task_revision_history_no_delete BEFORE DELETE ON task_revision_history
 BEGIN SELECT RAISE(ABORT,'immutable task definition history'); END;
CREATE INDEX workstreams_program ON workstreams(program,status,parent);
CREATE INDEX workstream_records_scope ON workstream_records(scope,kind,created);
CREATE TRIGGER workstreams_immutable BEFORE UPDATE OF project,program,parent,previous,breakdown,body,digest,created ON workstreams
 BEGIN SELECT RAISE(ABORT,'immutable delegated scope'); END;
CREATE TRIGGER workstream_packets_immutable BEFORE UPDATE ON workstream_packets
 BEGIN SELECT RAISE(ABORT,'immutable scope review packet'); END;
CREATE TRIGGER workstream_records_immutable BEFORE UPDATE ON workstream_records
 BEGIN SELECT RAISE(ABORT,'immutable delegated scope history'); END;
CREATE TRIGGER workstream_records_no_delete BEFORE DELETE ON workstream_records
 BEGIN SELECT RAISE(ABORT,'immutable delegated scope history'); END;
CREATE INDEX scope_returns_scope ON scope_returns(scope,status,created);
CREATE INDEX scope_return_packets_proposal ON scope_return_packets(proposal,level,ordinal);
CREATE TRIGGER scope_returns_body_immutable BEFORE UPDATE OF project,scope,body,digest,created ON scope_returns
 BEGIN SELECT RAISE(ABORT,'immutable scope return proposal'); END;
CREATE TRIGGER scope_returns_no_delete BEFORE DELETE ON scope_returns
 BEGIN SELECT RAISE(ABORT,'retain scope return history'); END;
CREATE TRIGGER scope_return_packets_immutable BEFORE UPDATE ON scope_return_packets
 BEGIN SELECT RAISE(ABORT,'immutable scope return review'); END;
CREATE TRIGGER scope_return_packets_no_delete BEFORE DELETE ON scope_return_packets
 BEGIN SELECT RAISE(ABORT,'retain scope return reviews'); END;
CREATE INDEX subplans_program ON subplans(program,created,id);
CREATE TRIGGER subplans_immutable BEFORE UPDATE ON subplans
 BEGIN SELECT RAISE(ABORT,'immutable partial design'); END;
CREATE TRIGGER subplans_no_delete BEFORE DELETE ON subplans
 BEGIN SELECT RAISE(ABORT,'retain partial design history'); END;
CREATE TRIGGER subplan_packets_immutable BEFORE UPDATE ON subplan_packets
 BEGIN SELECT RAISE(ABORT,'immutable partial review'); END;
CREATE TRIGGER subplan_packets_no_delete BEFORE DELETE ON subplan_packets
 BEGIN SELECT RAISE(ABORT,'retain partial review'); END;
CREATE TRIGGER subplan_compositions_immutable BEFORE UPDATE ON subplan_compositions
 BEGIN SELECT RAISE(ABORT,'immutable composition history'); END;
CREATE TRIGGER subplan_compositions_no_delete BEFORE DELETE ON subplan_compositions
 BEGIN SELECT RAISE(ABORT,'retain composition history'); END;
CREATE INDEX local_execution_proposals_program
 ON local_execution_proposals(program,created,id);
CREATE INDEX local_execution_packets_proposal
 ON local_execution_packets(proposal,ordinal);
CREATE INDEX local_execution_records_proposal
 ON local_execution_records(proposal,created,id);
CREATE INDEX local_execution_records_task
 ON local_execution_records(task,epoch,kind,created,id);
CREATE UNIQUE INDEX local_execution_records_certified
 ON local_execution_records(proposal) WHERE kind='certified';
CREATE UNIQUE INDEX local_execution_records_withdrawn
 ON local_execution_records(proposal) WHERE kind='withdrawn';
CREATE UNIQUE INDEX local_execution_records_task_event
 ON local_execution_records(proposal,task,epoch,kind) WHERE task IS NOT NULL;
CREATE TRIGGER local_execution_proposals_immutable
 BEFORE UPDATE ON local_execution_proposals
 BEGIN SELECT RAISE(ABORT,'immutable local execution proposal'); END;
CREATE TRIGGER local_execution_proposals_no_delete
 BEFORE DELETE ON local_execution_proposals
 BEGIN SELECT RAISE(ABORT,'retain local execution proposal history'); END;
CREATE TRIGGER local_execution_packets_immutable
 BEFORE UPDATE ON local_execution_packets
 BEGIN SELECT RAISE(ABORT,'immutable local execution packet'); END;
CREATE TRIGGER local_execution_packets_no_delete
 BEFORE DELETE ON local_execution_packets
 BEGIN SELECT RAISE(ABORT,'retain local execution packet history'); END;
CREATE TRIGGER local_execution_records_immutable
 BEFORE UPDATE ON local_execution_records
 BEGIN SELECT RAISE(ABORT,'immutable local execution record'); END;
CREATE TRIGGER local_execution_records_no_delete
 BEFORE DELETE ON local_execution_records
 BEGIN SELECT RAISE(ABORT,'retain local execution record history'); END;
CREATE TRIGGER local_execution_records_shape
 BEFORE INSERT ON local_execution_records
 WHEN (NEW.kind IN ('certified','withdrawn') AND (NEW.task IS NOT NULL OR NEW.epoch IS NOT NULL))
   OR (NEW.kind IN ('claimed','invalidated') AND (NEW.task IS NULL OR NEW.epoch IS NULL OR NEW.epoch < 0))
 BEGIN SELECT RAISE(ABORT,'invalid local execution record shape'); END;
CREATE INDEX execution_attempts_project ON execution_attempts(project,task,attempt_ordinal);
CREATE INDEX attempt_assessments_task ON attempt_assessments(task,attempt_ordinal,created);
CREATE INDEX execution_control_proposals_task ON execution_control_proposals(task,created,id);
CREATE INDEX execution_control_packets_proposal ON execution_control_packets(proposal,ordinal);
CREATE INDEX execution_control_events_proposal ON execution_control_events(proposal,created,id);
CREATE INDEX execution_control_authorizations_task ON execution_control_authorizations(task,created,id);
CREATE UNIQUE INDEX execution_control_authorizations_proposal ON execution_control_authorizations(proposal);
CREATE TRIGGER execution_attempts_identity_immutable
 BEFORE UPDATE OF task,project,attempt_epoch,attempt_ordinal,task_revision,task_binding,body,digest,created
 ON execution_attempts BEGIN SELECT RAISE(ABORT,'immutable execution attempt identity'); END;
CREATE TRIGGER execution_attempts_no_delete
 BEFORE DELETE ON execution_attempts BEGIN SELECT RAISE(ABORT,'retain execution attempt history'); END;
CREATE TRIGGER attempt_assessments_immutable
 BEFORE UPDATE ON attempt_assessments BEGIN SELECT RAISE(ABORT,'immutable attempt assessment'); END;
CREATE TRIGGER attempt_assessments_no_delete
 BEFORE DELETE ON attempt_assessments BEGIN SELECT RAISE(ABORT,'retain attempt assessment history'); END;
CREATE TRIGGER execution_control_proposals_identity_immutable
 BEFORE UPDATE OF task,project,task_revision,body,digest,binding,created
 ON execution_control_proposals BEGIN SELECT RAISE(ABORT,'immutable execution control proposal'); END;
CREATE TRIGGER execution_control_proposals_no_delete
 BEFORE DELETE ON execution_control_proposals BEGIN SELECT RAISE(ABORT,'retain execution control proposal history'); END;
CREATE TRIGGER execution_control_packets_immutable
 BEFORE UPDATE ON execution_control_packets BEGIN SELECT RAISE(ABORT,'immutable execution control packet'); END;
CREATE TRIGGER execution_control_packets_no_delete
 BEFORE DELETE ON execution_control_packets BEGIN SELECT RAISE(ABORT,'retain execution control packet history'); END;
CREATE TRIGGER execution_control_events_immutable
 BEFORE UPDATE ON execution_control_events BEGIN SELECT RAISE(ABORT,'immutable execution control event'); END;
CREATE TRIGGER execution_control_events_no_delete
 BEFORE DELETE ON execution_control_events BEGIN SELECT RAISE(ABORT,'retain execution control event history'); END;
CREATE TRIGGER execution_control_authorizations_immutable
 BEFORE UPDATE ON execution_control_authorizations BEGIN SELECT RAISE(ABORT,'immutable execution control authorization'); END;
CREATE TRIGGER execution_control_authorizations_no_delete
 BEFORE DELETE ON execution_control_authorizations BEGIN SELECT RAISE(ABORT,'retain execution control authorization history'); END;
CREATE INDEX assurance_objects_project_kind ON assurance_objects(project,kind,logical_id,revision);
CREATE INDEX assurance_objects_digest ON assurance_objects(project,digest);
CREATE INDEX assurance_events_subject ON assurance_events(project,subject_id,created,id);
CREATE INDEX assurance_events_project ON assurance_events(project,created,id);
CREATE INDEX assurance_heads_event ON assurance_heads(head_event);
CREATE INDEX assurance_refs_lookup ON assurance_refs(ref_kind,ref_id,ref_revision,ref_digest);
CREATE INDEX assurance_refs_object ON assurance_refs(object_id,ordinal);
CREATE TRIGGER assurance_objects_no_update
 BEFORE UPDATE ON assurance_objects BEGIN SELECT RAISE(ABORT,'immutable assurance object'); END;
CREATE TRIGGER assurance_objects_no_delete
 BEFORE DELETE ON assurance_objects BEGIN SELECT RAISE(ABORT,'immutable assurance object'); END;
CREATE TRIGGER assurance_events_no_update
 BEFORE UPDATE ON assurance_events BEGIN SELECT RAISE(ABORT,'immutable assurance event'); END;
CREATE TRIGGER assurance_events_no_delete
 BEFORE DELETE ON assurance_events BEGIN SELECT RAISE(ABORT,'immutable assurance event'); END;
CREATE TRIGGER assurance_refs_no_update
 BEFORE UPDATE ON assurance_refs BEGIN SELECT RAISE(ABORT,'immutable assurance reference'); END;
CREATE TRIGGER assurance_refs_no_delete
 BEFORE DELETE ON assurance_refs BEGIN SELECT RAISE(ABORT,'immutable assurance reference'); END;
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('events',8);
COMMIT;
