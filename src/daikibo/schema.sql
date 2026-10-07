CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projects (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, config TEXT NOT NULL CHECK(json_valid(config)),
 paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0,1)), created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS repos (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), name TEXT NOT NULL,
 path TEXT NOT NULL, head TEXT, UNIQUE(project,name)
);
CREATE TABLE IF NOT EXISTS sources (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), blob TEXT NOT NULL,
 locator TEXT NOT NULL, characters INTEGER NOT NULL, actor TEXT NOT NULL,
 trust TEXT NOT NULL CHECK(trust IN ('human','agent','external')), created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS dispositions (
 id TEXT PRIMARY KEY, source TEXT NOT NULL REFERENCES sources(id), start INTEGER NOT NULL,
 end INTEGER NOT NULL CHECK(end>start), category TEXT NOT NULL,
 refs TEXT NOT NULL CHECK(json_valid(refs)), reason TEXT NOT NULL, actor TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), kind TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision>0), status TEXT NOT NULL CHECK(status IN ('draft','accepted','superseded','withdrawn')),
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, owner TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
 artifact TEXT NOT NULL REFERENCES artifacts(id), revision INTEGER NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, status TEXT NOT NULL,
 reason TEXT NOT NULL, actor TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(artifact,revision)
);
CREATE TABLE IF NOT EXISTS links (
 source TEXT NOT NULL REFERENCES artifacts(id), target TEXT NOT NULL REFERENCES artifacts(id),
 relation TEXT NOT NULL, confidence TEXT NOT NULL CHECK(confidence IN ('asserted','inferred')),
 basis TEXT NOT NULL, PRIMARY KEY(source,target,relation)
);
CREATE INDEX IF NOT EXISTS links_target ON links(target,relation);
CREATE TABLE IF NOT EXISTS baselines (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), body TEXT NOT NULL CHECK(json_valid(body)),
 digest TEXT NOT NULL, git_commit TEXT, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), revision INTEGER NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, status TEXT NOT NULL,
 response TEXT, source TEXT REFERENCES sources(id), consistency_receipt TEXT, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS decision_batches (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id),
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('prepared','applied')),
 result TEXT CHECK(result IS NULL OR json_valid(result)), created REAL NOT NULL, applied REAL
);
CREATE INDEX IF NOT EXISTS decision_batches_project ON decision_batches(project,created,id);
CREATE TRIGGER IF NOT EXISTS decision_batches_immutable BEFORE UPDATE OF project,body,digest,created ON decision_batches
 BEGIN SELECT RAISE(ABORT,'immutable decision batch packet'); END;
CREATE TRIGGER IF NOT EXISTS decision_batches_no_delete BEFORE DELETE ON decision_batches
 BEGIN SELECT RAISE(ABORT,'retain decision batch history'); END;
CREATE TABLE IF NOT EXISTS inbox (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), kind TEXT NOT NULL, ref TEXT NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), severity TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
 displayed INTEGER NOT NULL DEFAULT 0, due REAL, created REAL NOT NULL, UNIQUE(project,kind,ref)
);
CREATE TABLE IF NOT EXISTS changes (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), stage TEXT NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), revision INTEGER NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
 id TEXT PRIMARY KEY, change_id TEXT NOT NULL REFERENCES changes(id), level TEXT NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS conflicts (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), body TEXT NOT NULL CHECK(json_valid(body)),
 status TEXT NOT NULL, decision TEXT, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), body TEXT NOT NULL CHECK(json_valid(body)),
 revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL CHECK(status IN ('planned','ready','running','submitted','completed','cancelled')),
 validity TEXT NOT NULL CHECK(validity IN ('current','needs_review','invalid')), epoch INTEGER NOT NULL DEFAULT 0,
 lease_owner TEXT, lease_until REAL, candidate TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 no_progress_count INTEGER NOT NULL DEFAULT 0,
 paused INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_queue ON tasks(project,status,validity,paused,created);
