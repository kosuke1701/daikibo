# daikibo

daikibo is a local workflow-control system for evidence-bound, large-scale
agentic software development. It connects specifications, requirements,
implementation, tests, and reviews while retaining the evidence behind each
decision.

This repository contains the application source, its current tests, concise
technical documentation, and runnable examples. Development logs, historical
review evidence, prebuilt packages, and third-party dependencies are not
included.

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
