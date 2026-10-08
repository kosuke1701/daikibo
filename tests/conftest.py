import os
import sys
from pathlib import Path
if os.environ.get('DAIKIBO_TEST_INSTALLED')!='1':
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import pytest
from daikibo.db import Store
from daikibo.security import Security
from daikibo.knowledge import Knowledge
from daikibo.governance import Governance
from daikibo.workflow import Workflow
from daikibo.planning import Planning
from daikibo.gitops import Snapshots
from daikibo.runtime import Runtime
from daikibo.indexing import Indexer,Contexts
from daikibo.assurance import Assurance
from daikibo.assurance_relations import REGISTRY_V2_DIGEST
from daikibo.common import parse_json


def _ensure_accepted_artifact(control, project, kind, title, body):
    """Create one missing phase artifact for the public execution fixture."""
    row = control.s.one(
        "SELECT id FROM artifacts WHERE project=? AND kind=? "
        "AND status='accepted' ORDER BY id LIMIT 1",
        (project, kind),
    )
    if row is not None:
        return row["id"]
    item = control.k.propose(control.owner, project, kind, body)
    control.k.accept(control.owner, item["id"], item["revision"])
    return item["id"]


def seed_execution_phase_material(control, project, requirements=None):
    """Seed the public phase prefix before a breakdown is composed.

    A root's immutable input digest includes accepted phase artifacts.  Older
    fixtures created the root first and added those artifacts only when they
    reached execution, which correctly makes the root stale.  New execution
    fixtures seed the same accepted phase material before root composition so
    later phase advancement does not rewrite a subplan's governed input.
    """
    source = control.s.one(
        "SELECT id FROM sources WHERE project=? ORDER BY id LIMIT 1",
        (project,), True,
    )
    source_ref = source["id"] if source is not None else None
    source_refs = {"source_refs": [source_ref]} if source_ref else {}
    requirements = list(requirements or [item["id"] for item in control.s.all(
        "SELECT id FROM artifacts WHERE project=? AND kind='requirement' "
        "AND status='accepted' ORDER BY id", (project,),
    )])
    scenario = _ensure_accepted_artifact(
        control, project, "scenario", "Execution scenario",
        {"title": "Execution scenario",
         "statement": "The selected production task completes its bounded journey.",
         **source_refs},
    )
    interface = _ensure_accepted_artifact(
        control, project, "interface", "Execution interface",
        {"title": "Execution interface",
         "statement": "The selected task exposes its accepted arithmetic result.",
         "input": "two integers", "output": "one integer", "authentication": "none",
         "errors": "invalid values are rejected",
         "idempotency": "same inputs have the same result",
         "compatibility": "existing callers remain compatible", "consumers": [],
         "verification": "test_calc.py", **source_refs},
    )
    finding = _ensure_accepted_artifact(
        control, project, "finding", "Execution feasibility",
        {"title": "Execution feasibility",
         "statement": "The bounded execution experiment completed.", **source_refs},
    )
    design = _ensure_accepted_artifact(
        control, project, "design", "Execution design",
        {"title": "Execution design",
         "statement": "The selected task preserves the accepted requirement.",
         **source_refs},
    )
    verification = _ensure_accepted_artifact(
        control, project, "test", "Execution verification",
        {"title": "Execution verification",
         "statement": "The measured test checks the accepted requirement.",
         **source_refs},
    )
    for requirement in requirements:
        for artifact, relation, rationale in (
            (design, "realizes", "The execution design preserves this requirement."),
            (verification, "verifies", "The execution test covers this requirement."),
        ):
            if control.s.one(
                "SELECT source FROM links WHERE source=? AND target=? AND relation=? "
                "AND confidence='asserted'",
                (artifact, requirement, relation),
            ) is None:
                control.k.link(
                    control.owner, artifact, requirement, relation, "asserted", rationale,
                )
    for repo in control.s.all("SELECT id FROM repos WHERE project=?", (project,)):
        control.idx.index(control.owner, repo["id"])
    control.idx.search(control.owner, project, "add")
    return {
        "scenario": scenario, "interface": interface, "finding": finding,
        "design": design, "verification": verification,
    }