CREATE TABLE IF NOT EXISTS task_reads (
 task TEXT NOT NULL REFERENCES tasks(id), artifact TEXT NOT NULL REFERENCES artifacts(id), revision INTEGER NOT NULL,
 digest TEXT NOT NULL, PRIMARY KEY(task,artifact)
);
CREATE INDEX IF NOT EXISTS task_reads_artifact ON task_reads(artifact);
CREATE TABLE IF NOT EXISTS task_deps (
 task TEXT NOT NULL REFERENCES tasks(id), dependency TEXT NOT NULL REFERENCES tasks(id), PRIMARY KEY(task,dependency), CHECK(task!=dependency)
);
CREATE INDEX IF NOT EXISTS task_deps_dependency ON task_deps(dependency);
CREATE TABLE IF NOT EXISTS blocks (
 task TEXT NOT NULL REFERENCES tasks(id), kind TEXT NOT NULL, ref TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(task,kind,ref)
);
CREATE TABLE IF NOT EXISTS plans (
 task TEXT PRIMARY KEY REFERENCES tasks(id), body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, approved TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), task TEXT REFERENCES tasks(id), subject TEXT NOT NULL,
 role TEXT NOT NULL, adapter TEXT NOT NULL, status TEXT NOT NULL, binding TEXT NOT NULL,
 epoch INTEGER, worker_uid INTEGER, pid INTEGER, start REAL NOT NULL, end REAL,
 body TEXT NOT NULL CHECK(json_valid(body)), result TEXT CHECK(result IS NULL OR json_valid(result))
);
CREATE INDEX IF NOT EXISTS runs_subject ON runs(subject,role,status);
CREATE TABLE IF NOT EXISTS receipts (
 id TEXT PRIMARY KEY, run TEXT NOT NULL REFERENCES runs(id), project TEXT NOT NULL REFERENCES projects(id), subject TEXT NOT NULL,
 role TEXT NOT NULL, binding TEXT NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), key_id TEXT NOT NULL,
 mac TEXT NOT NULL, created REAL NOT NULL, UNIQUE(run)
);
CREATE INDEX IF NOT EXISTS receipts_subject ON receipts(subject,role,binding);
CREATE TABLE IF NOT EXISTS candidates (
 id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id), epoch INTEGER NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, implementation_run TEXT NOT NULL REFERENCES runs(id), created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS gate_results (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), subject TEXT NOT NULL, gate TEXT NOT NULL,
 binding TEXT NOT NULL, policy_digest TEXT NOT NULL, verdict TEXT NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS policies (
 project TEXT PRIMARY KEY REFERENCES projects(id), revision INTEGER NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS waivers (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), subject TEXT NOT NULL, criterion TEXT NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), status TEXT NOT NULL, expires REAL NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
 id TEXT PRIMARY KEY, hash TEXT NOT NULL UNIQUE, actor TEXT NOT NULL, role TEXT NOT NULL,
 project TEXT REFERENCES projects(id), task TEXT REFERENCES tasks(id), expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS outbox (
 id TEXT PRIMARY KEY, project TEXT REFERENCES projects(id), kind TEXT NOT NULL, dedup TEXT NOT NULL UNIQUE,
 body TEXT NOT NULL CHECK(json_valid(body)), status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 due REAL NOT NULL, result TEXT CHECK(result IS NULL OR json_valid(result)), created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS timers (
 id TEXT PRIMARY KEY, project TEXT REFERENCES projects(id), kind TEXT NOT NULL, ref TEXT NOT NULL,
 due REAL NOT NULL, fired REAL, UNIQUE(kind,ref)
);
CREATE TABLE IF NOT EXISTS requests (
 actor TEXT NOT NULL, id TEXT NOT NULL, request_digest TEXT NOT NULL,
 result TEXT NOT NULL CHECK(json_valid(result)), created REAL NOT NULL, PRIMARY KEY(actor,id)
);
CREATE TABLE IF NOT EXISTS events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, project TEXT, kind TEXT NOT NULL,
 actor TEXT NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), created REAL NOT NULL,
 previous TEXT NOT NULL, key_id TEXT NOT NULL, mac TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profiles (
 project TEXT PRIMARY KEY REFERENCES projects(id), body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL,
 scope TEXT NOT NULL CHECK(json_valid(scope)), created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), body TEXT NOT NULL CHECK(json_valid(body)),
 digest TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS adapters (
 name TEXT PRIMARY KEY, body TEXT NOT NULL CHECK(json_valid(body)), qualified INTEGER NOT NULL DEFAULT 0, receipt TEXT
);
CREATE TABLE IF NOT EXISTS contexts (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), subject TEXT NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, created REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS revisions_no_update BEFORE UPDATE ON revisions BEGIN SELECT RAISE(ABORT,'immutable revision'); END;
CREATE TRIGGER IF NOT EXISTS revisions_no_delete BEFORE DELETE ON revisions BEGIN SELECT RAISE(ABORT,'immutable revision'); END;
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'immutable event'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'immutable event'); END;
CREATE TRIGGER IF NOT EXISTS receipts_no_update BEFORE UPDATE ON receipts BEGIN SELECT RAISE(ABORT,'immutable receipt'); END;
CREATE TRIGGER IF NOT EXISTS receipts_no_delete BEFORE DELETE ON receipts BEGIN SELECT RAISE(ABORT,'immutable receipt'); END;
CREATE TABLE IF NOT EXISTS programs (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), phase TEXT NOT NULL,
 revision INTEGER NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS program_origins (
 program TEXT PRIMARY KEY REFERENCES programs(id),
 project TEXT NOT NULL REFERENCES projects(id),
 digest TEXT NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)),
 UNIQUE(program,project)
);
CREATE INDEX IF NOT EXISTS program_origins_project ON program_origins(project,program);
CREATE TRIGGER IF NOT EXISTS program_origins_no_update BEFORE UPDATE ON program_origins
 BEGIN SELECT RAISE(ABORT,'immutable program origin'); END;
CREATE TRIGGER IF NOT EXISTS program_origins_no_delete BEFORE DELETE ON program_origins
 BEGIN SELECT RAISE(ABORT,'retain program origin history'); END;
