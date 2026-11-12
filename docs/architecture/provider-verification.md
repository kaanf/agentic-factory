# Provider verification: current evidence and acceptance gap

Status: **open**. Factory Core does not establish which AI provider an Archon run would use, and
reports this on every preflight (`ARCHON_DATABASE_AI_CONFIGURATION_UNOBSERVED`, UNKNOWN). This
document records the evidence, keeps four questions apart, lists what is missing, and proposes a
read-only design **for future approval**. Nothing here is implemented, and no shortcut around the
UNKNOWN is acceptable.

## Four separate questions

| Question | Meaning | Stage 5 answer |
|---|---|---|
| Technical configuration validity | Archon's configuration files are well formed, use only reviewed keys, and their file layers name the task's provider | Checked by preflight (structured parsing, allowlist, F4 layer rules) |
| Provider verification | The provider an actual run would use is established, including every layer Archon applies | **Not established** (UNKNOWN) |
| Operational readiness | Archon, the agent OS user and its provider logins are set up as intended (4B P1-P4) | Not assessed by Factory Core; prerequisites open |
| Execution authorization | Something may act | **Never granted** (`authorization.granted` is structurally false) |

A dry run that passed every technical check would still not verify the provider, and a verified
provider would still not authorize execution. The four are reported separately and none implies
another.

## Evidence

Read from the installed Archon 0.14.0 binary's strings during the first remediation (not by running
it):

- The run's AI configuration is composed from the global config, the repository config, per-codebase
  environment variables from Archon's store (`getCodebaseEnvVars`), per-user AI preferences from its
  store (`getUserAiPrefs(userId)`, applied when a user id is attached to the run and the dependency
  is present), and run layers.
- Per-user preferences outrank the repository assistant: the provider is taken as
  `runLayer.assistant ?? userPrefs.defaultProvider ?? config.assistant`.
- Whether a CLI-started `workflow run` carries a user id was not determined.

Reported by the second independent review (not re-verified here):

- Archon 0.14.0 exposes AI-preference APIs.
- A CLI tier listing exists but may have side effects and fallback behavior.
- Archon's SQLite store may be readable.
- A files store backend is supported, so the store is not always SQLite.
- The effective execution identity and provider of a run remain unverified.

## Missing acceptance criteria

Provider verification is accepted only when all of these hold for the agent OS user (4B P2):

1. **Execution identity**: it is established which user identity, if any, Archon attaches to a
   CLI-started `workflow run` from that OS user, from Archon's code or documentation, not inference.
2. **Store layers**: for that identity and the registered codebase, the per-user AI preferences and
   per-codebase settings are read through an interface that is documented, read-only and free of
   side effects (no writes, no migrations, no locks that block Archon, no fallback defaults).
3. **Store backend**: the active store backend (SQLite or files) is identified, and the reader is
   pinned to its schema version; an unknown backend or version is UNKNOWN.
4. **Composition**: the effective provider for the selected workflow's model chain is computed with
   the same precedence Archon uses, and the computation is cross-checked against Archon's own
   resolution on fixtures.
5. **Binding**: the inputs (config file digests, store snapshot identity, Archon binary identity)
   are recorded in the report so a Stage 6 dispatch can revalidate them under its lock at action
   time.
6. **Fail closed**: any missing, unreadable, ambiguous or version-mismatched input keeps the result
   UNKNOWN.

Alternatively to 2-4: Archon is configured so that per-user preferences and per-codebase AI settings
are provably disabled for the agent identity, and that configuration is itself verified read-only.

## Proposed read-only design (for future approval)

Not part of Stage 5; to be reviewed as its own change.

- Prefer an Archon-provided, documented read-only command that prints a run's effective AI
  configuration as JSON without creating a run or contacting a provider. If none exists, request
  one rather than reading internals.
- If the store must be read directly: copy the store file through Factory Core's confined reader to
  a private temporary location, open the copy read-only, verify the schema version against a pinned
  allowlist, and query only the tables for the identity and codebase from criterion 1. Never open
  Archon's live database file, never run Archon's CLI for this purpose.
- Report a new, separate result (`providerVerification`) next to `dryRun` and `authorization`; it
  never changes either of them.
- Tests: fixture stores for each backend and version, missing and corrupted stores, conflicting
  layers, and an assertion that no file under Archon's home changes.
