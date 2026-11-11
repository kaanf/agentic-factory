# 0005: Stage 5 Archon adapter, dry run and reconciliation

- Status: proposed (for human review with the Stage 5 PR)
- Date: 2026-11-11
- Inputs: Stage 4B §7 (crash safety), §8 (security model), §9 (independence), §10 (Stage 5
  acceptance and P1-P4); Stage 4A §8 where 4B does not differ; decisions 0001-0004; the installed
  Archon 0.14.0 (its bundled option table and workflow definitions, read from the binary's strings
  without running it); `docs/orchestration/archon-repo-config.md` in the pilot workspace (read only).
- Baseline: Stage 4 merge `34ef0ae` on `dev` (589 tests passing, push CI run 43025478332 green).

## Findings that shaped the design

1. Archon 0.14.0 `workflow run` documents `--branch` as "create worktree for branch (or reuse
   existing)". A dispatch for a branch that already exists would silently continue old work. 4B §7
   assumed this was unverified; it is now a documented behavior, and preflight refuses any existing
   feature branch (local, worktree, last fetch or GitHub).
2. `workflow run` has no provider option. It has `--model <tier|@alias>=<spec>` and `--config <path>`
   layers; neither was verified to select a provider. 4B §9 already cut provider routing.
3. The bundled `archon-implement` commits on its branch and **opens no pull request**. A successful
   run therefore normally ends as `NO_PR` with a local-only branch. Stage 6 must decide how the PR is
   opened (another reviewed workflow, or a human); Stage 5 only reports it (`BRANCH_NOT_PUSHED`).
4. JSON Schema `pattern` uses search semantics, so `$` accepts a trailing newline (debt item 4). A
   task id `APP-1\n` or an integration branch `next\n` passes schema validation. Every value Stage 5
   places in a path, request or argument is re-checked with full matches.
5. Archon's run store is SQLite (`~/.archon/archon.db`) with no documented read interface.

## Decisions