CREATE TABLE jobs(id TEXT PRIMARY KEY,project TEXT,kind TEXT NOT NULL,actor TEXT NOT NULL,args TEXT NOT NULL,status TEXT NOT NULL,dedup TEXT UNIQUE,created REAL NOT NULL,started REAL,ended REAL,result TEXT,error TEXT,cancelled INTEGER NOT NULL DEFAULT 0,attempt_count INTEGER NOT NULL DEFAULT 0,retry_due REAL,retry_deadline REAL,retry_policy TEXT NOT NULL DEFAULT '{}',retry_fingerprint TEXT);
CREATE INDEX jobs_ready ON jobs(status,created);
CREATE TABLE automation(project TEXT PRIMARY KEY REFERENCES projects(id),enabled INTEGER NOT NULL,actor TEXT NOT NULL,adapter TEXT NOT NULL,reviewer TEXT NOT NULL,budget REAL NOT NULL,spent REAL NOT NULL DEFAULT 0,concurrency INTEGER NOT NULL,failures INTEGER NOT NULL DEFAULT 0,last_progress REAL NOT NULL,body TEXT NOT NULL);
CREATE TABLE providers(name TEXT PRIMARY KEY,body TEXT NOT NULL,secret_path TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE remote_targets(project TEXT NOT NULL,repo TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(project,repo));
CREATE TABLE remote_receipts(id TEXT PRIMARY KEY,project TEXT NOT NULL,delivery TEXT NOT NULL,repo TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,project TEXT NOT NULL REFERENCES projects(id),body TEXT NOT NULL CHECK(json_valid(body)),status TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS review_scopes(id TEXT PRIMARY KEY,program TEXT NOT NULL REFERENCES programs(id),project TEXT NOT NULL REFERENCES projects(id),phase TEXT NOT NULL,body TEXT NOT NULL CHECK(json_valid(body)),digest TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS review_scopes_program ON review_scopes(program,phase,status);

CREATE TABLE native_sessions(id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), client TEXT NOT NULL, cwd TEXT NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), updated REAL NOT NULL);
CREATE INDEX native_sessions_workspace ON native_sessions(cwd,updated);
CREATE TABLE native_turns(session TEXT NOT NULL REFERENCES native_sessions(id), turn_id TEXT NOT NULL, source TEXT NOT NULL REFERENCES sources(id), digest TEXT NOT NULL, origin TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(session,turn_id));
CREATE INDEX native_turns_source ON native_turns(session,source);

CREATE TABLE IF NOT EXISTS job_attempts(job TEXT NOT NULL REFERENCES jobs(id),attempt INTEGER NOT NULL,status TEXT NOT NULL,started REAL NOT NULL,ended REAL,result TEXT,error TEXT,PRIMARY KEY(job,attempt));
CREATE TABLE IF NOT EXISTS execution_limits(project TEXT PRIMARY KEY REFERENCES projects(id),revision INTEGER NOT NULL,body TEXT NOT NULL CHECK(json_valid(body)),reason TEXT NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS execution_usage(run TEXT PRIMARY KEY REFERENCES runs(id),project TEXT NOT NULL REFERENCES projects(id),adapter TEXT NOT NULL,status TEXT NOT NULL,tokens INTEGER,cost_microusd INTEGER,body TEXT NOT NULL CHECK(json_valid(body)),created REAL NOT NULL,updated REAL);
CREATE INDEX IF NOT EXISTS execution_usage_project ON execution_usage(project,status);
CREATE TABLE IF NOT EXISTS knowledge_snapshots(baseline TEXT PRIMARY KEY REFERENCES baselines(id),project TEXT NOT NULL REFERENCES projects(id),blob TEXT NOT NULL,git_commit TEXT NOT NULL,created REAL NOT NULL);

CREATE TABLE IF NOT EXISTS breakdowns (
 id TEXT PRIMARY KEY, program TEXT NOT NULL REFERENCES programs(id), project TEXT NOT NULL REFERENCES projects(id),
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('proposed','active','superseded')), previous TEXT REFERENCES breakdowns(id), created REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS breakdowns_active ON breakdowns(program) WHERE status='active';
CREATE TABLE IF NOT EXISTS breakdown_packets (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS breakdown_members (
 breakdown TEXT NOT NULL REFERENCES breakdowns(id), packet TEXT NOT NULL REFERENCES breakdown_packets(id), ordinal INTEGER NOT NULL,
 PRIMARY KEY(breakdown,packet), UNIQUE(breakdown,ordinal)
);
CREATE INDEX IF NOT EXISTS breakdown_members_packet ON breakdown_members(packet);
CREATE TABLE IF NOT EXISTS breakdown_adoptions (
 breakdown TEXT PRIMARY KEY REFERENCES breakdowns(id), body TEXT NOT NULL CHECK(json_valid(body)), created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS program_closures (
 id TEXT PRIMARY KEY, program TEXT NOT NULL REFERENCES programs(id), project TEXT NOT NULL REFERENCES projects(id),
 delivery TEXT NOT NULL REFERENCES deliveries(id), binding TEXT NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS program_closures_program ON program_closures(program,created);
CREATE TRIGGER IF NOT EXISTS breakdowns_no_rewrite BEFORE UPDATE OF body,digest,program,project,previous ON breakdowns BEGIN SELECT RAISE(ABORT,'immutable breakdown proposal'); END;
CREATE TRIGGER IF NOT EXISTS breakdown_packets_no_rewrite BEFORE UPDATE ON breakdown_packets BEGIN SELECT RAISE(ABORT,'immutable breakdown packet'); END;
CREATE TRIGGER IF NOT EXISTS program_closures_no_rewrite BEFORE UPDATE ON program_closures BEGIN SELECT RAISE(ABORT,'immutable workflow closure'); END;

CREATE TABLE IF NOT EXISTS supervisor_views (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL REFERENCES projects(id), method TEXT NOT NULL,
 request_digest TEXT NOT NULL, response_digest TEXT NOT NULL, receipt TEXT NOT NULL REFERENCES receipts(id), created REAL NOT NULL,
 UNIQUE(project,method,request_digest,response_digest)
);
CREATE INDEX IF NOT EXISTS supervisor_views_project ON supervisor_views(project,seq);

-- dev28 Unit A: immutable source populations and typed traceability history.
-- The implementation lives in traceability.py and uses this exact DDL for
-- fresh databases and schema-13 migrations alike.
CREATE TABLE IF NOT EXISTS traceability_sets (
 id TEXT PRIMARY KEY,
 project TEXT NOT NULL REFERENCES projects(id),
 name TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('population','code','document')),
 active_revision TEXT REFERENCES traceability_revisions(id),
 active_digest TEXT,
 created REAL NOT NULL,
 UNIQUE(project,name)
);
CREATE TABLE IF NOT EXISTS traceability_revisions (
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
CREATE INDEX IF NOT EXISTS traceability_revisions_project ON traceability_revisions(project,set_id,revision);
CREATE TABLE IF NOT EXISTS traceability_items (
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
CREATE INDEX IF NOT EXISTS traceability_items_page ON traceability_items(revision,ordinal,id);
CREATE INDEX IF NOT EXISTS traceability_items_path ON traceability_items(revision,path,ordinal);
CREATE INDEX IF NOT EXISTS traceability_items_leaf ON traceability_items(revision,leaf,status);
CREATE TABLE IF NOT EXISTS traceability_proposals (
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
CREATE INDEX IF NOT EXISTS traceability_proposals_project ON traceability_proposals(project,set_id,created,id);
CREATE TABLE IF NOT EXISTS traceability_decisions (
 id TEXT PRIMARY KEY,
 revision TEXT NOT NULL REFERENCES traceability_revisions(id),
 project TEXT NOT NULL REFERENCES projects(id),
 body TEXT NOT NULL CHECK(json_valid(body)),
 digest TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('proposed','accepted','rejected','stale')),
 created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS traceability_mappings (
 id TEXT PRIMARY KEY,
 revision TEXT NOT NULL REFERENCES traceability_revisions(id),
 project TEXT NOT NULL REFERENCES projects(id),
 body TEXT NOT NULL CHECK(json_valid(body)),
 digest TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('proposed','accepted','stale')),
 created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS traceability_bindings (
 id TEXT PRIMARY KEY,
 project TEXT NOT NULL REFERENCES projects(id),
 revision TEXT NOT NULL REFERENCES traceability_revisions(id),
 body TEXT NOT NULL CHECK(json_valid(body)),
 digest TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','mandatory','stale','withdrawn')),
 created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS traceability_records (
 id TEXT PRIMARY KEY,
 project TEXT NOT NULL REFERENCES projects(id),
 revision TEXT REFERENCES traceability_revisions(id),
 proposal TEXT REFERENCES traceability_proposals(id),
 kind TEXT NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)),
 digest TEXT NOT NULL,
 created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS traceability_records_project ON traceability_records(project,created,id);
CREATE TRIGGER IF NOT EXISTS traceability_sets_immutable
 BEFORE UPDATE OF project,name,kind,created ON traceability_sets
 BEGIN SELECT RAISE(ABORT,'immutable traceability set'); END;
CREATE TRIGGER IF NOT EXISTS traceability_sets_no_delete
 BEFORE DELETE ON traceability_sets BEGIN SELECT RAISE(ABORT,'retain traceability sets'); END;
CREATE TRIGGER IF NOT EXISTS traceability_revisions_immutable
 BEFORE UPDATE OF set_id,project,revision,body,digest,population_digest,adapter,created ON traceability_revisions
 BEGIN SELECT RAISE(ABORT,'immutable traceability revision'); END;
CREATE TRIGGER IF NOT EXISTS traceability_revisions_no_delete
 BEFORE DELETE ON traceability_revisions BEGIN SELECT RAISE(ABORT,'retain traceability revisions'); END;
CREATE TRIGGER IF NOT EXISTS traceability_items_immutable
 BEFORE UPDATE ON traceability_items BEGIN SELECT RAISE(ABORT,'immutable traceability item'); END;
CREATE TRIGGER IF NOT EXISTS traceability_items_no_delete
 BEFORE DELETE ON traceability_items BEGIN SELECT RAISE(ABORT,'retain traceability items'); END;
CREATE TRIGGER IF NOT EXISTS traceability_proposals_immutable
 BEFORE UPDATE OF set_id,project,kind,body,digest,expected_active,semantic_material_digest,created ON traceability_proposals
 BEGIN SELECT RAISE(ABORT,'immutable traceability proposal'); END;
CREATE TRIGGER IF NOT EXISTS traceability_proposals_no_delete
 BEFORE DELETE ON traceability_proposals BEGIN SELECT RAISE(ABORT,'retain traceability proposals'); END;
CREATE TRIGGER IF NOT EXISTS traceability_decisions_immutable
 BEFORE UPDATE ON traceability_decisions BEGIN SELECT RAISE(ABORT,'immutable traceability decision'); END;
CREATE TRIGGER IF NOT EXISTS traceability_decisions_no_delete
 BEFORE DELETE ON traceability_decisions BEGIN SELECT RAISE(ABORT,'retain traceability decisions'); END;
CREATE TRIGGER IF NOT EXISTS traceability_mappings_immutable
 BEFORE UPDATE ON traceability_mappings BEGIN SELECT RAISE(ABORT,'immutable traceability mapping'); END;
CREATE TRIGGER IF NOT EXISTS traceability_mappings_no_delete
 BEFORE DELETE ON traceability_mappings BEGIN SELECT RAISE(ABORT,'retain traceability mappings'); END;
CREATE TRIGGER IF NOT EXISTS traceability_bindings_immutable
 BEFORE UPDATE ON traceability_bindings BEGIN SELECT RAISE(ABORT,'immutable traceability binding'); END;
CREATE TRIGGER IF NOT EXISTS traceability_bindings_no_delete
 BEFORE DELETE ON traceability_bindings BEGIN SELECT RAISE(ABORT,'retain traceability bindings'); END;
CREATE TRIGGER IF NOT EXISTS traceability_records_immutable
 BEFORE UPDATE ON traceability_records BEGIN SELECT RAISE(ABORT,'immutable traceability record'); END;
CREATE TRIGGER IF NOT EXISTS traceability_records_no_delete
 BEFORE DELETE ON traceability_records BEGIN SELECT RAISE(ABORT,'retain traceability records'); END;


CREATE TABLE IF NOT EXISTS breakdown_uploads(
 id TEXT PRIMARY KEY, program TEXT NOT NULL REFERENCES programs(id),
 project TEXT NOT NULL REFERENCES projects(id), body TEXT NOT NULL CHECK(json_valid(body)),
 scope_digest TEXT NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL
 CHECK(status IN ('open','finalized','abandoned')), result TEXT, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS breakdown_upload_units(
 upload TEXT NOT NULL REFERENCES breakdown_uploads(id), unit TEXT NOT NULL,
 ordinal INTEGER NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), bytes INTEGER NOT NULL,
 PRIMARY KEY(upload,unit), UNIQUE(upload,ordinal)
);
CREATE TABLE IF NOT EXISTS breakdown_upload_batches(
 upload TEXT NOT NULL REFERENCES breakdown_uploads(id), revision INTEGER NOT NULL,
 digest TEXT NOT NULL, result TEXT NOT NULL CHECK(json_valid(result)),
 PRIMARY KEY(upload,revision)
);

CREATE TABLE IF NOT EXISTS task_revision_proposals (
 id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id),
 project TEXT NOT NULL REFERENCES projects(id), from_revision INTEGER NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL,
 binding TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('proposed','applied','withdrawn')),
 result TEXT CHECK(result IS NULL OR json_valid(result)), created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS task_revision_proposals_task ON task_revision_proposals(task,created);
CREATE TABLE IF NOT EXISTS task_revision_history (
 id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id),
 project TEXT NOT NULL REFERENCES projects(id), from_revision INTEGER NOT NULL,
 to_revision INTEGER NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)),
 digest TEXT NOT NULL, created REAL NOT NULL, UNIQUE(task,to_revision),
 CHECK(to_revision=from_revision+1)
);
CREATE INDEX IF NOT EXISTS task_revision_history_task ON task_revision_history(task,to_revision);
CREATE TRIGGER IF NOT EXISTS task_revision_proposals_immutable
 BEFORE UPDATE OF task,project,from_revision,body,digest,binding,created ON task_revision_proposals
 BEGIN SELECT RAISE(ABORT,'immutable task revision proposal'); END;
CREATE TRIGGER IF NOT EXISTS task_revision_history_no_update BEFORE UPDATE ON task_revision_history
 BEGIN SELECT RAISE(ABORT,'immutable task definition history'); END;
CREATE TRIGGER IF NOT EXISTS task_revision_history_no_delete BEFORE DELETE ON task_revision_history
 BEGIN SELECT RAISE(ABORT,'immutable task definition history'); END;


CREATE TABLE IF NOT EXISTS workstreams(
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id),
 program TEXT NOT NULL REFERENCES programs(id), parent TEXT REFERENCES workstreams(id),
 previous TEXT REFERENCES workstreams(id), breakdown TEXT NOT NULL REFERENCES breakdowns(id),
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('proposed','active','superseded','withdrawn')), created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS workstreams_program ON workstreams(program,status,parent);
CREATE TABLE IF NOT EXISTS workstream_packets(
 id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES workstreams(id), ordinal INTEGER NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, UNIQUE(scope,ordinal)
);
CREATE TABLE IF NOT EXISTS workstream_records(
 id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES workstreams(id), project TEXT NOT NULL REFERENCES projects(id),
 kind TEXT NOT NULL CHECK(kind IN ('adopt','finish','withdraw')),
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS workstream_records_scope ON workstream_records(scope,kind,created);
CREATE TRIGGER IF NOT EXISTS workstreams_immutable BEFORE UPDATE OF project,program,parent,previous,breakdown,body,digest,created ON workstreams
 BEGIN SELECT RAISE(ABORT,'immutable delegated scope'); END;
CREATE TRIGGER IF NOT EXISTS workstream_packets_immutable BEFORE UPDATE ON workstream_packets
 BEGIN SELECT RAISE(ABORT,'immutable scope review packet'); END;
CREATE TRIGGER IF NOT EXISTS workstream_records_immutable BEFORE UPDATE ON workstream_records
 BEGIN SELECT RAISE(ABORT,'immutable delegated scope history'); END;
CREATE TRIGGER IF NOT EXISTS workstream_records_no_delete BEFORE DELETE ON workstream_records
 BEGIN SELECT RAISE(ABORT,'immutable delegated scope history'); END;


CREATE TABLE IF NOT EXISTS scope_returns(
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), scope TEXT NOT NULL REFERENCES workstreams(id),
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('proposed','applied','abandoned')), result TEXT CHECK(result IS NULL OR json_valid(result)), created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS scope_returns_scope ON scope_returns(scope,status,created);
CREATE TABLE IF NOT EXISTS scope_return_packets(
 id TEXT PRIMARY KEY, proposal TEXT NOT NULL REFERENCES scope_returns(id), project TEXT NOT NULL REFERENCES projects(id),
 level INTEGER NOT NULL, ordinal INTEGER NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, created REAL NOT NULL,
 UNIQUE(proposal,digest)
);
CREATE INDEX IF NOT EXISTS scope_return_packets_proposal ON scope_return_packets(proposal,level,ordinal);
CREATE TRIGGER IF NOT EXISTS scope_returns_body_immutable BEFORE UPDATE OF project,scope,body,digest,created ON scope_returns
 BEGIN SELECT RAISE(ABORT,'immutable scope return proposal'); END;
CREATE TRIGGER IF NOT EXISTS scope_returns_no_delete BEFORE DELETE ON scope_returns
 BEGIN SELECT RAISE(ABORT,'retain scope return history'); END;
CREATE TRIGGER IF NOT EXISTS scope_return_packets_immutable BEFORE UPDATE ON scope_return_packets
 BEGIN SELECT RAISE(ABORT,'immutable scope return review'); END;
CREATE TRIGGER IF NOT EXISTS scope_return_packets_no_delete BEFORE DELETE ON scope_return_packets
 BEGIN SELECT RAISE(ABORT,'retain scope return reviews'); END;


CREATE TABLE IF NOT EXISTS subplans(
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id),
 program TEXT NOT NULL REFERENCES programs(id), body TEXT NOT NULL CHECK(json_valid(body)),
 digest TEXT NOT NULL, created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS subplans_program ON subplans(program,created,id);
CREATE TABLE IF NOT EXISTS subplan_packets(
 id TEXT PRIMARY KEY, subplan TEXT NOT NULL REFERENCES subplans(id), project TEXT NOT NULL REFERENCES projects(id),
 ordinal INTEGER NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL,
 UNIQUE(subplan,ordinal)
);
CREATE TABLE IF NOT EXISTS subplan_compositions(
 id TEXT PRIMARY KEY, subplan TEXT NOT NULL REFERENCES subplans(id), project TEXT NOT NULL REFERENCES projects(id),
 breakdown TEXT NOT NULL REFERENCES breakdowns(id), request_digest TEXT NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, created REAL NOT NULL,
 UNIQUE(subplan,request_digest)
);
CREATE TRIGGER IF NOT EXISTS subplans_immutable BEFORE UPDATE ON subplans
 BEGIN SELECT RAISE(ABORT,'immutable partial design'); END;
CREATE TRIGGER IF NOT EXISTS subplans_no_delete BEFORE DELETE ON subplans
 BEGIN SELECT RAISE(ABORT,'retain partial design history'); END;
CREATE TRIGGER IF NOT EXISTS subplan_packets_immutable BEFORE UPDATE ON subplan_packets
 BEGIN SELECT RAISE(ABORT,'immutable partial review'); END;
CREATE TRIGGER IF NOT EXISTS subplan_packets_no_delete BEFORE DELETE ON subplan_packets
 BEGIN SELECT RAISE(ABORT,'retain partial review'); END;
CREATE TRIGGER IF NOT EXISTS subplan_compositions_immutable BEFORE UPDATE ON subplan_compositions
 BEGIN SELECT RAISE(ABORT,'immutable composition history'); END;
CREATE TRIGGER IF NOT EXISTS subplan_compositions_no_delete BEFORE DELETE ON subplan_compositions
 BEGIN SELECT RAISE(ABORT,'retain composition history'); END;

CREATE TABLE IF NOT EXISTS local_execution_proposals(
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id),
 program TEXT NOT NULL REFERENCES programs(id), subplan TEXT NOT NULL REFERENCES subplans(id),
 digest TEXT NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS local_execution_proposals_program
 ON local_execution_proposals(program,created,id);
CREATE TABLE IF NOT EXISTS local_execution_packets(
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id),
 proposal TEXT NOT NULL REFERENCES local_execution_proposals(id), ordinal INTEGER NOT NULL,
 digest TEXT NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)),
 UNIQUE(proposal,ordinal)
);
CREATE INDEX IF NOT EXISTS local_execution_packets_proposal
 ON local_execution_packets(proposal,ordinal);
