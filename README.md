# agentic-factory

Factory Core v0.1: an offline, deterministic validator and dependency planner for project task
graphs that span one or more independent Git repositories.

It answers, for every task: may it start, may it open a PR, may it be merged, is it complete, and
is it safe for downstream tasks? Each answer is backed by explicit reason codes. Unknown evidence
never counts as success.

**Scope.** `validate` and `plan --evidence` read only local files. `evidence collect` and
`plan --live` make read-only GitHub API requests through the GitHub CLI. `preflight` and `reconcile`
(Stage 5) check a task revision for dispatch and print the Archon command a future dispatch would run,
without running it. Nothing dispatches agents, starts Archon, pushes, merges, deploys or writes to
the project directory. No snapshot is treated as authenticated. Approvals are read from merge records,
but human authorization is unverifiable while agents can use the approver's GitHub credentials, so no
collected approval lets a task start.

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
factory plan <project-dir> --live [--save-evidence <snapshot.json>] [--format text|json]
factory evidence collect <project-dir> [--output <snapshot.json>]
factory preflight <project-dir> --task ID --revision N --checkout DIR (--evidence FILE | --live) [--format text|json]
factory reconcile <project-dir> --task ID [--checkout DIR] (--live | --offline) [--format text|json]
```

`validate` checks the manifest, tasks, architecture profiles and ADR front matter against their
schemas. It also checks every cross-reference (repositories, profiles, ADRs, outputs, dependency
targets) and rejects duplicate ids, self-dependencies, dependency cycles, unsupported assurance
levels and self-declared approval fields. Each error names the file and field.

`plan` validates the project, loads the evidence snapshot, and prints each task's state, gate
results, assurance levels and the reasons behind every non-PASS result. JSON output is
byte-identical for identical inputs: keys are sorted and no timestamps are generated.

`plan --live` does the same in one read-only operation: it validates the project, collects evidence
and approval provenance from GitHub, validates that snapshot exactly as a file would be validated,
confirms that no integration head moved, and plans. Exactly one of `--evidence` and `--live` is
required; there is no fallback between them. A live plan is never authenticated and never makes a
task READY while human authorization is unverifiable. See
[live-planning.md](docs/architecture/live-planning.md).

`evidence collect` reads repository, branch, pull request, commit, check-run, workflow-run and file
data from GitHub and writes a snapshot for `plan`. It reports facts; `plan` judges them. Facts that
cannot be collected completely are omitted and evaluate as UNKNOWN. See
[evidence-collector.md](docs/architecture/evidence-collector.md). It also records which merged PR
introduced each task, profile and ADR file, and who merged it; see
[approval-verification.md](docs/architecture/approval-verification.md) for why that is provenance
and not human approval.

`preflight` runs every dispatch precondition it can observe (task revision and readiness, workflow
and provider policy, the Archon version from its install record, the checkout's Archon configuration
and branches, run records, reconciliation) and builds the Archon command as an argument vector. Its
report keeps the dry-run result apart from authorization, which is never granted in this version. The
dry run itself cannot pass yet: Archon's database-held AI configuration can change the provider and
is not readable, so the effective provider is never established.
`reconcile` classifies one task revision's dispatch state (Stage 4B §7) from run records, local and
remote branches and pull requests, read-only. Neither has an option to execute anything. See
[archon-adapter.md](docs/architecture/archon-adapter.md) for their exit codes, every reason code and
the decision table.

| Exit code | Meaning |
|---|---|
| 0 | Success (validate: project valid; plan: plan computed, whatever the task states, and with `--live` every fact collected; collect: every fact collected) |
| 2 | Invalid input: project or evidence snapshot (file or collected) failed validation |
| 3 | Collect or `plan --live`: snapshot or plan written but incomplete (missing facts evaluate as UNKNOWN), or the GitHub CLI is unusable |
| 64 | Usage error, including an `--output` / `--save-evidence` path inside the project or one that is a symbolic link |
| 70 | Internal error |
| 73 | `--output` / `--save-evidence` could not be written; the destination is unchanged |

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
`tests/fixtures/single-repo-project` (one Python repository, `trunk`/`stable`/`topic/`) and
`tests/fixtures/dispatch-project` (a dispatch configuration, `next`/`stable`/`work/`).

Schemas: `src/factory/schemas/*.schema.json` (JSON Schema 2020-12, shipped inside the package).

## Documentation

- [Planning model](docs/architecture/planning-model.md): levels, gates, states, the evidence
  snapshot format and all reason codes.
- [Evidence collector](docs/architecture/evidence-collector.md): what is read from GitHub and how
  failures are handled.
- [Decision 0001](docs/decisions/0001-stage1-implementation-decisions.md): how the blueprint, 4A
  and 4B were reconciled, and what was retained, changed, deferred or rejected.
- [Approval verification](docs/architecture/approval-verification.md): merge-record provenance and the
  human-authorization trust boundary.
- [Decision 0002](docs/decisions/0002-stage2-evidence-collector.md): Stage 2 collector decisions.
- [Decision 0003](docs/decisions/0003-stage3-approval-verification.md): Stage 3 approval decisions.
- [Live planning](docs/architecture/live-planning.md): `plan --live`, the architecture gate end to
  end, trust boundary, determinism and collection consistency.
- [Decision 0004](docs/decisions/0004-stage4-live-planning.md): Stage 4 decisions.
- [Archon adapter](docs/architecture/archon-adapter.md): Stage 5 preflight, command builder, run
  records, reconciliation, security boundaries and Stage 6 prerequisites.
- [Decision 0005](docs/decisions/0005-stage5-archon-adapter.md): Stage 5 decisions and deviations.
- [Decision 0006](docs/decisions/0006-stage5-security-remediation.md): Stage 5 security review
  findings F1-F6 and their fixes.
- [Decision 0007](docs/decisions/0007-stage5-second-remediation.md): second review findings R1-R3.
- [Decision 0008](docs/decisions/0008-stage5-observation-integrity.md): observation integrity, third
  review findings S1-S5.
- [Decision 0009](docs/decisions/0009-observation-consistency-contract.md): **PROPOSED** observation
  consistency contract (requires a human decision).
- [Decision 0010](docs/decisions/0010-stage5-fourth-remediation.md): fourth review findings Q2-Q5.
- [Archon probes](docs/probes/archon-probes.md): why no executable probe ships, and the conditions
  for a future approved manual probe (acceptance incomplete).
- [Provider verification](docs/architecture/provider-verification.md): evidence, missing acceptance
  criteria and a proposed read-only design.
- [Technical debt](docs/technical-debt.md): known limitations and their classification.

## Development

```sh
uv run pytest                                   # all tests, sockets blocked in-process
uv run ruff check . && uv run ruff format --check .   # lint and format (also in CI)
uv run pytest tests/acceptance -v               # Stage 1 acceptance suite T1-T16
python tests/support.py                         # regenerate committed evidence fixtures
python tests/github_support.py                  # re-record real GitHub responses (read-only)
```

CI runs the suite inside a Linux network namespace with no interfaces, so network access fails at the
operating-system level. It also runs a clean wheel install.

## Branches

`main` is the release branch and `dev` the integration branch; work happens on `feat/*` and reaches
`dev` through pull requests. Merges are made by a human only.