| # | Decision | Reason |
|---|---|---|
| S1 | New commands `preflight` and `reconcile`; no `dispatch` command and no `--execute` | 4A named the dry run `factory dispatch`. A command called dispatch that cannot dispatch invites a later one-line change into an execution path. `preflight` says what it does. Stage 6 adds `dispatch` deliberately |
| S2 | Preflight reports `dryRun` and `authorization` separately; `authorization.granted` is defined as "no unmet preconditions" and four preconditions are unconditional in this version | The brief requires dry-run success to imply nothing for Stage 6, and simulated READY must never become authority. Making it structural rather than a flag check means no input can flip it |
| S3 | Dispatch configuration is additive and optional in `factory.project/v1` (`dispatch.archon`, `workflows`, `providers`, `maxAttemptsPerTaskRevision`) and `factory.task/v1` (`execution`) | Existing manifests, tasks, snapshots and golden plans are unchanged (the new fixture is separate). No schema version bump for optional fields that no older reader sees |
| S4 | Workflow policy = manifest allowlist ∩ built-in supported set − (built-in denylist ∪ denied words ∪ manifest forbidden); validation rejects a denied workflow in the allowlist, and preflight and builder refuse it again independently | 4A §4: a manifest can add to the denylist but never remove from it. Three independent layers so a bypass of one (for example a hand-built `Manifest`) is still refused |
| S5 | Supported workflows: `archon-implement` only | It is the only workflow named by 4A whose bundled definition was reviewed here: no merge, no deploy, no PR |
| S6 | The task's provider must be Archon's configured `defaultAssistant` (`PROVIDER_NOT_ARCHON_DEFAULT`); the builder emits no provider or model option | Finding 2. Refusing is the only behavior that cannot select the wrong agent. Revisit after the probe verifies a selection mechanism |
| S7 | Archon's version and default assistant are read from `install.json` and `config.yaml`, not by running `archon --version` | The public dry-run interface must start no process. The values are recorded claims; Stage 6 must re-verify the binary at action time (documented limitation) |
| S8 | Adapter-verified Archon versions are a constant (`0.14.0`); a manifest pinned elsewhere fails `ARCHON_VERSION_UNSUPPORTED` | The builder's options were verified against 0.14.0 only. A manifest pin alone must not make an unverified CLI acceptable |
| S9 | Command: `archon workflow run <wf> --branch <feature> --base <integration> <message>`, cwd = checkout, env `DO_NOT_TRACK=1`, as an argument vector; only `--branch` and `--base` are ever emitted | `--base` explicit per `archon-repo-config.md`; `--branch` because Archon otherwise names branches itself; nothing that detaches, skips the worktree, adopts, resumes or rebinds |
| S10 | The message is a single argument starting with `Task <id> revision <n>:` and built only from approved task fields | Cannot be parsed as an option; 4B §8 control 5 (no external text) |
| S11 | Local git state is read from `.git` files through a confined, no-follow reader (`gitstate.py`, `fsread.py`) instead of running `git` | No subprocess in the dry run; symbolic links in the checkout cannot redirect reads. Cross-checked against real `git` in tests. Linked worktrees and reftable are refused as unsupported |
| S12 | The checkout's `origin` must be the manifest's GitHub repository | A checkout of the wrong repository would make every local fact meaningless |
| S13 | Run-record schemas `factory.run-intent/v1` and `factory.run-outcome/v1` are defined now; the reader validates them and reports every partial, corrupt, contradictory, linked or unexpected entry | 4B §7 defines the layout; fixing the schema before the writer exists lets Stage 6 be tested against the reader |
| S14 | The dispatch lock is probed with a shared non-blocking `flock`, never created; held → immediate refusal before anything else is read, exit 3 | 4B §7 row 2 ("exits immediately with code 3, no side effects") |
| S15 | Reconciliation is a pure decision function over observations, with the priority COORDINATOR_ACTIVE > UNCERTAIN > UNKNOWN > SUPERSEDED_PR_OPEN > untracked > PR states > outcome states > ATTEMPTS_EXHAUSTED > NO_RUN | Contradiction outranks missing data (it needs a human even when GitHub is down); fail-closed states outrank informative ones; only `NO_RUN` does not block |
| S16 | `--adopt` and `--abandon` are not implemented | They are the first write into the ledger and need the Stage 6 lock and atomic publication to be safe. Documented as manual actions |
| S17 | Archon run status is reported as not observed | Finding 5; reading an undocumented store would be guessing. Interrupted dispatches tell the human to check `archon workflow runs` |
| S18 | The preflight report records manifest and task hashes and a command digest; both files are re-hashed before returning | Narrows TOCTOU between validation and command construction and gives Stage 6 values to revalidate at action time |
| S19 | `collector.branch_pull_requests` extracted from the Stage 2 collector; `approvals` uses the shared validators in `refs.py` | Reconcile reuses the exact fork filtering; one set of ref rules. Behavior of both is unchanged (all Stage 2-4 tests pass unmodified) |
| S20 | Ruff joins CI as a pinned dev dependency (`ruff==0.17.4` in `uv.lock`) | Lint and format were checked by hand before; CI now enforces the same configuration. Dev-only, no runtime effect, exact pin keeps formatter output stable |
| S21 | A bash-only probe script and procedure are added but never run by the suite or by this stage | 4B §10 acceptance asks for live bash-only probes; they need a distinct human authorization (`FACTORY_ARCHON_PROBE_AUTHORIZED=<checkout>` plus an explicit flag). Its refusal paths are tested with a stand-in `archon` |

## Deviations from 4B

- **Command name**: `preflight` instead of a dry-run `dispatch` (S1).
- **Provider selection**: refused unless it is Archon's default (S6), stricter than 4A §8 item 6.
- **Live probes not executed**: the 4B §10 acceptance item "live bash-only probe in each repo" is
  prepared (script, procedure, refusal tests) but deliberately not run, because the brief requires a
  distinct authorization step. Stage 5 acceptance is therefore incomplete on that one item.
- **Archon status in reconcile**: 4B §7 says reconcile checks Archon's run status; Stage 5 reports it
  as not observed (S17).

## Changes to existing behavior

- `tests/integration/test_live_plan.py::test_no_command_can_dispatch_or_act` pinned the command set to
  `validate`, `plan`, `evidence`; it now also lists `preflight` and `reconcile`. Its subprocess and
  HTTP-method checks are unchanged.
- `validate` reports new errors only for manifests and tasks that use the new optional fields
  inconsistently. Existing fixtures validate and plan byte-identically.

## Not done

Execution of any kind, intent or outcome writing, `--adopt`/`--abandon`, automatic retries, Archon run
status, provider routing, the live probes, and everything listed as Stage 6 in
[archon-adapter.md](../architecture/archon-adapter.md#stage-6-prerequisites).