CREATE TABLE IF NOT EXISTS local_execution_records(
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id),
 proposal TEXT NOT NULL REFERENCES local_execution_proposals(id),
 task TEXT REFERENCES tasks(id), epoch INTEGER, kind TEXT NOT NULL
 CHECK(kind IN ('certified','withdrawn','claimed','invalidated')),
 digest TEXT NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS local_execution_records_proposal
 ON local_execution_records(proposal,created,id);
CREATE INDEX IF NOT EXISTS local_execution_records_task
 ON local_execution_records(task,epoch,kind,created,id);
CREATE UNIQUE INDEX IF NOT EXISTS local_execution_records_certified
 ON local_execution_records(proposal) WHERE kind='certified';
CREATE UNIQUE INDEX IF NOT EXISTS local_execution_records_withdrawn
 ON local_execution_records(proposal) WHERE kind='withdrawn';
CREATE UNIQUE INDEX IF NOT EXISTS local_execution_records_task_event
 ON local_execution_records(proposal,task,epoch,kind) WHERE task IS NOT NULL;
CREATE TRIGGER IF NOT EXISTS local_execution_proposals_immutable
 BEFORE UPDATE ON local_execution_proposals
 BEGIN SELECT RAISE(ABORT,'immutable local execution proposal'); END;
