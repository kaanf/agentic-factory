# Technical debt register

Known limitations carried by Factory Core v0.1, as of Stage 4. Classification:

- **Stage 4 blocker**: invalidates a guarantee of read-only live planning. Must be fixed in Stage 4.
- **Nonblocking hardening**: worth fixing, but every effect fails closed (UNKNOWN or BLOCKED), or
  affects nothing a plan could authorize.
- **Future security prerequisite**: must be resolved before any consumer can act on a plan (Stage 6).

No item is a Stage 4 blocker. Item 6 was narrowed in Stage 4 (end-of-collection head confirmation);
nothing else was changed.

| # | Item | Where | Effect today | Classification |
|---|---|---|---|---|
| 1 | Stage 2 comparison responses are not checked for identity or coherence: `reachableFromIntegrationHead` is read from `status` alone | `collector._RepositoryCollector._reachable` | A wrong or substituted compare response could make `reachable` true, which feeds `validated` and `integration-ready` levels, the `complete` gate and the COMPLETE state. It cannot make anything READY (approvals are unverifiable). Stage 3 already applies the full rules (decision 0003, A17, A19) | Nonblocking for Stage 4; **future security prerequisite** (Stage 6 merge gating depends on dependency levels) |
| 2 | Stage 2 reads any contents 404 as "output absent" | `collector._RepositoryCollector._output` | A 404 for an unconfirmed commit yields FAIL `OUTPUT_MISSING` (BLOCKED) instead of UNKNOWN. Wrong classification, but in the closed direction | Nonblocking hardening |
| 3 | Transport path allowlist uses `re.match` with `$`, which accepts a trailing newline | `github._PATH` | `repos/a/b/pulls\n` passes the allowlist. The method is still hard-coded GET and only `repos/...` and `user` are reachable, so no write is possible | Nonblocking hardening |
| 4 | Object IDs: approvals accept only 40-hex SHA-1; the snapshot schema accepts 40 or 64 hex; JSON Schema `pattern` uses search semantics, so `$` also accepts a trailing newline in `integrationHead` and commit keys; the Stage 2 head is not format-checked | `approvals._SHA`, `evidence-snapshot.v1.schema.json`, `collector.read_integration_head` | A SHA-256 repository would fail closed in Stage 3. A malformed head from GitHub is URL-encoded into later requests, which 404, so its facts are omitted (UNKNOWN). File and live snapshots are validated identically, so neither path bypasses the other | Nonblocking hardening |
| 5 | Compare pagination: GitHub lists at most 250 commits per comparison | `approvals._ApprovalCollector._reachable` (Stage 2 does not look at commits) | Approval entries over longer ranges are omitted (UNKNOWN) | Nonblocking hardening |
| 6 | Cross-phase snapshot atomicity | `collector.collect`, `approvals`, `live._confirm_heads` | Narrowed in Stage 4: every recorded head is re-read after collection, and approvals need a confirmed planning head. A head that moves and moves back between reads is not detected, and the plan is stale the moment it is written | **Future security prerequisite**: action-time revalidation |
| 7 | Simulated evidence authorization boundary | `planning._Evaluator._approval` | `simulated` approvals PASS, so a hand-written snapshot can produce READY. `plan --live` refuses non-`github` evidence, and `authenticated` is always false | **Future security prerequisite**: action-capable consumers must refuse `simulated` provenance |
| 8 | Credential isolation and action-time revalidation | outside Factory Core (4B P1–P3, D4–D8) | Human authorization is unverifiable; every live approval is UNKNOWN | **Future security prerequisite** |
