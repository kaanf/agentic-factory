# 0007: Stage 5 second security review remediation (R1-R3)

- Status: proposed (for a further independent review on PR #5)
- Date: 2026-11-12
- Reviewed head: `b460a37`. Baseline: `34ef0ae` on `dev`.
- Inputs: the second independent review of PR #5 (three Medium findings), decision 0006, Stage 4B
  §7-§10.
- Supersedes: the probe section of decision 0006 (the executable probe it hardened is removed).

All three findings were reproduced on `b460a37` before any change: R1 and R2 with one script run
unchanged on both heads, R3 by running the script's own guard expression against valid YAML.

| # | Finding | Root cause | Fix |
|---|---|---|---|
| R1 | A corrupted primary `.git/HEAD` was detected (`head_known = false`) but reconcile reported only an INFO note: the real `factory reconcile` CLI returned `NO_RUN`, `blocksDispatch=false`, exit 0 | `observe` never consulted `head_known`; preflight did, so the two disagreed | `observe` adds `CHECKOUT_HEAD_UNKNOWN` (UNKNOWN) when HEAD is neither a branch reference nor an object id, so reconcile is UNKNOWN, blocks, exits 3, and agrees with preflight. A missing, unreadable or linked HEAD was already a checkout-level UNKNOWN. Detached HEADs (an object id) and linked worktrees stay valid |
| R2 | An absolute symbolic link in a configured root (e.g. `--state-dir`) restarted the walk from `/` by name; an ancestor swapped meanwhile made `read_task_records` return a readable, empty history instead of the existing intent record | Following symbolic links in the root path re-resolved ancestors by name, and nothing compared what was found with what was established | Explicit trust contract in `fsread` (documented in archon-adapter.md): ancestors modifiable only by root or the current user; only root-controlled, relative links followed (absolute targets and user-owned links refused); no restart from `/`; parent times and entry identity checked around every lookup (`ROOT_CHANGED_DURING_ACQUISITION`). Refusals surface as unreadable state, never as an empty history |
| R3 | The opt-in probe script recognized hooks by text matching; flow mappings, quoted keys, JSON, anchors with merge keys and complex keys bypassed it, so `postCreate` could run | A blacklist on text, and an execution path whose boundary depends on Archon's effective (partly unreadable) configuration and on files Archon re-reads later | Option A: the executable probe is **removed**; the procedure document records why and the remaining acceptance gap. Option B (structured parsing plus allowlist inside the script) was rejected: the effective merged configuration includes database-held layers, and nothing outside Archon can bind Archon's later read to the inspected bytes. The useful part moved into `factory preflight` as a non-executing guard: structured YAML, ambiguity rejection (anchors, aliases, merge keys, explicit tags, duplicates), and an allowlist (`worktree.baseBranch`, `assistant`, `tiers`, `aliases`) for both the local and the integration-branch configuration; the local file's digest is recorded and re-checked before the report is returned |

## Behavior changes

- Configured roots that pass through a symbolic link owned by the current user, or through a
  directory another user can modify, are refused (`ROOT_PATH_SYMLINK_UNTRUSTED`, `ROOT_PATH_UNSAFE`).
  macOS `/tmp`, `/var` and `/etc` and Linux merged-`/usr` links are root-controlled and still work.
- A concurrent change in a directory of the root path during acquisition now refuses
  (`ROOT_CHANGED_DURING_ACQUISITION`); before, a change to an already-opened ancestor was ignored
  and the original tree read. Three race tests were updated to assert that exact refusal; one test of
  user-owned root links now asserts refusal.
- Preflight fails a repository configuration with hooks or unreviewed keys, and reports its digest
  as `inputs.archonRepoConfigSha256`; the test pinning the `inputs` map now includes it.
- `scripts/probes/` is gone; its 15 tests were removed with it and replaced by tests that no
  executable probe or Archon invocation ships and that the procedure states execution is disabled.

## Provider verification

Unchanged and deliberately so: the effective provider stays UNKNOWN. The evidence, the four
separate questions (configuration validity, provider verification, operational readiness, execution
authorization), the missing acceptance criteria and a read-only design for future approval are in
[provider-verification.md](../architecture/provider-verification.md).

## Not done

Stage 6, execution, ledger writing, reading Archon's store, running any probe.