CREATE TRIGGER IF NOT EXISTS local_execution_proposals_no_delete
 BEFORE DELETE ON local_execution_proposals
 BEGIN SELECT RAISE(ABORT,'retain local execution proposal history'); END;
CREATE TRIGGER IF NOT EXISTS local_execution_packets_immutable
 BEFORE UPDATE ON local_execution_packets
 BEGIN SELECT RAISE(ABORT,'immutable local execution packet'); END;
CREATE TRIGGER IF NOT EXISTS local_execution_packets_no_delete
 BEFORE DELETE ON local_execution_packets
 BEGIN SELECT RAISE(ABORT,'retain local execution packet history'); END;
CREATE TRIGGER IF NOT EXISTS local_execution_records_immutable
 BEFORE UPDATE ON local_execution_records
 BEGIN SELECT RAISE(ABORT,'immutable local execution record'); END;
CREATE TRIGGER IF NOT EXISTS local_execution_records_no_delete
 BEFORE DELETE ON local_execution_records
 BEGIN SELECT RAISE(ABORT,'retain local execution record history'); END;
CREATE TRIGGER IF NOT EXISTS local_execution_records_shape
 BEFORE INSERT ON local_execution_records
 WHEN (NEW.kind IN ('certified','withdrawn') AND (NEW.task IS NOT NULL OR NEW.epoch IS NOT NULL))
   OR (NEW.kind IN ('claimed','invalidated') AND (NEW.task IS NULL OR NEW.epoch IS NULL OR NEW.epoch < 0))
 BEGIN SELECT RAISE(ABORT,'invalid local execution record shape'); END;