def ensure_current_root(control, project, task):
    """Migrate an active test root through the public phase prefix.

    Unit4-R requires a root route to carry current root proof at every Task
    checkpoint.  Older execution fixtures stopped at an adopted plan and
    then called ``Workflow.ready`` directly.  This helper supplies the same
    public phase outputs and observed phase reviews as a real execution
    fixture, while leaving planning-only tests at their original phase.
    """
    traceability = getattr(control, "traceability", None)
    programs_for_task = getattr(traceability, "_programs_for_task", None)
    if not callable(programs_for_task):
        return []
    programs = sorted(programs_for_task(project, task))
    advanced = []
    for program in programs:
        row = control.s.one(
            "SELECT phase FROM programs WHERE id=? AND project=?",
            (program, project),
            True,
        )
        if row is None or row["phase"] in {"implementation", "integration", "delivery"}:
            continue

        requirements = [item["id"] for item in control.s.all(
            "SELECT id FROM artifacts WHERE project=? AND kind='requirement' "
            "AND status='accepted' ORDER BY id",
            (project,),
        )]
        seed_execution_phase_material(control, project, requirements)

        if control.s.one("SELECT project FROM profiles WHERE project=?", (project,)) is None:
            from test_delivery_git_and_recovery import profile

            task_ids = [item["id"] for item in control.s.all(
                "SELECT id FROM tasks WHERE project=? AND status!='cancelled' ORDER BY id",
                (project,),
            )]
            requirement_ids = requirements or [
                control.s.one(
                    "SELECT artifact FROM task_reads WHERE task=? ORDER BY artifact LIMIT 1",
                    (task,),
                    True,
                )["artifact"]
            ]
            repos = [item["id"] for item in control.s.all(
                "SELECT id FROM repos WHERE project=? ORDER BY id", (project,)
            )]
            body = profile(project, repos[0], requirement_ids[0], task)
            body["program"] = program
            body["required_requirements"] = requirement_ids
            body["required_tasks"] = task_ids
            body["repo_order"] = repos
            control.d.configure(control.owner, project, body)

        adapter = (
            "markers"
            if control.s.one("SELECT name FROM adapters WHERE name='markers'")
            else "fixture"
        )
        while control.p.next(control.owner, program)["phase"] != "implementation":
            current = control.p.next(control.owner, program)
            receipt = control.rt.review(control.owner, program, "phase", adapter)["receipt"]
            control.p.advance(
                control.owner, program, current["revision"], receipt,
            )
        advanced.append(program)
    return advanced


