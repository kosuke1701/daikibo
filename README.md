# daikibo

daikibo is a local workflow-control system for evidence-bound, large-scale
agentic software development. It connects specifications, requirements,
implementation, tests, and reviews while retaining the evidence behind each
decision.

This repository contains the application source, its current tests, concise
technical documentation, and runnable examples. Development logs, historical
review evidence, prebuilt packages, and third-party dependencies are not
included.

The [decision and change lifecycle guide](docs/DECISION-LIFECYCLE.md) explains
how to review and atomically apply decision batches and how to handle tightly
scoped title-only repairs.

## Requirements

- Linux
- CPython 3.13
- Git
- A logged-in Claude Code or Codex CLI when using managed agents

The controller runs as the current user and stores its state locally. It does
not require sudo, containers, or a separate service account.

## Install

Create a virtual environment and install from the checkout. pip downloads the
pinned Python dependencies declared in `pyproject.toml`.

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/daikibo version
```

## Connect a project

From the project that daikibo should manage:

```bash
/path/to/daikibo/.venv/bin/daikibo connect \
  --client codex \
  --workspace "$PWD" \
  --name "My project"
```

Use `--client claude` for Claude Code or `--client both` for both clients.
`connect` starts the local controller, registers the workspace, and installs
the bundled project-local Skill. For Claude Code it also installs the local
conversation hooks unless `--no-hooks` is given.

The default state directory is `$HOME/.local/state/daikibo`. Override it with
`--home` or `DAIKIBO_HOME`.

Useful controller commands:

```bash
daikibo start
daikibo doctor
daikibo stop
daikibo disconnect --workspace "$PWD"
```

Before assigning multiple workers, a coordinating agent can inspect the
currently claimable, non-conflicting Task candidates without reserving them:

```bash
daikibo call task.parallel_candidates --json '{"project":"PRJ-..."}'
```

Before claiming a Task, inspect preparation errors without starting a process:

```bash
daikibo call task.preflight --json '{"task":"TASK-...","adapter":"my-adapter"}'
```

Pass `adapter` to `task.claim` to run this preparation check before acquiring
the lease. Automation does this automatically. Preflight does not authorize
execution; the execution and completion gates still check current evidence.

Draft synchronization can use `artifact.save`. It preserves the current revision
only when the body, draft status, reason, expected revision and saving actor
match the previous save. `artifact.revise` remains the operation for an
intentional new revision. `change.delta` likewise preserves an identical
immediate re-save, including its reason and actor; use `force_revision=true`
for an intentional new submission. Same request IDs remain the preferred way
to retry a lost RPC response.

Artifact and test-plan approvals bind the actual reviewed material, including
the current definition and supporting inputs. Technical changes, provisional
decisions and Delivery profile amendments likewise bind current constraints
and the replaced profile.
An old approval cannot authorize different material, or override a newer valid
failure for the same material and role. Existing receipts with the older,
body-only bindings remain historical evidence; obtain a new review before using
them for these transitions.
Assurance and Traceability packet adoption also require the latest valid
judgment for each exact packet and role.
Frozen test plans retain their baseline snapshot manifest. After execution,
reviewing the same frozen plan uses that actual baseline only while its Task,
read pins, dependencies and policy still match. Before execution, a new review
uses the current repository snapshot, so an agent can review and freeze a new
baseline. Implementation reviews continue to inspect the implementation snapshot.

To assert an Artifact link as an agent, submit a `trace` or `design` review of
the source Artifact with this exact `proposal`:

```json
{"format":"artifact.link.v1","target":"ARTIFACT-ID","relation":"realizes","confidence":"asserted","basis":"Why this exact relationship holds"}
```

Use the resulting receipt with the same target, relation and basis in
`trace.link`. A general review of the source alone cannot approve the link.

Ordinary conversation reuses the existing Program, including during delivery.
After the first Program, start separate work explicitly with `program.begin`
or `start_program=true` on `dialogue.input` / `native.input`. New input is always
retained; use the normal change/reopen workflow for changes to existing work.
Native answers and notification acknowledgements reference their original
human source and record a separate exact quote for each target. They never
classify a mixed user turn as entirely non-requirement text.
Decision options with linked changes bind each choice to an explicit effect:
`approve` applies the proposal, while `keep_existing` preserves the current
specification and closes the declined change workflow. Custom options on a
side-effecting proposal declare `choice_effects`; standalone custom options
remain record-only. A response retry under another RPC request ID should pass
the original source, choice and exact quote to remain a no-op. Changing the
choice requires a new source recorded after the prior response; omitting the
source creates a new authenticated response observation.
Pass a notification's catalog digest as `expected_digest` when acknowledging
it to reject a changed notice; acknowledgement records the exact notice digest.
Changed notices require a human source recorded after that notice version was
published. Reusing an answer to an earlier version is rejected, including when
the wall clock moves backwards. Identical notice resends preserve the version.
Generic acknowledgement cannot answer product or provisional decisions or
resolve conflicts. Automatic notice closure is recorded separately from a
human acknowledgement; a later acknowledgement retry is accepted only when
the exact human acknowledgement remains current for that notice.
For notices, `created` is the current version's display timestamp; publication
and source event sequences determine the actual ordering of versions and answers.
Saved Contexts retain their historical material; `context.fresh` also checks
the current adoption status of their referenced Artifacts.

`job.list` supports kind, status, Task, subject and time filters. `job.list` and
`code.consumers` return `next_offset` and `snapshot`; pass that snapshot as
`expected_snapshot` on subsequent pages. A changed catalog or index requires
a fresh query. Consumer matching remains inferred, not a complete semantic
impact analysis.

Automatic Task selection scans up to `scan_limit` candidates (default 1000).
`search_incomplete` includes `next_after_task`; continue with `after_task`.
It is distinct from `no_work`. Every selected Task still undergoes the normal
claim and conflict checks. Concurrent index Jobs stay queued while the shared
index build slot is occupied, without consuming an execution attempt.

For an oversized mutation response, the RPC returns a digest-bound saved-result
reference instead of reporting an error after committing the change. The
Python client retrieves and verifies its bounded `request.result` pages
automatically. Custom RPC clients should follow the reference's request ID,
SHA-256 and byte size, verify every range and the final digest, and then decode
the original JSON. The frame size limit still applies to every page.

Heartbeat renewal alone no longer makes a Task revision proposal stale; lease
expiry, ownership/epoch changes, and changes to its actual review material do.
Repeating the same pause state preserves the execution epoch; an explicit
`fence=true` or running Job cancellation still fences execution. Completion
reports retain both blocker codes and structured release-gate details, while
repeating the final evidence checks.

Managed implementation prompts contain one copy of each Task, requirement and
test-plan body. The prompt's Context view uses JSON pointers to those same
inputs; its ID and digest identify the complete immutable stored Context.
All unique authority, source, policy, invariant and test-evidence material is
retained. This reduces duplicate input, without relaxing context capacity or
claiming that LLM judgment quality has been measured.

Managed agent adapters use the existing login and environment of their CLI.
Their execution modes may allow workspace changes without an interactive
approval prompt, so run daikibo only in a workspace where that behavior is
appropriate.

## Development

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install '.[dev]'
.venv/bin/python -m pytest -q
```

The package uses the small standard-library PEP 517 backend in
`tools/standardbuild.py`; no separate build backend dependency is bundled.
See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the component model and
[`docs/API.md`](docs/API.md) for the controller API. JSON Schema validation is
described in [`docs/SCHEMA-CHECKS.md`](docs/SCHEMA-CHECKS.md). Small input examples are
under [`examples/`](examples/).

## License

MIT. See [`LICENSE`](LICENSE).