-- dev18 execution-control schema


CREATE TABLE IF NOT EXISTS execution_attempts(
 id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id), project TEXT NOT NULL REFERENCES projects(id),
 attempt_epoch INTEGER NOT NULL, attempt_ordinal INTEGER NOT NULL, task_revision INTEGER NOT NULL,
 task_binding TEXT NOT NULL, status TEXT NOT NULL,
 implementer_run TEXT REFERENCES runs(id), implementer_receipt TEXT REFERENCES receipts(id),
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL,
 created REAL NOT NULL, updated REAL NOT NULL,
 UNIQUE(task,attempt_epoch), UNIQUE(task,attempt_ordinal)
);
CREATE INDEX IF NOT EXISTS execution_attempts_project ON execution_attempts(project,task,attempt_ordinal);

CREATE TABLE IF NOT EXISTS attempt_assessments(
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
CREATE INDEX IF NOT EXISTS attempt_assessments_task ON attempt_assessments(task,attempt_ordinal,created);

CREATE TABLE IF NOT EXISTS execution_control_proposals(
 id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id), project TEXT NOT NULL REFERENCES projects(id),
 task_revision INTEGER NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL,
 binding TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('proposed','applied','withdrawn','superseded')),
 result TEXT CHECK(result IS NULL OR json_valid(result)), created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS execution_control_proposals_task ON execution_control_proposals(task,created,id);