def _adopt_current_root_relation(control, project, task):
    """Seal the observed candidate producer required by a root completion."""
    traceability = getattr(control, "traceability", None)
    programs_for_task = getattr(traceability, "_programs_for_task", None)
    if not callable(programs_for_task):
        return []
    from daikibo.assurance_denominators import _find_current_candidate, _task_ref
    from daikibo.assurance_stage import _matching_relation_set

    adopted = []
    adapter = (
        "markers"
        if control.s.one("SELECT name FROM adapters WHERE name='markers'")
        else "fixture"
    )
    task_row = control.s.one(
        "SELECT * FROM tasks WHERE id=? AND project=?", (task, project), True,
    )
    task_ref = _task_ref(project, task_row)

    def refresh_root(program):
        active = control.breakdowns.active(control.owner, program)
        if active is None:
            return
        if control.breakdowns.audit(control.owner, active["id"])["current"]:
            return
        root = control.breakdowns._row(control.owner, active["id"])
        proposal = control.breakdowns.propose(
            control.owner, program, "Execution root refresh",
            "Retain the exact governed scope after producer material adoption",
            root["body"]["units"], previous=active["id"],
        )
        from test_reviewed_breakdowns import review_all
        review_all(control, proposal["id"])
        control.breakdowns.activate(
            control.owner, proposal["id"], expected_active=active["id"],
        )

    for program in sorted(programs_for_task(project, task)):
        selection = control.assurance.selected_profile(control.owner, project, program)
        profile_ref = selection.get("profile_ref")
        if profile_ref is None:
            continue
        registry_digest = selection.get(
            "effective_relation_contract_digest", REGISTRY_V2_DIGEST,
        )
        existing, _diagnostic = _matching_relation_set(
            control, project, relation="produced_by", direction="incoming",
            center_ref=task_ref, scope_ref=profile_ref,
            registry_digest=registry_digest,
        )
        if existing is not None:
            continue
        candidate = _find_current_candidate(control, control.owner, project, task_row, [])
        if candidate is None:
            continue
        task_body = parse_json(task_row["body"])
        declarations = task_body.get("structural_obligations", {}).get("required_outputs", [])
        if not any(
                isinstance(item, dict) and item.get("realization_kind") == "artifact"
                for item in declarations
        ):
            continue
        repository = task_body.get("repos", [None])[0]
        write_paths = task_body.get("write_paths", [])
        if not isinstance(repository, str) or "artifact-output.json" not in write_paths:
            continue
        collected = control.w.artifacts_collect(
            control.owner, task, task_row["revision"], candidate["ref"]["candidate"],
            repository, "artifact-output.json",
        )
        produced = collected.get("artifacts", [])
        if not produced:
            continue
        artifact = produced[0]["artifact"]
        control.k.accept(control.owner, artifact["id"], artifact["revision"])
        refresh_root(program)
        source_ref = {
            "kind": "artifact", "project": project,
            "artifact": artifact["id"], "revision": artifact["revision"],
            "body_digest": artifact["digest"],
        }
        scope = control.assurance._scope_from_ref(project, profile_ref)
        obligations_row = control.s.one(
            "SELECT * FROM assurance_objects WHERE project=? AND kind='obligations' "
            "AND logical_id=? ORDER BY revision DESC LIMIT 1",
            (project, "obligations:" + scope["id"]), True,
        )
        obligations = control.assurance._decode_object(obligations_row)["body"]
        obligation_ids = [
            item["id"] for item in obligations.get("obligations", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        edge = control.assurance.edge_propose(
            control.owner, project,
            {
                "source_ref": source_ref, "target_ref": task_ref,
                "relation": "produced_by", "relation_contract_digest": registry_digest,
                "scope_ref": profile_ref,
                "claim": "The observed candidate is produced by this current Task.",
                "obligation_ids": obligation_ids, "required_evidence_refs": [],
                "authority_refs": [],
            },
        )
        edge_root = edge["edge"]
        refs = []
        for packet, role in control.assurance._review_requirements(
                project, control.assurance._adoption_roots(project, edge_root)):
            receipt = control.rt.review(control.owner, packet["id"], role, adapter)["receipt"]
            refs.append({"packet": packet["id"], "role": role, "id": receipt})
        control.assurance.adopt(
            control.owner, project, edge_root["id"], edge_root["digest"], None, refs,
        )
        relation_set = control.assurance.set_propose(
            control.owner, project,
            {
                "center_ref": task_ref, "relation": "produced_by", "direction": "incoming",
                "relation_contract_digest": registry_digest,
                "scope_ref": profile_ref, "criteria": {}, "required_evidence_refs": [],
            },
        )
        relation_root = relation_set["set"]
        refs = []
        for packet, role in control.assurance._review_requirements(
                project, control.assurance._adoption_roots(project, relation_root)):
            receipt = control.rt.review(control.owner, packet["id"], role, adapter)["receipt"]
            refs.append({"packet": packet["id"], "role": role, "id": receipt})
        control.assurance.adopt(
            control.owner, project, relation_root["id"], relation_root["digest"], None, refs,
        )
        adopted.append(program)
    return adopted

@pytest.fixture
def system(tmp_path):
    # Worker paths are outside pytest's private root; protected DB stays private.
    store=Store(tmp_path/'control');sec=Security(store);owner=sec.authenticate(Path(sec.bootstrap()).read_text())
    k=Knowledge(store,sec);g=Governance(store,sec,k,mode='validation');w=Workflow(store,sec,k,g)
    p=Planning(store,sec,k,g,w);sn=Snapshots(store,sec,k);rt=Runtime(store,sec,k,g,w,sn,p,mode='validation')
    idx=Indexer(store,sec,k);ctx=Contexts(store,k,g,idx);rt.context=ctx
    s=type('System',(),dict(s=store,sec=sec,owner=owner,k=k,g=g,w=w,p=p,sn=sn,rt=rt,idx=idx,ctx=ctx))()
    # Compose the same E1 immutable writer that Control installs.  The focused
    # fixture intentionally builds the individual services, but Runtime test
    # evidence must still be stored through real Assurance/CAS reads rather
    # than a test-only material store or an accepted-success fallback.
    s.assurance=Assurance(s)
    rt.assurance=s.assurance
    yield s
    rt.shutdown();idx.close();store.close()

@pytest.fixture
def project(system,tmp_path):
    s=system
    project=s.k.create_project(s.owner,'example')['id']
    repo=tmp_path/'source';repo.mkdir();(repo/'calc.py').write_text('def add(a, b):\n    return a - b\n')
    (repo/'test_calc.py').write_text('from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n')
    rid=s.sn.register(s.owner,project,'app',str(repo))['id']
    src=s.k.source(s.owner,project,'Provide correct addition.')
    req=s.k.propose(s.owner,project,'requirement',{'title':'addition','statement':'add returns sum','acceptance':['AC-ADD'],'source_refs':[src['id']]})
    s.k.accept(s.owner,req['id'],1)
    s.k.classify(s.owner,src['id'],0,25,'requirement',[req['id']],'Original requirement')
    s.rt.adapters.register(s.owner,'fixture','fixture',sys.executable,[str(Path(__file__).with_name('fixture_agent.py'))])
    return project,rid,req['id'],repo

@pytest.fixture
def task(system,project):
    import json
    s=system;pid,rid,req,_=project
    task=s.w.create(s.owner,pid,{'title':'correct addition','goal':'WRITE:'+json.dumps({'calc.py':'def add(a, b):\n    return a + b\n'}),
                                'read_artifacts':[req],'write_paths':['calc.py'],'acceptance':['AC-ADD'],'dependencies':[],'repos':[rid],'non_goals':[]})
    s.w.plan_tests(s.owner,task['id'],{'checks':[{'id':'unit','argv':['python','-m','pytest','-q','test_calc.py'],'kind':'pytest','required_tests':['test_add']}]})
    s.w.ready(s.owner,task['id'])
    return task['id']

@pytest.fixture
def full(tmp_path):
    from daikibo.control import Control
    c=Control(tmp_path/'control',mode='validation',start_workers=False)
    c.owner=c.sec.authenticate(Path(c.sec.bootstrap()).read_text())
    yield c
    c.close()

@pytest.fixture
def full_project(full,tmp_path):
    c=full
    pid=c.k.create_project(c.owner,'Full integration')['id']
    root=tmp_path/'repo';root.mkdir()
    (root/'calc.py').write_text('def add(a,b):\n    return a-b\n')
    (root/'test_calc.py').write_text('from calc import add\ndef test_add():\n    assert add(2,3)==5\n')
    rid=c.sn.register(c.owner,pid,'app',str(root))['id']
    src=c.k.source(c.owner,pid,'Addition returns the arithmetic sum.')
    req=c.k.propose(c.owner,pid,'requirement',{'title':'Addition','statement':'Returns arithmetic sum','acceptance':['AC-ADD'],'source_refs':[src['id']]})
    c.k.accept(c.owner,req['id'],1);c.k.classify(c.owner,src['id'],0,src['characters'],'requirement',[req['id']],'Original source')
    c.rt.adapters.register(c.owner,'fixture','fixture',sys.executable,[str(Path(__file__).with_name('fixture_agent.py'))])
    return pid,rid,req['id'],root


def route_change_to_product(control,change,adapter='fixture'):
    """Reach product adjudication through the public typed review/attempt path."""
    for _ in range(3):
        stage=control.s.one('SELECT stage FROM changes WHERE id=?',(change,),True)['stage']
        if stage=='awaiting_product_decision':
            return stage
        review=control.rt.review(control.owner,change,'consistency',adapter)
        scope=next((item.get('resolution') for item in review['result'].get('dispositions',[])
                    if item.get('id')=='scope:'+change),None)
        if scope=='upper_scope_required':
            receipt=review['receipt'];outcome='scope_exceeded'
        else:
            review=control.rt.review(control.owner,change,'feasibility',adapter)
            receipt=review['receipt'];outcome='no_solution_found'
        control.p.attempt(control.owner,change,stage,{
            'hypothesis':'Exercise the independently reviewed test route.',
            'alternatives':['Keep the current change scope.'],'evidence':[receipt],
            'outcome':outcome,'remaining_unknown':'None in this deterministic test fixture.',
            'review_receipt':receipt})
    return control.s.one('SELECT stage FROM changes WHERE id=?',(change,),True)['stage']


def make_task(c,project,goal=None,paths=None,deps=None):
    import json
    pid,rid,req,_=project
    task=c.w.create(c.owner,pid,{'title':'Fix arithmetic','goal':goal or 'WRITE:'+json.dumps({'calc.py':'def add(a,b):\n    return a+b\n'}),
                                'read_artifacts':[req],'write_paths':paths or ['calc.py'],'acceptance':['AC-ADD'],'dependencies':deps or [],'repos':[rid],'non_goals':[]})
    c.w.plan_tests(c.owner,task['id'],{'checks':[{'id':'unit','argv':['python','-m','pytest','-q','test_calc.py'],'kind':'pytest','required_tests':['test_add']}]})
    c.w.ready(c.owner,task['id']);return task['id']


def finish_task(c,project,task):
    ensure_current_root(c, project, task)
    c.w.claim(c.owner,project,task);c.rt.execute(c.owner,task,'fixture');c.rt.tests(c.owner,task)
    for role in ('spec','quality','test_adequacy'):c.rt.review(c.owner,task,role,'fixture')
    _adopt_current_root_relation(c, project, task)
    return c.w.complete(c.owner,task,c.w.task(c.owner,task)['revision'])
