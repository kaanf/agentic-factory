# 0001: Factory Core Stage 1 implementation decisions

- Status: proposed (for human review with the Stage 1 PR)
- Date: 2026-11-11
- Inputs: `AGENTIC_SOFTWARE_FACTORY_BLUEPRINT.md` (blueprint), `Factory Core 4A.md` (4A proposal),
  `Factory Core 4B.md` (4B critical review), and the Stage 5 implementation brief.

## Source reconciliation

**The blueprint was found.** 4B could not locate it and made Stage 1 conditional on D0 ("locate and
reconcile the accepted blueprint"). It is a local document outside every repository. It predates 4A
and 4B, and it records goals and open questions rather than detailed Factory Core decisions.
Reconciled below, D0 is satisfied for Stage 1.

| Blueprint principle or goal | 4A / 4B treatment | Stage 1 |
|---|---|---|
| Reusable, project-agnostic; project names in a manifest (§1.11) | Kept (4B §9) | Implemented; enforced by test T15 |
| GitHub is the source of truth for work state (§1.4) | Kept; evidence read from GitHub | Engine consumes a snapshot; collector deferred to Stage 2 |
| AI proposes, deterministic systems gate (§1.5) | Kept; no model output in decisions | Implemented: pure, deterministic engine |
| No unattended merge or production deploy (§1.6) | Kept; Factory Core has no merge path | No merge, dispatch or deploy code exists |
| Separate start and integration dependencies (§6 Phase F) | 4A start/completion, corrected by 4B F5 into three gates and five levels | Implemented per 4B |
| Contract-first; mobile integration waits for staging and contract (§1.2, §7) | 4B `integration-ready` and `environment-verified` | Implemented; `environment-verified` is always UNKNOWN until staging evidence exists |
| Idempotency keyed by (project, feature, task, spec SHA) (§6) | 4B (project, task, revision) plus spec hash for staleness | Branch is `<prefix><id>-r<rev>`; spec hash drives STALE. Dispatch idempotency deferred |
| Bounded retries on failure (§6 Phase F) | 4B: no automatic retries in the MVP | Deferred, no dispatch |
| Detailed state machine (DRAFT … RELEASED) (§7) | 4A and 4B simplified it | Stage 1 states: READY, BLOCKED, UNKNOWN, STALE, IN_REVIEW, MERGED, COMPLETE. Release states deferred |
| Coordinator style left open (Actions, CLI, service) (§7) | 4A/4B: stateless CLI, no server | Implemented as a CLI |
| `main → dev → feat/*` preserved for the pilot (§4) | Kept as manifest configuration | Branch names come only from the manifest |
| Human approval needs a verifiable mechanism (§4, §13 Q9) | 4B §5: approval derived from human-only merges, after credential separation | Approval comes only from the snapshot; snapshots are `simulated` and never claimed as authenticated |

No conflict between the blueprint and 4B affects Stage 1. 4B is the latest technical design, and
where 4B and 4A differ, 4B is used.

## Decision status

The Stage 5 brief explicitly directs D1 (separate private repository), D2 (Python 3.12, pyyaml,
jsonschema, pytest), and the Stage 1 parts of C2, C4, C5, C6, C8 and D11. Those are treated as approved for
Stage 1 only. Decisions D4 to D10 (machine user, rulesets, push CI in product repositories, separate OS user,
squash-only, single host) are **not** approved by this work and are not needed for Stage 1.

## Retained from 4B

- C2: YAML approval fields are rejected (`SELF_DECLARED_APPROVAL`). This covers `approvedBy`,
  `approval`, `approved_by`, `approvedAt`, `signedOffBy` and the like, nested anywhere in tasks,
  profiles and ADR front matter.
- C4: gates `start` / `merge` / `complete`; levels `approved`, `merged`, `validated`, `integration-ready`, `environment-verified`.
- C5: non-spike tasks require an approved architecture profile.
- C6: no plugin system; architecture rules are "named CI check passed".
- C8: the engine is a pure function over a JSON snapshot; no network.
- §3 integration-evidence rule: push event, integration branch, expected app and workflow path,
  latest attempt, reachability, exactly one merged PR per commit. Tree-equivalence is not a PASS path.
- §9 independence rules and §11 acceptance tests T1–T16.

## Changed relative to 4B (with reason)

| Item | 4B | Implemented | Reason |
|---|---|---|---|
| Repository visibility | D1 suggested public | **Private** | Stage 5 brief requires private. Consequence: GitHub Free does not allow branch protection or rulesets on private repositories (see Blocked). |
| Reason code for profile-pins-old-ADR (T12) | `PROFILE_REVISION_MISMATCH` | `ADR_REVISION_MISMATCH`; `PROFILE_REVISION_MISMATCH` is used when a task pins an old profile revision | Two different staleness causes need two codes |
| T7 dependent state | "BLOCKED" | `UNKNOWN` when the only blockers are missing evidence, `BLOCKED` when something definitely failed | The brief asks for READY/BLOCKED/STALE/UNKNOWN; both prevent starting |
| "May open a PR" | Always allowed | Requires the task's own approval, architecture gate and freshness, not its dependencies | A stale or unapproved task should not produce work; dependency-independent so mocked-contract work can still be reviewed |
| Spec staleness | Task file approved at hash X vs Y | Both: task file hash vs approval hash (`TASK_CHANGED_SINCE_APPROVAL`) **and** spec file hash vs the hash pinned in the task (`SPEC_CHANGED`) | The brief asks for spec hash staleness; 4B asks for task-file approval staleness |
| ADR revision mismatch in `validate` | Not specified | Warning, exit 0; plan marks dependents STALE | T12 requires dependents to be STALE (a state), not a validation failure |
| `dispatch.enabled: true` | Allowed in manifest | Validation error `DISPATCH_NOT_SUPPORTED` | No dispatch exists; failing closed is more honest than an inert switch |
| Snapshot provenance | Not specified | Schema accepts only `simulated` | Prevents a fixture from claiming authenticated approvals |
| Schema location | `schemas/` | `src/factory/schemas/` | One copy shipped inside the wheel, so an installed CLI and the repository can never drift |

## Deferred (out of Stage 1 scope)

GitHub evidence collector (Stage 2), approval verification from merge records (Stage 3), Archon
adapter, dispatch, run records and reconcile (Stages 5–6), `factory-gate` required check (Stage 8),
staging evidence, contract diffing, constraint compiler, plugin registry, provider routing, BMAD
converter, issue mirroring.

## Rejected

LangGraph, CrewAI, AutoGen, Temporal, databases, servers and schedulers: no need at this scale, and
gating must stay deterministic (4A §11, brief §5).

## Blocked (needs a human decision)

**Branch protection on `kaanf/agentic-factory`.** GitHub refused the API call with HTTP 403: "Upgrade to
GitHub Pro or make this repository public to enable this feature." The `main` / `dev` / `feat/*`
workflow is therefore convention only for this repository. Options: upgrade to GitHub Pro, make the
repository public (4B D1's original recommendation; it contains no secrets), or accept convention-only
protection. Nothing was forced or worked around.