CREATE TABLE IF NOT EXISTS execution_control_packets(
 id TEXT PRIMARY KEY, proposal TEXT NOT NULL REFERENCES execution_control_proposals(id),
 project TEXT NOT NULL REFERENCES projects(id), ordinal INTEGER NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, created REAL NOT NULL,
 UNIQUE(proposal,ordinal)
);
CREATE INDEX IF NOT EXISTS execution_control_packets_proposal ON execution_control_packets(proposal,ordinal);

CREATE TABLE IF NOT EXISTS execution_control_events(
 id TEXT PRIMARY KEY, proposal TEXT NOT NULL REFERENCES execution_control_proposals(id),
 project TEXT NOT NULL REFERENCES projects(id), kind TEXT NOT NULL
 CHECK(kind IN ('applied','withdrawn','superseded')),
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS execution_control_events_proposal ON execution_control_events(proposal,created,id);

CREATE TABLE IF NOT EXISTS execution_control_authorizations(
 id TEXT PRIMARY KEY, proposal TEXT NOT NULL REFERENCES execution_control_proposals(id),
 project TEXT NOT NULL REFERENCES projects(id), task TEXT NOT NULL REFERENCES tasks(id),
 task_revision INTEGER NOT NULL, control_revision INTEGER NOT NULL, proposal_digest TEXT NOT NULL,
 requested_seconds REAL, effective_seconds REAL, assessment TEXT
 CHECK(assessment IS NULL OR assessment IN ('progress','no_progress')),
 reviewer_run TEXT NOT NULL, reviewer_receipt TEXT NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS execution_control_authorizations_task ON execution_control_authorizations(task,created,id);
CREATE UNIQUE INDEX IF NOT EXISTS execution_control_authorizations_proposal ON execution_control_authorizations(proposal);

CREATE TRIGGER IF NOT EXISTS execution_attempts_identity_immutable
 BEFORE UPDATE OF task,project,attempt_epoch,attempt_ordinal,task_revision,task_binding,body,digest,created
 ON execution_attempts BEGIN SELECT RAISE(ABORT,'immutable execution attempt identity'); END;
CREATE TRIGGER IF NOT EXISTS execution_attempts_no_delete
 BEFORE DELETE ON execution_attempts BEGIN SELECT RAISE(ABORT,'retain execution attempt history'); END;
CREATE TRIGGER IF NOT EXISTS attempt_assessments_immutable
 BEFORE UPDATE ON attempt_assessments BEGIN SELECT RAISE(ABORT,'immutable attempt assessment'); END;
CREATE TRIGGER IF NOT EXISTS attempt_assessments_no_delete
 BEFORE DELETE ON attempt_assessments BEGIN SELECT RAISE(ABORT,'retain attempt assessment history'); END;
CREATE TRIGGER IF NOT EXISTS execution_control_proposals_identity_immutable
 BEFORE UPDATE OF task,project,task_revision,body,digest,binding,created
 ON execution_control_proposals BEGIN SELECT RAISE(ABORT,'immutable execution control proposal'); END;
CREATE TRIGGER IF NOT EXISTS execution_control_proposals_no_delete
 BEFORE DELETE ON execution_control_proposals BEGIN SELECT RAISE(ABORT,'retain execution control proposal history'); END;
CREATE TRIGGER IF NOT EXISTS execution_control_packets_immutable
 BEFORE UPDATE ON execution_control_packets BEGIN SELECT RAISE(ABORT,'immutable execution control packet'); END;
CREATE TRIGGER IF NOT EXISTS execution_control_packets_no_delete
 BEFORE DELETE ON execution_control_packets BEGIN SELECT RAISE(ABORT,'retain execution control packet history'); END;
CREATE TRIGGER IF NOT EXISTS execution_control_events_immutable
 BEFORE UPDATE ON execution_control_events BEGIN SELECT RAISE(ABORT,'immutable execution control event'); END;
CREATE TRIGGER IF NOT EXISTS execution_control_events_no_delete
 BEFORE DELETE ON execution_control_events BEGIN SELECT RAISE(ABORT,'retain execution control event history'); END;
CREATE TRIGGER IF NOT EXISTS execution_control_authorizations_immutable
 BEFORE UPDATE ON execution_control_authorizations BEGIN SELECT RAISE(ABORT,'immutable execution control authorization'); END;
CREATE TRIGGER IF NOT EXISTS execution_control_authorizations_no_delete
 BEFORE DELETE ON execution_control_authorizations BEGIN SELECT RAISE(ABORT,'retain execution control authorization history'); END;

CREATE TABLE IF NOT EXISTS assurance_objects (
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
CREATE INDEX IF NOT EXISTS assurance_objects_project_kind ON assurance_objects(project,kind,logical_id,revision);
CREATE INDEX IF NOT EXISTS assurance_objects_digest ON assurance_objects(project,digest);
CREATE TABLE IF NOT EXISTS assurance_events (
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
CREATE INDEX IF NOT EXISTS assurance_events_subject ON assurance_events(project,subject_id,created,id);
CREATE INDEX IF NOT EXISTS assurance_events_project ON assurance_events(project,created,id);
CREATE TABLE IF NOT EXISTS assurance_heads (
 project TEXT NOT NULL REFERENCES projects(id),
 logical_id TEXT NOT NULL,
 head_event TEXT NOT NULL REFERENCES assurance_events(id),
 PRIMARY KEY(project,logical_id)
);
CREATE INDEX IF NOT EXISTS assurance_heads_event ON assurance_heads(head_event);
CREATE TABLE IF NOT EXISTS assurance_refs (
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
CREATE INDEX IF NOT EXISTS assurance_refs_lookup ON assurance_refs(ref_kind,ref_id,ref_revision,ref_digest);
CREATE INDEX IF NOT EXISTS assurance_refs_object ON assurance_refs(object_id,ordinal);
CREATE TRIGGER IF NOT EXISTS assurance_objects_no_update
 BEFORE UPDATE ON assurance_objects BEGIN SELECT RAISE(ABORT,'immutable assurance object'); END;
CREATE TRIGGER IF NOT EXISTS assurance_objects_no_delete
 BEFORE DELETE ON assurance_objects BEGIN SELECT RAISE(ABORT,'immutable assurance object'); END;
CREATE TRIGGER IF NOT EXISTS assurance_events_no_update
 BEFORE UPDATE ON assurance_events BEGIN SELECT RAISE(ABORT,'immutable assurance event'); END;
CREATE TRIGGER IF NOT EXISTS assurance_events_no_delete
 BEFORE DELETE ON assurance_events BEGIN SELECT RAISE(ABORT,'immutable assurance event'); END;
CREATE TRIGGER IF NOT EXISTS assurance_refs_no_update
 BEFORE UPDATE ON assurance_refs BEGIN SELECT RAISE(ABORT,'immutable assurance reference'); END;
CREATE TRIGGER IF NOT EXISTS assurance_refs_no_delete
 BEFORE DELETE ON assurance_refs BEGIN SELECT RAISE(ABORT,'immutable assurance reference'); END;
