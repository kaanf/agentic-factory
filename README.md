# agentic-factory

Factory Core v0.1: an offline, deterministic validator and dependency planner for project task
graphs that span one or more independent Git repositories.

It answers, for every task: may it start, may it open a PR, may it be merged, is it complete, and
is it safe for downstream tasks? Each answer is backed by explicit reason codes. Unknown evidence
never counts as success.

**Scope.** `validate` and `plan` read only local files. `evidence collect` makes read-only GitHub
API requests through the GitHub CLI. Nothing dispatches agents, pushes, merges, deploys or writes to
the project directory. No snapshot is treated as authenticated, and approvals are not collected yet.

## Install

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --locked          # development environment with pytest
uv run factory --help
```

## Commands

```sh
factory validate <project-dir> [--format text|json]
factory plan <project-dir> --evidence <snapshot.json> [--format text|json]
factory evidence collect <project-dir> [--output <snapshot.json>]
```

`validate` checks the manifest, tasks, architecture profiles and ADR front matter against their
schemas. It also checks every cross-reference (repositories, profiles, ADRs, outputs, dependency
targets) and rejects duplicate ids, self-dependencies, dependency cycles, unsupported assurance
levels and self-declared approval fields. Each error names the file and field.

`plan` validates the project, loads the evidence snapshot, and prints each task's state, gate
results, assurance levels and the reasons behind every non-PASS result. JSON output is
byte-identical for identical inputs: keys are sorted and no timestamps are generated.

`evidence collect` reads repository, branch, pull request, commit, check-run, workflow-run and file
data from GitHub and writes a snapshot for `plan`. It reports facts; `plan` judges them. Facts that
cannot be collected completely are omitted and evaluate as UNKNOWN. See
[evidence-collector.md](docs/architecture/evidence-collector.md).

| Exit code | Meaning |
|---|---|
| 0 | Success (validate: project valid; plan: plan computed, whatever the task states; collect: every fact collected) |
| 2 | Invalid input: project or evidence snapshot failed validation |
| 3 | Collect: snapshot written but incomplete, or the GitHub CLI is unusable |
| 64 | Usage error |
| 70 | Internal error |

Warnings, such as an architecture profile pinning an old ADR revision, do not change the exit code.

## Project layout

The project directory is the root of the project's planning repository:

```text
factory/project.yaml        manifest (factory.project/v1)
factory/tasks/*.yaml        one task per file (factory.task/v1)
<profile paths>             architecture profiles listed in the manifest (factory.profile/v1)
<adrDirectory>/*.md         ADRs with YAML front matter (factory.adr/v1)
```

Project-specific names (repositories, branches, check names, workflow paths) live only in the
manifest. See `tests/fixtures/three-repo-project` (three repositories, `dev`/`main`/`feat/`) and
`tests/fixtures/single-repo-project` (one Python repository, `trunk`/`stable`/`topic/`).

Schemas: `src/factory/schemas/*.schema.json` (JSON Schema 2020-12, shipped inside the package).

## Documentation

- [Planning model](docs/architecture/planning-model.md): levels, gates, states, the evidence
  snapshot format and all reason codes.
- [Evidence collector](docs/architecture/evidence-collector.md): what is read from GitHub and how
  failures are handled.
- [Decision 0001](docs/decisions/0001-stage1-implementation-decisions.md): how the blueprint, 4A
  and 4B were reconciled, and what was retained, changed, deferred or rejected.
- [Decision 0002](docs/decisions/0002-stage2-evidence-collector.md): Stage 2 collector decisions.

## Development

```sh
uv run pytest                                   # all tests, sockets blocked in-process
uv run pytest tests/acceptance -v               # Stage 1 acceptance suite T1-T16
python tests/support.py                         # regenerate committed evidence fixtures
python tests/github_support.py                  # re-record real GitHub responses (read-only)
```

CI runs the suite inside a Linux network namespace with no interfaces, so network access fails at the
operating-system level. It also runs a clean wheel install.

## Branches

`main` is the release branch and `dev` the integration branch; work happens on `feat/*` and reaches
`dev` through pull requests. Merges are made by a human only.
