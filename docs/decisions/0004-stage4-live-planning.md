# 0004: Stage 4 live planning and the architecture gate end to end

- Status: proposed (for human review with the Stage 4 PR)
- Date: 2026-11-11
- Inputs: Stage 4B §6 (architecture governance) and §10 (Stage 4 acceptance), Stage 4 brief,
  decisions 0001–0003.
- Baseline: Stage 3 merge `72077a0` on `dev` (451 tests passing, push CI green).

## Finding

Stage 4B §10 defines Stage 4 as "profile requirement for implementation tasks; ADR structure and
link checks; `plan` = collector + approval + engine". The first two already existed: the Stage 1
schemas and planner implement the profile requirement, the spike exemption, ADR research and
critique links, and STALE on revision mismatch. What was missing was the third part, a single
planning operation that composes collection and planning, and a validation path shared by files and
in-memory snapshots. Stage 4 adds that composition and changes no planning rule.

## Decisions

| # | Decision | Reason |
|---|---|---|
| L1 | Extend `factory plan` with `--live`; `--evidence` and `--live` form a required, mutually exclusive group | No new command; the existing default (a source is required) is kept, so nothing changes silently; both or neither is a usage error |
| L2 | The composition lives in a new `factory/live.py`; collectors, planner and schemas are reused unchanged | One place for the orchestration; no duplicated rules |
| L3 | `evidence.parse_snapshot` is extracted from `load_snapshot` and used by both paths | Files and in-memory snapshots must pass identical schema and semantic checks; one schema, one representation |
| L4 | A live snapshot must have provenance `github` and a `collection` record (`LIVE_EVIDENCE_NOT_COLLECTED`) | A live plan must never rest on simulated evidence, even through a collector bug |
| L5 | Live plans exit 3 when any fact could not be collected or confirmed, and still write the plan | Same contract as `evidence collect`; an incomplete plan is visible both in the exit code and in the output |
| L6 | Plan output copies the snapshot's `collection` and `credentialIsolation` records into `evidence` when present | Consumers need to tell a complete collection from a partial one, and see why approvals are unverifiable. Adding them only when present keeps plans of hand-written snapshots byte-identical |
| L7 | After collection, every recorded integration head is read again; a moved or unreadable head is dropped with its reachability facts, and approvals require a confirmed planning head | Narrows the gap between the Stage 2 and Stage 3 windows at the cost of one request per repository, without changing either collector. Does not claim atomicity |
| L8 | `--save-evidence FILE` writes the validated snapshot a live plan used; it requires `--live` and is refused inside the project | Audit and reproducibility: a saved live snapshot replans offline to the same plan |
| L9 | `collector.read_integration_head` is extracted from the Stage 2 collector | L7 reuses the exact ref read instead of a copy; Stage 2 behavior is unchanged |
| L10 | `cli.run` gains an optional `now` clock, used by `plan --live` and `evidence collect` | Tests prove byte-identical live output; production uses the system clock |

## Changes to existing behavior

- `factory plan` usage: `--evidence` is no longer marked required on its own; one of `--evidence` or
  `--live` is. Invoking `plan` with neither still exits 64.
- Plans of snapshots that contain `collection` or `credentialIsolation` (that is, snapshots written
  by `factory evidence collect`) now include those records under `evidence`, and the text output has
  two more header lines. Plans of the committed simulated fixtures are byte-identical to Stage 3
  (checked against golden files written at `72077a0`).
- `evidence collect` output, exit codes and messages are unchanged.

## Not done

Snapshot age limits, atomic multi-repository reads, credential isolation, any verified human
authorization, Stage 2 collector hardening (see [technical-debt.md](../technical-debt.md)), and
everything in Stage 5 and later. No dispatch exists.
