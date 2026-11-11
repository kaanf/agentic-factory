"""Approval provenance from merge records (Stage 3), judged end to end by the planner.

Every scenario uses mocked GitHub responses. The central rule under test: GitHub provenance may be
recorded, but a merge by an approver's login is never treated as human approval.
"""

from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import codes
from github_support import (
    BASE,
    FIXTURES,
    REPO,
    FakeTransport,
    Sequence,
    commit_entry,
    comparison,
    contents,
    git_commit,
    identity,
    listing,
    not_found,
    page,
    pull,
    pull_detail,
    q,
    ref,
)
from support import THREE_REPO

from factory.approvals import approval_paths, collect_approvals
from factory.cli import run
from factory.collector import collect
from factory.evidence import load_snapshot
from factory.github import GitHubApi, GitHubError, Response
from factory.planning import evaluate
from factory.project import load_project


def sha(label: str) -> str:
    return hashlib.sha1(label.encode()).hexdigest()  # noqa: S324 - fake commit ids


HEAD = sha("dev head")
MERGE = sha("merge commit of #7")
FEATURE = sha("second commit on the feature branch")
PARENT = sha("dev before #7")
LATER = sha("direct push after #7")
PROFILE_COMMIT = sha("profile commit")
PROFILE_MERGE = sha("merge commit of #6")
CLOCK = lambda: datetime(2026, 11, 11, 12, 0, tzinfo=UTC)  # noqa: E731
TASK = "factory/tasks/SVC-1.yaml"
PROFILE = "factory/profiles/svc.yaml"
BRANCH = "feat/SVC-1-r1"
PROFILE_TEXT = "schemaVersion: factory.profile/v1\nid: svc-arch\nrevision: 1\nrepository: svc\nadrs: []\n"


def _task_text(spec_digest: str) -> str:
    return f"""schemaVersion: factory.task/v1
id: SVC-1
revision: 1
title: Stock endpoint
repository: svc
kind: implementation
specification: {{path: specs/svc-1.md, sha256: {spec_digest}}}
architecture: {{profile: svc-arch, profileRevision: 1}}
acceptance: [GET /items works]
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "svc-project"
    for directory in ("factory/tasks", "factory/profiles", "specs"):
        (root / directory).mkdir(parents=True)
    (root / "factory/project.yaml").write_text(
        f"""schemaVersion: factory.project/v1
project: {{id: svc-project}}
approvers: [example-human]
planningRepository: svc
repositories:
  - id: svc
    github: {REPO}
    branches: {{integration: dev, release: main, featurePrefix: feat/}}
    requiredChecks:
      integration:
        - {{name: "Svc Check", workflowPath: .github/workflows/ci.yml}}
architecture:
  adrDirectory: docs/adr
  profiles:
    - {{id: svc-arch, repository: svc, path: {PROFILE}}}
dispatch: {{enabled: false}}
"""
    )
    (root / PROFILE).write_text(PROFILE_TEXT)
    (root / "specs/svc-1.md").write_text("# Stock endpoint\n")
    (root / TASK).write_text(_task_text(hashlib.sha256(b"# Stock endpoint\n").hexdigest()))
    return root


def text_of(project: Path, path: str) -> str:
    return (project / path).read_text()


# -- mocked GitHub ----------------------------------------------------------------------------


def base_world(head: str = HEAD, user: Any = None) -> dict[str, Any]:
    """Repository facts for an empty task branch, plus identity and head for approvals."""
    return {
        BASE: identity(),
        f"{BASE}/git/ref/heads/dev": ref(head),
        page(f"{BASE}/pulls", head=f"example-org:{BRANCH}", state="all"): [],
        page(f"{BASE}/commits/{head}/pulls"): [],
        page(f"{BASE}/commits/{head}/check-runs", filter="all"): listing("check_runs", []),
        q(f"{BASE}/compare/{head}...{head}"): comparison(head, head, "identical"),
        f"{BASE}/git/commits/{head}": git_commit(head),
        "user": Response(401, {"message": "Requires authentication"}) if user is None else user,
    }


def artifact(
    path: str,
    text: str,
    *,
    head: str = HEAD,
    commit: str = FEATURE,
    number: int = 7,
    merge: str = MERGE,
    associated: list[dict[str, Any]] | None = None,
    detail: dict[str, Any] | None = None,
    merge_text: str | None = None,
    parent_text: str | None = None,
    compare: str = "ahead",
) -> dict[str, Any]:
    """Responses for a file whose content at ``head`` was last changed by ``commit``, part of PR #``number``."""
    return {
        q(f"{BASE}/contents/{path}", ref=head): contents(text),
        q(f"{BASE}/commits", path=path, per_page=1, sha=head): [commit_entry(commit)],
        page(f"{BASE}/commits/{commit}/pulls"): [pull(number, BRANCH, merged_sha=merge)]
        if associated is None
        else associated,
        f"{BASE}/pulls/{number}": pull_detail(number, BRANCH, merged_sha=merge) if detail is None else detail,
        q(f"{BASE}/compare/{merge}...{head}"): comparison(merge, head, compare),
        q(f"{BASE}/contents/{path}", ref=merge): contents(text if merge_text is None else merge_text),
        f"{BASE}/commits/{merge}": commit_entry(merge, [PARENT]),
        q(f"{BASE}/contents/{path}", ref=PARENT): not_found() if parent_text is None else contents(parent_text),
        f"{BASE}/git/commits/{PARENT}": git_commit(PARENT),
    }


def approved_world(project: Path, **task_overrides: Any) -> dict[str, Any]:
    """Both artifacts introduced by PR #7, merged by the approver; ``task_overrides`` change the task file's records."""
    responses = base_world(user=task_overrides.pop("user", None))
    responses.update(artifact(PROFILE, PROFILE_TEXT, commit=PROFILE_COMMIT, number=6, merge=PROFILE_MERGE))
    responses.update(artifact(TASK, text_of(project, TASK), **task_overrides))
    return responses


def collected(project: Path, responses: dict[str, Any]) -> tuple[dict[str, Any], FakeTransport]:
    loaded = load_project(project).project
    assert loaded is not None
    transport = FakeTransport(responses)
    return collect(loaded, GitHubApi(transport), now=CLOCK).snapshot, transport


def entry(snapshot: dict[str, Any], path: str = TASK) -> dict[str, Any] | None:
    return next((a for a in snapshot["approvals"] if a["path"] == path), None)


def planned(project: Path, snapshot: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    loaded = load_project(project).project
    assert loaded is not None
    file = tmp_path / "snapshot.json"
    file.write_text(json.dumps(snapshot))
    result = load_snapshot(file, loaded)
    assert result.errors == [], result.errors
    assert result.snapshot is not None
    return evaluate(loaded, result.snapshot).tasks[0].to_dict()


def approval_reasons(task: dict[str, Any]) -> tuple[str, set[str]]:
    level = task["levels"]["approved"]
    return level["result"], codes(level["reasons"])


# -- valid provenance is recorded, but never counts as human approval ---------------------------


@pytest.mark.parametrize(
    ("commit", "merge"),
    [
        pytest.param(FEATURE, MERGE, id="merge-commit-over-several-feature-commits"),
        pytest.param(MERGE, MERGE, id="squash-merge"),
    ],
)
def test_artifact_introduced_by_merged_pr_records_provenance_only(
    project: Path, tmp_path: Path, commit: str, merge: str
) -> None:
    responses = base_world()
    responses.update(artifact(PROFILE, PROFILE_TEXT, commit=commit, merge=merge))
    responses.update(artifact(TASK, text_of(project, TASK), commit=commit, merge=merge))
    snapshot, _ = collected(project, responses)
    assert entry(snapshot) == {
        "approved": True,
        "commit": commit,
        "evidence": "MERGED_PULL_REQUEST",
        "humanAuthorization": "unverifiable",
        "mergeCommit": merge,
        "mergedBy": "example-human",
        "path": TASK,
        "pullRequest": 7,
        "sha256": hashlib.sha256((project / TASK).read_bytes()).hexdigest(),
    }
    assert snapshot["collection"]["complete"] is True
    task = planned(project, snapshot, tmp_path)
    assert approval_reasons(task) == ("UNKNOWN", {"HUMAN_AUTHORIZATION_UNVERIFIABLE"})
    assert task["state"] == "UNKNOWN"


def test_unverifiable_approval_never_makes_an_implementation_task_ready(project: Path, tmp_path: Path) -> None:
    snapshot, _ = collected(project, approved_world(project, user={"login": "example-human"}))
    task = planned(project, snapshot, tmp_path)
    assert task["state"] != "READY"
    for gate in ("start", "openPullRequest", "merge"):
        assert task["gates"][gate]["result"] == "UNKNOWN"
        assert "HUMAN_AUTHORIZATION_UNVERIFIABLE" in codes(task["gates"][gate]["reasons"])
    # The architecture profile is held to the same rule.
    assert "PROFILE_NOT_APPROVED" in codes(task["gates"]["start"]["reasons"])


def test_same_facts_with_simulated_approvals_are_ready(project: Path, tmp_path: Path) -> None:
    """Control: the only thing keeping the task from READY above is the trust boundary."""
    snapshot, _ = collected(project, approved_world(project))
    simulated = {
        **snapshot,
        "provenance": {"kind": "simulated"},
        "approvals": [
            {k: v for k, v in a.items() if k not in ("humanAuthorization", "evidence")} for a in snapshot["approvals"]
        ],
    }
    assert planned(project, simulated, tmp_path)["state"] == "READY"


# -- credential isolation ----------------------------------------------------------------------


def test_shared_human_and_agent_identity_is_detected(project: Path, tmp_path: Path) -> None:
    snapshot, _ = collected(project, approved_world(project, user={"login": "example-human"}))
    isolation = snapshot["credentialIsolation"]
    assert isolation["status"] == "shared"
    assert isolation["identity"] == "example-human"
    assert "credentials are shared" in isolation["message"]
    assert entry(snapshot)["humanAuthorization"] == "unverifiable"


@pytest.mark.parametrize(
    ("user", "identity_login"),
    [
        pytest.param(Response(401, {"message": "Requires authentication"}), None, id="identity-unreadable"),
        pytest.param({"login": "factory-bot"}, "factory-bot", id="non-approver-identity"),
    ],
)
def test_missing_credential_isolation_evidence_stays_unverifiable(
    project: Path, tmp_path: Path, user: Any, identity_login: str | None
) -> None:
    """A non-approver collector identity is not evidence of isolation: agents may hold other credentials."""
    snapshot, _ = collected(project, approved_world(project, user=user))
    assert snapshot["credentialIsolation"]["status"] == "unverified"
    assert snapshot["credentialIsolation"]["identity"] == identity_login
    assert approval_reasons(planned(project, snapshot, tmp_path))[0] == "UNKNOWN"


def test_collected_snapshot_with_the_marker_removed_is_still_unverifiable(project: Path, tmp_path: Path) -> None:
    snapshot, _ = collected(project, approved_world(project))
    for approval in snapshot["approvals"]:
        del approval["humanAuthorization"]
    assert "HUMAN_AUTHORIZATION_UNVERIFIABLE" in approval_reasons(planned(project, snapshot, tmp_path))[1]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("humanAuthorization", "verified"),
        ("credentialIsolation", {"status": "isolated", "identity": "factory-bot", "message": ""}),
    ],
)
def test_snapshot_schema_has_no_verified_human_authorization(
    project: Path, tmp_path: Path, field: str, value: Any
) -> None:
    snapshot, _ = collected(project, approved_world(project))
    if field == "humanAuthorization":
        snapshot["approvals"][0][field] = value
    else:
        snapshot[field] = value
    file = tmp_path / "forged.json"
    file.write_text(json.dumps(snapshot))
    loaded = load_project(project).project
    assert loaded is not None
    assert load_snapshot(file, loaded).errors


# -- definite negative findings: written with approved=false, the task is BLOCKED --------------


def test_wrong_merge_actor(project: Path, tmp_path: Path) -> None:
    detail = pull_detail(7, BRANCH, merged_sha=MERGE, merged_by="factory-bot")
    snapshot, _ = collected(project, approved_world(project, detail=detail))
    assert entry(snapshot)["mergedBy"] == "factory-bot"
    task = planned(project, snapshot, tmp_path)
    assert approval_reasons(task) == ("FAIL", {"APPROVER_NOT_AUTHORIZED"})
    assert task["state"] == "BLOCKED"


@pytest.mark.parametrize(
    ("associated", "evidence"),
    [
        pytest.param([], "NO_PULL_REQUEST", id="direct-push-without-pr"),
        pytest.param([pull(7, BRANCH, state="open")], "PR_NOT_MERGED", id="unmerged-pr"),
        pytest.param([pull(7, BRANCH, state="closed")], "PR_NOT_MERGED", id="closed-unmerged-pr"),
        pytest.param(
            [pull(7, BRANCH, base="main", merged_sha=MERGE)], "PR_NOT_INTO_INTEGRATION_BRANCH", id="wrong-base"
        ),
    ],
)
def test_content_not_introduced_by_a_merged_pr_into_dev(
    project: Path, tmp_path: Path, associated: list[dict[str, Any]], evidence: str
) -> None:
    snapshot, transport = collected(project, approved_world(project, associated=associated))
    approval = entry(snapshot)
    assert approval is not None
    assert (approval["approved"], approval["evidence"]) == (False, evidence)
    assert "mergedBy" not in approval
    assert f"{BASE}/pulls/7" not in transport.requests
    task = planned(project, snapshot, tmp_path)
    assert approval_reasons(task) == ("FAIL", {"NOT_APPROVED"})
    assert task["state"] == "BLOCKED"


def test_artifact_modified_on_dev_after_approval_by_direct_push(project: Path, tmp_path: Path) -> None:
    """The approving PR is no longer the latest change: the direct push that followed is."""
    snapshot, _ = collected(project, approved_world(project, commit=LATER, associated=[]))
    assert (entry(snapshot)["commit"], entry(snapshot)["evidence"]) == (LATER, "NO_PULL_REQUEST")


def test_content_already_on_dev_before_the_pr_was_merged(project: Path, tmp_path: Path) -> None:
    snapshot, _ = collected(project, approved_world(project, parent_text=text_of(project, TASK)))
    approval = entry(snapshot)
    assert (approval["approved"], approval["evidence"], approval["pullRequest"]) == (
        False,
        "CONTENT_PRESENT_BEFORE_MERGE",
        7,
    )


def test_artifact_not_on_the_integration_branch(project: Path, tmp_path: Path) -> None:
    responses = approved_world(project)
    responses[q(f"{BASE}/contents/{TASK}", ref=HEAD)] = not_found()
    snapshot, _ = collected(project, responses)
    approval = entry(snapshot)
    assert (approval["approved"], approval["evidence"]) == (False, "NOT_ON_INTEGRATION_BRANCH")
    assert approval["sha256"] == hashlib.sha256((project / TASK).read_bytes()).hexdigest()


# -- staleness ----------------------------------------------------------------------------------


def test_local_artifact_modified_after_approval_is_stale(project: Path, tmp_path: Path) -> None:
    approved_text = text_of(project, TASK)
    responses = approved_world(project)
    (project / TASK).write_text(approved_text.replace("GET /items works", "GET /items and /stock work"))
    snapshot, _ = collected(project, responses)
    assert entry(snapshot)["sha256"] == hashlib.sha256(approved_text.encode()).hexdigest()
    task = planned(project, snapshot, tmp_path)
    assert task["state"] == "STALE"
    assert "TASK_CHANGED_SINCE_APPROVAL" in codes(task["stale"])


def test_invalid_artifact_hash_in_snapshot_is_rejected(project: Path, tmp_path: Path) -> None:
    snapshot, _ = collected(project, approved_world(project))
    snapshot["approvals"][0]["sha256"] = "not-a-hash"
    file = tmp_path / "bad.json"
    file.write_text(json.dumps(snapshot))
    loaded = load_project(project).project
    assert loaded is not None
    assert [e.code for e in load_snapshot(file, loaded).errors] == ["INVALID_FORMAT"]


# -- missing, contradictory or ambiguous evidence: omitted, the task is UNKNOWN ----------------


def _no_merge_commit() -> dict[str, Any]:
    detail = pull_detail(7, BRANCH, merged_sha=MERGE)
    detail["merge_commit_sha"] = None
    return detail


def _contradictory_detail() -> dict[str, Any]:
    detail = pull_detail(7, BRANCH, merged_sha=None)
    return detail


def _blob_mismatch(project: Path) -> dict[str, Any]:
    body = contents(text_of(project, TASK))
    body["sha"] = "0" * 40
    return body


@pytest.mark.parametrize(
    ("change", "message"),
    [
        pytest.param(
            {"detail": pull_detail(7, BRANCH, merged_sha=MERGE, merged_by=None)},
            "no recorded merge commit or merge actor",
            id="missing-merge-actor",
        ),
        pytest.param(
            {"detail": _no_merge_commit()}, "no recorded merge commit or merge actor", id="missing-merge-commit"
        ),
        pytest.param(
            {"associated": [pull(7, BRANCH, merged_sha=MERGE), pull(8, "feat/other-r1", merged_sha=sha("m8"))]},
            "several merged pull requests (#7, #8)",
            id="ambiguous-pr-association",
        ),
        pytest.param(
            {"detail": _contradictory_detail()},
            "listed as merged into the integration branch, but is not",
            id="list-and-detail-disagree",
        ),
        pytest.param(
            {"merge_text": "something else\n"}, "differs from the integration head", id="merge-content-differs"
        ),
        pytest.param({"compare": "diverged"}, "not reachable from the integration head", id="merge-not-on-dev"),
    ],
)
def test_incomplete_or_contradictory_records_are_omitted(
    project: Path, tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    snapshot, _ = collected(project, approved_world(project, **change))
    assert entry(snapshot) is None
    assert entry(snapshot, PROFILE) is not None  # only the affected artifact is dropped
    errors = snapshot["collection"]["errors"]
    assert [e["fact"] for e in errors] == [f"approvals.{TASK}"]
    assert message in errors[0]["message"]
    task = planned(project, snapshot, tmp_path)
    assert approval_reasons(task) == ("UNKNOWN", {"APPROVAL_EVIDENCE_MISSING"})
    assert task["state"] == "UNKNOWN"


def test_content_not_matching_its_blob_sha_is_omitted(project: Path) -> None:
    responses = approved_world(project)
    responses[q(f"{BASE}/contents/{TASK}", ref=HEAD)] = _blob_mismatch(project)
    snapshot, _ = collected(project, responses)
    assert entry(snapshot) is None
    assert "does not match its blob SHA" in snapshot["collection"]["errors"][0]["message"]


def test_no_commit_recorded_for_a_file_on_dev_is_omitted(project: Path) -> None:
    responses = approved_world(project)
    responses[q(f"{BASE}/commits", path=TASK, per_page=1, sha=HEAD)] = []
    snapshot, _ = collected(project, responses)
    assert entry(snapshot) is None


def test_integration_head_moving_drops_every_approval(project: Path) -> None:
    moved = sha("dev moved")
    responses = approved_world(project)
    # Two reads by the repository collector, then before/after reads by the approval collector.
    responses[f"{BASE}/git/ref/heads/dev"] = Sequence([ref(HEAD), ref(HEAD), ref(HEAD), ref(moved)])
    snapshot, _ = collected(project, responses)
    assert snapshot["approvals"] == []
    assert {
        "fact": "approvals",
        "message": f"integration head moved from {HEAD} to {moved} during collection",
        "repository": "svc",
    } in snapshot["collection"]["errors"]


# -- malformed records: never positive provenance, never an unjustified definite rejection -----


def _listed(**fields: Any) -> list[dict[str, Any]]:
    """PR #7 as the commit's associated-PR listing returns it, with ``fields`` replaced."""
    return [{**pull(7, BRANCH, merged_sha=MERGE), **fields}]


def _listed_without(field: str) -> list[dict[str, Any]]:
    record = pull(7, BRANCH, merged_sha=MERGE)
    del record[field]
    return [record]


def _detail(**fields: Any) -> dict[str, Any]:
    """PR #7's detail record, with ``fields`` replaced."""
    return {**pull_detail(7, BRANCH, merged_sha=MERGE), **fields}


def _detail_without(field: str) -> dict[str, Any]:
    record = pull_detail(7, BRANCH, merged_sha=MERGE)
    del record[field]
    return record


def assert_omitted(project: Path, tmp_path: Path, snapshot: dict[str, Any], message: str) -> None:
    assert entry(snapshot) is None
    assert entry(snapshot, PROFILE) is not None  # only the affected artifact is dropped
    errors = snapshot["collection"]["errors"]
    assert [e["fact"] for e in errors] == [f"approvals.{TASK}"]
    assert message in errors[0]["message"]
    assert snapshot["collection"]["complete"] is False
    task = planned(project, snapshot, tmp_path)
    assert approval_reasons(task) == ("UNKNOWN", {"APPROVAL_EVIDENCE_MISSING"})
    assert task["state"] == "UNKNOWN"


NO_FIRST_PARENT = "has no valid first parent"


@pytest.mark.parametrize(
    ("record", "message"),
    [
        pytest.param({"parents": None, "sha": MERGE}, NO_FIRST_PARENT, id="parents-null"),
        pytest.param({"parents": {}, "sha": MERGE}, NO_FIRST_PARENT, id="parents-empty-object"),
        pytest.param({"parents": False, "sha": MERGE}, NO_FIRST_PARENT, id="parents-false"),
        pytest.param({"parents": [], "sha": MERGE}, NO_FIRST_PARENT, id="parents-empty-array"),
        pytest.param({"sha": MERGE}, NO_FIRST_PARENT, id="parents-missing"),
        pytest.param({"parents": [PARENT], "sha": MERGE}, NO_FIRST_PARENT, id="first-parent-not-a-record"),
        pytest.param({"parents": [{"url": "x"}], "sha": MERGE}, NO_FIRST_PARENT, id="first-parent-without-sha"),
        pytest.param({"parents": [{"sha": "HEAD~1"}], "sha": MERGE}, NO_FIRST_PARENT, id="first-parent-sha-malformed"),
        pytest.param(
            {"parents": [{"sha": PARENT + "\n"}], "sha": MERGE}, NO_FIRST_PARENT, id="first-parent-sha-newline"
        ),
        pytest.param(
            {"parents": [{"sha": "\n" + PARENT}], "sha": MERGE}, NO_FIRST_PARENT, id="first-parent-sha-leading-newline"
        ),
        pytest.param(
            {"parents": [{"sha": PARENT + "\r\n"}], "sha": MERGE}, NO_FIRST_PARENT, id="first-parent-sha-crlf"
        ),
        pytest.param(
            {"parents": [{"sha": PARENT + " "}], "sha": MERGE}, NO_FIRST_PARENT, id="first-parent-sha-trailing-space"
        ),
        pytest.param(
            {"parents": [{"sha": PARENT.upper()}], "sha": MERGE}, NO_FIRST_PARENT, id="first-parent-sha-uppercase"
        ),
        pytest.param(
            {"parents": [{"sha": PARENT[:39]}], "sha": MERGE}, NO_FIRST_PARENT, id="first-parent-sha-too-short"
        ),
        pytest.param(
            {"parents": [{"sha": PARENT + "0"}], "sha": MERGE}, NO_FIRST_PARENT, id="first-parent-sha-too-long"
        ),
        pytest.param({"parents": [{"sha": PARENT}], "sha": FEATURE}, "describes another commit", id="other-commit"),
        pytest.param({"parents": [{"sha": PARENT}]}, "describes another commit", id="commit-sha-missing"),
        pytest.param([], "describes another commit", id="not-a-commit-record"),
    ],
)
def test_merge_commit_without_a_valid_first_parent_is_omitted(
    project: Path, tmp_path: Path, record: Any, message: str
) -> None:
    """Without the first parent, nothing shows that this PR introduced the content."""
    responses = approved_world(project)
    responses[f"{BASE}/commits/{MERGE}"] = record
    snapshot, _ = collected(project, responses)
    assert_omitted(project, tmp_path, snapshot, message)


def _parent_with_newline(project: Path) -> dict[str, Any]:
    """The task is already at PARENT, but the commit record names PARENT followed by a newline.

    Looked up, that malformed ref would 404 and read as "absent before the merge"."""
    responses = approved_world(project, parent_text=text_of(project, TASK))
    responses[f"{BASE}/commits/{MERGE}"] = commit_entry(MERGE, [PARENT + "\n"])
    responses[q(f"{BASE}/contents/{TASK}", ref=PARENT + "\n")] = not_found()
    return responses


def test_first_parent_sha_with_trailing_newline_is_rejected_before_any_lookup(project: Path, tmp_path: Path) -> None:
    control, _ = collected(project, approved_world(project, parent_text=text_of(project, TASK)))
    assert entry(control)["evidence"] == "CONTENT_PRESENT_BEFORE_MERGE"  # the same facts with a valid parent SHA

    snapshot, transport = collected(project, _parent_with_newline(project))
    assert_omitted(project, tmp_path, snapshot, NO_FIRST_PARENT)
    assert not any(r.startswith(f"{BASE}/contents/") and "%0A" in r for r in transport.requests)


def test_cli_reports_first_parent_sha_with_trailing_newline_with_exit_3(project: Path, tmp_path: Path) -> None:
    out, err = io.StringIO(), io.StringIO()
    output = tmp_path / "s.json"
    code = run(
        ["evidence", "collect", str(project), "-o", str(output)],
        out=out,
        err=err,
        transport=FakeTransport(_parent_with_newline(project)),
    )
    assert code == 3
    assert f"incomplete: svc: approvals.{TASK}: merge commit {MERGE} of #7 has no valid first parent" in err.getvalue()
    assert entry(json.loads(output.read_text())) is None


@pytest.mark.parametrize("suffix", ["\n", "\r\n", " "])
def test_merge_commit_sha_with_trailing_whitespace_is_rejected_before_any_lookup(
    project: Path, tmp_path: Path, suffix: str
) -> None:
    """Listing and detail agree on the malformed SHA, so only the format check can catch it."""
    malformed = MERGE + suffix
    snapshot, transport = collected(
        project,
        approved_world(
            project, associated=_listed(merge_commit_sha=malformed), detail=_detail(merge_commit_sha=malformed)
        ),
    )
    assert_omitted(project, tmp_path, snapshot, "no recorded merge commit or merge actor")
    assert not any(r.startswith((f"{BASE}/compare/{MERGE}", f"{BASE}/commits/{MERGE}")) for r in transport.requests)
    assert q(f"{BASE}/contents/{TASK}", ref=malformed) not in transport.requests


def test_integration_head_sha_with_trailing_newline_is_not_read_as_absent_content(
    project: Path, tmp_path: Path
) -> None:
    """A malformed head ref would 404 every file and read as a definite NOT_ON_INTEGRATION_BRANCH."""
    malformed = HEAD + "\n"
    responses = approved_world(project)
    # Two reads by the repository collector, then the approval collector's reads.
    responses[f"{BASE}/git/ref/heads/dev"] = Sequence([ref(HEAD), ref(HEAD), ref(malformed)])
    snapshot, transport = collected(project, responses)
    assert snapshot["approvals"] == []
    assert snapshot["collection"]["complete"] is False
    assert any("malformed commit SHA" in e["message"] for e in snapshot["collection"]["errors"])
    assert not any("%0A" in r for r in transport.requests)
    assert approval_reasons(planned(project, snapshot, tmp_path)) == ("UNKNOWN", {"APPROVAL_EVIDENCE_MISSING"})


def test_latest_commit_sha_with_trailing_newline_is_omitted(project: Path, tmp_path: Path) -> None:
    responses = approved_world(project)
    responses[q(f"{BASE}/commits", path=TASK, per_page=1, sha=HEAD)] = [commit_entry(FEATURE + "\n")]
    snapshot, transport = collected(project, responses)
    assert_omitted(project, tmp_path, snapshot, "has a malformed SHA")
    assert not any("%0A" in r for r in transport.requests)


def test_valid_two_parent_merge_commit_is_checked_against_its_first_parent(project: Path) -> None:
    responses = approved_world(project)
    responses[f"{BASE}/commits/{MERGE}"] = commit_entry(MERGE, [PARENT, FEATURE])
    snapshot, transport = collected(project, responses)
    assert (entry(snapshot)["approved"], entry(snapshot)["evidence"]) == (True, "MERGED_PULL_REQUEST")
    assert q(f"{BASE}/contents/{TASK}", ref=PARENT) in transport.requests
    assert snapshot["collection"]["complete"] is True


@pytest.mark.parametrize(
    ("change", "message"),
    [
        pytest.param({"detail": _detail(number=999)}, "GitHub returned #999", id="detail-wrong-number"),
        pytest.param(
            {"detail": pull_detail(7, BRANCH, merged_sha=MERGE, base_repo="other/repo")},
            "targets repository 'other/repo'",
            id="detail-wrong-base-repository",
        ),
        pytest.param(
            {"associated": [pull(7, BRANCH, merged_sha=MERGE, base_repo="other/repo")]},
            "targets repository 'other/repo'",
            id="listing-wrong-base-repository",
        ),
        pytest.param(
            {"detail": pull_detail(7, BRANCH, merged_sha=MERGE, base="main")},
            "listed as merged into the integration branch, but is not",
            id="detail-wrong-base-branch",
        ),
        pytest.param(
            {"detail": _detail(merge_commit_sha=sha("another merge"))},
            "two different merge commits",
            id="listing-and-detail-merge-commits-differ",
        ),
        pytest.param(
            {"detail": _detail(merge_commit_sha="not-a-sha")},
            "no recorded merge commit or merge actor",
            id="detail-merge-commit-malformed",
        ),
        pytest.param(
            {"detail": _detail(merged_by={"login": 42})},
            "no recorded merge commit or merge actor",
            id="detail-merge-actor-malformed",
        ),
        pytest.param({"detail": _detail_without("number")}, "no valid number", id="detail-number-missing"),
        pytest.param({"detail": _detail(number="7")}, "no valid number", id="detail-number-not-an-integer"),
        pytest.param({"detail": _detail_without("base")}, "no valid number", id="detail-base-missing"),
        pytest.param(
            {"detail": _detail(base={"ref": "dev"})}, "targets repository None", id="detail-base-repository-missing"
        ),
        pytest.param(
            {"associated": _listed(base={"ref": "dev"})},
            "targets repository None",
            id="listing-base-repository-missing",
        ),
        pytest.param({"associated": _listed_without("number")}, "no valid number", id="listing-number-missing"),
        pytest.param({"associated": [None]}, "not a pull request record", id="listing-record-not-an-object"),
        pytest.param(
            {"associated": [pull(7, BRANCH, base="", merged_sha=MERGE)]},
            "no valid number or base branch",
            id="listing-base-branch-empty",
        ),
        pytest.param(
            {"associated": [pull(7, BRANCH, base="", state="open")]},
            "no valid number or base branch",
            id="unmerged-listing-base-branch-empty",
        ),
        pytest.param(
            {"associated": _listed(base={"repo": {"full_name": REPO}})},
            "no valid number or base branch",
            id="listing-base-branch-missing",
        ),
        pytest.param(
            {"associated": _listed(base={"ref": None, "repo": {"full_name": REPO}})},
            "no valid number or base branch",
            id="listing-base-branch-null",
        ),
        pytest.param(
            {"associated": _listed(base={"ref": 7, "repo": {"full_name": REPO}})},
            "no valid number or base branch",
            id="listing-base-branch-not-a-string",
        ),
        pytest.param(
            {"detail": pull_detail(7, BRANCH, merged_sha=MERGE, base="")},
            "no valid number or base branch",
            id="detail-base-branch-empty",
        ),
    ],
)
def test_contradictory_or_incomplete_pull_request_identity_is_omitted(
    project: Path, tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    snapshot, _ = collected(project, approved_world(project, **change))
    assert_omitted(project, tmp_path, snapshot, message)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        pytest.param({"associated": _listed_without("merged_at")}, "no merge status", id="listing-merged-at-missing"),
        pytest.param(
            {"associated": _listed(merged_at="yesterday")}, "malformed merge time", id="listing-merged-at-malformed"
        ),
        pytest.param({"associated": _listed(merged_at="")}, "malformed merge time", id="listing-merged-at-empty"),
        pytest.param(
            {"associated": _listed(merged_at=1762844400)}, "malformed merge time", id="listing-merged-at-number"
        ),
        pytest.param({"associated": _listed(merged_at=False)}, "malformed merge time", id="listing-merged-at-false"),
        pytest.param(
            {"associated": [{**pull(7, BRANCH), "state": None}]}, "no valid state", id="unmerged-listing-state-missing"
        ),
        pytest.param({"associated": _listed(state="open")}, "contradictory merge status", id="open-but-merged"),
        pytest.param(
            {"associated": _listed(merged=False)}, "contradictory merge status", id="listing-merged-flag-disagrees"
        ),
        pytest.param(
            {"associated": [{**pull(7, BRANCH), "merged": True}]},
            "contradictory merge status",
            id="merged-flag-without-merge-time",
        ),
        pytest.param(
            {"detail": _detail(merged_at=None)},
            "contradictory merge status",
            id="detail-merged-without-merge-time",
        ),
        pytest.param(
            {"detail": _detail(merged_at="2026-11-11T08:00:00Z")},
            "two different merge times",
            id="listing-and-detail-merge-times-differ",
        ),
    ],
)
def test_missing_or_contradictory_merge_metadata_is_unknown_not_blocked(
    project: Path, tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    snapshot, _ = collected(project, approved_world(project, **change))
    assert_omitted(project, tmp_path, snapshot, message)


def test_explicitly_unmerged_pr_alongside_unknown_merge_status_is_not_a_definite_rejection(
    project: Path, tmp_path: Path
) -> None:
    """One genuinely unmerged PR does not settle the question while another's status is unknown."""
    unknown = pull(8, "feat/other-r1", merged_sha=sha("m8"))
    del unknown["merged_at"]
    snapshot, _ = collected(project, approved_world(project, associated=[pull(7, BRANCH), unknown]))
    assert_omitted(project, tmp_path, snapshot, "pull request #8 has no merge status")


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"detail": _detail(number=999)}, id="pr-identity"),
        pytest.param({"associated": _listed_without("merged_at")}, id="merge-metadata"),
    ],
)
def test_cli_reports_malformed_records_with_exit_3(project: Path, tmp_path: Path, change: dict[str, Any]) -> None:
    responses = approved_world(project, **change)
    out, err = io.StringIO(), io.StringIO()
    output = tmp_path / "s.json"
    code = run(
        ["evidence", "collect", str(project), "-o", str(output)], out=out, err=err, transport=FakeTransport(responses)
    )
    assert code == 3
    assert f"incomplete: svc: approvals.{TASK}:" in err.getvalue()
    assert entry(json.loads(output.read_text())) is None


def test_cli_reports_empty_base_branch_with_exit_3(project: Path, tmp_path: Path) -> None:
    responses = approved_world(project, associated=[pull(7, BRANCH, base="", merged_sha=MERGE)])
    out, err = io.StringIO(), io.StringIO()
    output = tmp_path / "s.json"
    code = run(
        ["evidence", "collect", str(project), "-o", str(output)], out=out, err=err, transport=FakeTransport(responses)
    )
    assert code == 3
    assert f"incomplete: svc: approvals.{TASK}: an associated pull request has no valid" in err.getvalue()
    assert entry(json.loads(output.read_text())) is None


def test_cli_reports_missing_parent_records_with_exit_3(project: Path, tmp_path: Path) -> None:
    responses = approved_world(project)
    responses[f"{BASE}/commits/{MERGE}"] = {"parents": None, "sha": MERGE}
    out, err = io.StringIO(), io.StringIO()
    output = tmp_path / "s.json"
    code = run(
        ["evidence", "collect", str(project), "-o", str(output)], out=out, err=err, transport=FakeTransport(responses)
    )
    assert code == 3
    assert entry(json.loads(output.read_text())) is None


# -- a 404 proves absence only at a commit shown to exist -------------------------------------

GHOST = sha("a commit that does not exist")
OTHER = sha("some other commit")


def cli_collect(project: Path, tmp_path: Path, responses: dict[str, Any]) -> tuple[int, str, dict[str, Any]]:
    out, err = io.StringIO(), io.StringIO()
    output = tmp_path / "cli.json"
    code = run(
        ["evidence", "collect", str(project), "-o", str(output)], out=out, err=err, transport=FakeTransport(responses)
    )
    return code, err.getvalue(), json.loads(output.read_text())


def _ghost_parent(project: Path, lookup: Any = None) -> dict[str, Any]:
    """The merge commit names a well-formed first parent that GitHub does not have."""
    responses = approved_world(project, parent_text=text_of(project, TASK))
    responses[f"{BASE}/commits/{MERGE}"] = commit_entry(MERGE, [GHOST])
    responses[q(f"{BASE}/contents/{TASK}", ref=GHOST)] = not_found()
    responses[f"{BASE}/git/commits/{GHOST}"] = not_found() if lookup is None else lookup
    return responses


DOES_NOT_EXIST = "does not exist or is not accessible"


@pytest.mark.parametrize(
    ("lookup", "message"),
    [
        pytest.param(None, f"commit {GHOST} {DOES_NOT_EXIST}", id="nonexistent"),
        pytest.param(
            Response(403, {"message": "Resource not accessible by integration"}), "HTTP 403", id="inaccessible"
        ),
        pytest.param(Response(500, {"message": "Server Error"}), "HTTP 500", id="server-error"),
        pytest.param(GitHubError("gh api failed: connection reset"), "connection reset", id="transport-failure"),
        pytest.param(git_commit(OTHER), "describes another commit", id="record-of-another-commit"),
        pytest.param({"sha": GHOST}, "describes another commit", id="record-without-tree"),
        pytest.param(git_commit(GHOST, tree="not-a-tree"), "describes another commit", id="record-tree-malformed"),
        pytest.param([], "describes another commit", id="not-a-commit-record"),
    ],
)
def test_contents_404_at_an_unconfirmed_first_parent_is_not_absence(
    project: Path, tmp_path: Path, lookup: Any, message: str
) -> None:
    snapshot, transport = collected(project, _ghost_parent(project, lookup))
    assert_omitted(project, tmp_path, snapshot, message)
    assert f"{BASE}/git/commits/{GHOST}" in transport.requests


def test_cli_reports_nonexistent_first_parent_with_exit_3(project: Path, tmp_path: Path) -> None:
    code, err, snapshot = cli_collect(project, tmp_path, _ghost_parent(project))
    assert code == 3
    assert f"incomplete: svc: approvals.{TASK}: commit {GHOST} {DOES_NOT_EXIST}" in err
    assert entry(snapshot) is None


def test_existing_first_parent_without_the_file_is_confirmed_before_counting_as_absence(project: Path) -> None:
    snapshot, transport = collected(project, approved_world(project))
    assert (entry(snapshot)["approved"], entry(snapshot)["evidence"]) == (True, "MERGED_PULL_REQUEST")
    # The profile is verified first; its 404 at PARENT triggers the lookup.
    assert transport.requests.index(f"{BASE}/git/commits/{PARENT}") > transport.requests.index(
        q(f"{BASE}/contents/{PROFILE}", ref=PARENT)
    )
    # The profile's merge has the same first parent: the commit is confirmed once per collection.
    assert transport.requests.count(f"{BASE}/git/commits/{PARENT}") == 1


def test_first_parent_already_holding_the_content_needs_no_existence_lookup(project: Path) -> None:
    snapshot, transport = collected(project, approved_world(project, parent_text=text_of(project, TASK)))
    assert entry(snapshot)["evidence"] == "CONTENT_PRESENT_BEFORE_MERGE"
    assert snapshot["collection"]["complete"] is True


def test_contents_404_at_an_unconfirmed_integration_head_is_not_absence(project: Path, tmp_path: Path) -> None:
    responses = approved_world(project)
    responses[q(f"{BASE}/contents/{TASK}", ref=HEAD)] = not_found()
    responses[f"{BASE}/git/commits/{HEAD}"] = not_found()
    snapshot, _ = collected(project, responses)
    assert_omitted(project, tmp_path, snapshot, f"commit {HEAD} {DOES_NOT_EXIST}")


def test_file_absent_at_a_confirmed_integration_head_is_still_blocked(project: Path, tmp_path: Path) -> None:
    responses = approved_world(project)
    responses[q(f"{BASE}/contents/{TASK}", ref=HEAD)] = not_found()
    snapshot, transport = collected(project, responses)
    assert (entry(snapshot)["approved"], entry(snapshot)["evidence"]) == (False, "NOT_ON_INTEGRATION_BRANCH")
    assert f"{BASE}/git/commits/{HEAD}" in transport.requests
    assert snapshot["collection"]["complete"] is True
    assert planned(project, snapshot, tmp_path)["state"] == "BLOCKED"


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        pytest.param(f"{BASE}/commits/{MERGE}", not_found(), "HTTP 404", id="merge-record-404"),
        pytest.param(
            f"{BASE}/commits/{MERGE}",
            Response(403, {"message": "Resource not accessible by integration"}),
            "HTTP 403",
            id="merge-record-inaccessible",
        ),
        pytest.param(q(f"{BASE}/compare/{MERGE}...{HEAD}"), not_found(), "HTTP 404", id="compare-404"),
    ],
)
def test_nonexistent_or_inaccessible_merge_commit_is_omitted(
    project: Path, tmp_path: Path, key: str, value: Any, message: str
) -> None:
    responses = approved_world(project)
    responses[key] = value
    snapshot, _ = collected(project, responses)
    assert_omitted(project, tmp_path, snapshot, message)


def test_contents_404_at_an_unconfirmed_merge_commit_is_omitted(project: Path, tmp_path: Path) -> None:
    responses = approved_world(project)
    responses[q(f"{BASE}/contents/{TASK}", ref=MERGE)] = not_found()
    responses[f"{BASE}/git/commits/{MERGE}"] = not_found()
    snapshot, _ = collected(project, responses)
    assert_omitted(project, tmp_path, snapshot, f"commit {MERGE} {DOES_NOT_EXIST}")


# -- compare responses must describe exactly the requested commits ---------------------------


def _compared(**fields: Any) -> dict[str, Any]:
    return {**comparison(MERGE, HEAD, "ahead"), **fields}


def _truncated() -> dict[str, Any]:
    response = comparison(MERGE, HEAD, "ahead", ahead_by=300)
    response["commits"] = response["commits"][:250]  # the API lists at most 250 without pagination
    return response


CONTRADICTORY = "is contradictory"


@pytest.mark.parametrize(
    ("response", "message"),
    [
        pytest.param(
            {"status": "identical", "base_commit": {"sha": OTHER}},
            "does not identify that comparison",
            id="reported-counterexample",
        ),
        pytest.param(_compared(base_commit={"sha": OTHER}), "describes base", id="base-sha-conflicts"),
        pytest.param(_compared(base_commit=None), "describes base", id="base-commit-missing"),
        pytest.param(_compared(base_commit={"sha": MERGE + "\n"}), "describes base", id="base-sha-malformed"),
        pytest.param(comparison(MERGE, HEAD, "identical"), CONTRADICTORY, id="identical-for-distinct-commits"),
        pytest.param(_compared(commits=[{"sha": OTHER}]), "does not include the integration head", id="head-conflicts"),
        pytest.param(_compared(merge_base_commit={"sha": OTHER}), CONTRADICTORY, id="ahead-merge-base-not-base"),
        pytest.param(_compared(merge_base_commit=None), "malformed", id="merge-base-missing"),
        pytest.param(_compared(behind_by=1), CONTRADICTORY, id="ahead-but-behind"),
        pytest.param(_compared(ahead_by=0, total_commits=0, commits=[]), CONTRADICTORY, id="ahead-by-zero"),
        pytest.param(_compared(total_commits=2), "malformed", id="total-disagrees-with-ahead-by"),
        pytest.param(_compared(ahead_by=-1), "malformed", id="negative-count"),
        pytest.param(_compared(total_commits=True), "malformed", id="boolean-total"),
        pytest.param(_compared(behind_by=False), "malformed", id="boolean-count"),
        pytest.param(_compared(ahead_by="1"), "malformed", id="string-count"),
        pytest.param(_compared(commits=None), "malformed", id="commits-missing"),
        pytest.param(_compared(commits=[HEAD]), "malformed", id="commit-entry-not-a-record"),
        pytest.param(_compared(commits=[{"sha": HEAD + "\n"}]), "malformed", id="commit-entry-sha-malformed"),
        pytest.param(_truncated(), "lists 250 of 300 commits", id="commits-truncated"),
        pytest.param(_compared(status=None), "unexpected compare status", id="status-missing"),
        pytest.param(_compared(status="unrelated"), "unexpected compare status", id="status-unknown"),
        pytest.param(
            {**comparison(MERGE, HEAD, "behind"), "merge_base_commit": {"sha": OTHER}},
            CONTRADICTORY,
            id="behind-merge-base-not-head",
        ),
        pytest.param(
            {**comparison(MERGE, HEAD, "diverged"), "merge_base_commit": {"sha": MERGE}},
            CONTRADICTORY,
            id="diverged-merge-base-is-base",
        ),
        pytest.param(comparison(MERGE, HEAD, "behind"), "not reachable", id="coherent-behind"),
        pytest.param(comparison(MERGE, HEAD, "diverged"), "not reachable", id="coherent-diverged"),
        pytest.param([], "malformed", id="not-an-object"),
    ],
)
def test_compare_response_must_describe_the_requested_commits(
    project: Path, tmp_path: Path, response: Any, message: str
) -> None:
    responses = approved_world(project)
    responses[q(f"{BASE}/compare/{MERGE}...{HEAD}")] = response
    snapshot, _ = collected(project, responses)
    assert_omitted(project, tmp_path, snapshot, message)


def test_cli_reports_contradictory_compare_with_exit_3(project: Path, tmp_path: Path) -> None:
    responses = approved_world(project)
    responses[q(f"{BASE}/compare/{MERGE}...{HEAD}")] = {"status": "identical", "base_commit": {"sha": OTHER}}
    code, err, snapshot = cli_collect(project, tmp_path, responses)
    assert code == 3
    assert f"incomplete: svc: approvals.{TASK}: comparison of {MERGE}...{HEAD}" in err
    assert entry(snapshot) is None


@pytest.mark.parametrize("ahead_by", [1, 3, 250])
def test_coherent_ahead_comparison_is_reachable(project: Path, ahead_by: int) -> None:
    responses = approved_world(project)
    responses[q(f"{BASE}/compare/{MERGE}...{HEAD}")] = comparison(MERGE, HEAD, "ahead", ahead_by=ahead_by)
    snapshot, _ = collected(project, responses)
    assert (entry(snapshot)["approved"], entry(snapshot)["evidence"]) == (True, "MERGED_PULL_REQUEST")
    assert snapshot["collection"]["complete"] is True


def test_coherent_identical_comparison_when_the_merge_is_the_head(project: Path) -> None:
    responses = base_world(head=MERGE)
    responses.update(artifact(PROFILE, PROFILE_TEXT, head=MERGE, commit=PROFILE_COMMIT, number=6, merge=PROFILE_MERGE))
    responses.update(artifact(TASK, text_of(project, TASK), head=MERGE, compare="identical"))
    snapshot, transport = collected(project, responses)
    assert q(f"{BASE}/compare/{MERGE}...{MERGE}") in transport.requests
    assert (entry(snapshot)["approved"], entry(snapshot)["evidence"]) == (True, "MERGED_PULL_REQUEST")


# -- a compare response must identify the requested comparison, not merely list the head ---------

GIT_GIT = json.loads((FIXTURES / "github" / "git-git-compare.json").read_text())["responses"]
GIT_B = "fbd2d1e765da4d2931cadc4c62d3ca5d5b590e7e"
GIT_H = "03b41523c576f26ad34085378717a0c1b287470f"
GIT_L = "1891de46f79dc444a47db1c87bd0c8c76b5463f2"


def _rewrite(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, str):
        for old, new in mapping.items():
            value = value.replace(old, new)
        return value
    if isinstance(value, Response):
        return Response(value.status, _rewrite(value.body, mapping), value.headers)
    if isinstance(value, dict):
        return {_rewrite(k, mapping): _rewrite(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_rewrite(v, mapping) for v in value]
    return value


def _git_git_world(project: Path, head: str, compare: dict[str, Any]) -> dict[str, Any]:
    """The usual world moved to git/git, with PR #7 merged as GIT_B and the given integration head."""
    manifest = project / "factory/project.yaml"
    manifest.write_text(manifest.read_text().replace(REPO, "git/git"))
    # Full SHAs first, then the 7-character abbreviations left in permalink URLs.
    mapping = {
        REPO: "git/git",
        "example-org": "git",
        MERGE: GIT_B,
        HEAD: head,
        MERGE[:7]: GIT_B[:7],
        HEAD[:7]: head[:7],
    }
    responses = _rewrite(approved_world(project), mapping)
    responses[q(f"repos/git/git/compare/{GIT_B}...{head}")] = compare
    return responses


def test_real_comparison_for_another_head_is_not_accepted(project: Path, tmp_path: Path) -> None:
    """git/git's genuine B...L response (ahead, listing H) substituted for the requested B...H."""
    assert GIT_H in [c["sha"] for c in GIT_GIT["BL"]["commits"]]
    snapshot, _ = collected(project, _git_git_world(project, GIT_H, GIT_GIT["BL"]))
    assert_omitted(project, tmp_path, snapshot, f"comparison of {GIT_B}...{GIT_H} does not identify that comparison")


def test_cli_reports_real_comparison_for_another_head_with_exit_3(project: Path, tmp_path: Path) -> None:
    code, err, snapshot = cli_collect(project, tmp_path, _git_git_world(project, GIT_H, GIT_GIT["BL"]))
    assert code == 3
    assert f"comparison of {GIT_B}...{GIT_H} does not identify that comparison" in err
    assert entry(snapshot) is None


def test_real_comparison_for_the_requested_head_is_judged_on_its_merits(project: Path, tmp_path: Path) -> None:
    """The genuine B...H response is diverged: identified, coherent, and not reachable."""
    snapshot, _ = collected(project, _git_git_world(project, GIT_H, GIT_GIT["BH"]))
    assert_omitted(project, tmp_path, snapshot, "not reachable from the integration head")


def test_real_ahead_comparison_for_the_requested_head_is_reachable(project: Path) -> None:
    snapshot, _ = collected(project, _git_git_world(project, GIT_L, GIT_GIT["BL"]))
    assert (entry(snapshot)["approved"], entry(snapshot)["evidence"]) == (True, "MERGED_PULL_REQUEST")
    assert snapshot["collection"]["complete"] is True


API = f"https://api.github.com/repos/{REPO}/compare"
WEB = f"https://github.com/{REPO}/compare"
OWNER = REPO.split("/")[0]
NOT_IDENTIFIED = "does not identify that comparison"


def _for_other_head(head_position: int) -> dict[str, Any]:
    """An ahead comparison of MERGE...OTHER that lists HEAD among its commits."""
    response = comparison(MERGE, OTHER, "ahead", ahead_by=3)
    response["commits"] = [{"sha": OTHER}, {"sha": OTHER}, {"sha": OTHER}]
    response["commits"][head_position] = {"sha": HEAD}
    return response


@pytest.mark.parametrize(
    ("response", "message"),
    [
        pytest.param(_for_other_head(0), NOT_IDENTIFIED, id="head-early-in-commits-of-another-comparison"),
        pytest.param(_for_other_head(-1), NOT_IDENTIFIED, id="head-last-in-commits-of-another-comparison"),
        pytest.param(_compared(url=f"{API}/{OTHER}...{HEAD}"), NOT_IDENTIFIED, id="url-wrong-base"),
        pytest.param(_compared(url=f"{API}/{MERGE}...{OTHER}"), NOT_IDENTIFIED, id="url-wrong-head"),
        pytest.param(_compared(url=f"{API}/{HEAD}...{MERGE}"), NOT_IDENTIFIED, id="url-reversed"),
        pytest.param(
            _compared(url=f"https://api.github.com/repos/other/repo/compare/{MERGE}...{HEAD}"),
            NOT_IDENTIFIED,
            id="url-wrong-repository",
        ),
        pytest.param(
            _compared(url=f"https://api.github.com/repos/{REPO}-fork/compare/{MERGE}...{HEAD}"),
            NOT_IDENTIFIED,
            id="url-repository-name-extended",
        ),
        pytest.param(
            _compared(url=f"https://evil.example/repos/{REPO}/compare/{MERGE}...{HEAD}"), NOT_IDENTIFIED, id="url-host"
        ),
        pytest.param(
            _compared(url=f"http://api.github.com/repos/{REPO}/compare/{MERGE}...{HEAD}"),
            NOT_IDENTIFIED,
            id="url-scheme",
        ),
        pytest.param(
            _compared(url=f"https://x@api.github.com/repos/{REPO}/compare/{MERGE}...{HEAD}"),
            NOT_IDENTIFIED,
            id="url-userinfo",
        ),
        pytest.param(
            _compared(url=f"https://api.github.com:443/repos/{REPO}/compare/{MERGE}...{HEAD}"),
            NOT_IDENTIFIED,
            id="url-port",
        ),
        pytest.param(
            _compared(url=f"https://api.github.com/repos/{REPO}/x/../compare/{MERGE}...{HEAD}"),
            NOT_IDENTIFIED,
            id="url-dot-segment",
        ),
        pytest.param(_compared(url=f"{API}/{MERGE}...{HEAD}?page=2"), NOT_IDENTIFIED, id="url-query"),
        pytest.param(_compared(url=f"{API}/{MERGE}...{HEAD}#x"), NOT_IDENTIFIED, id="url-fragment"),
        pytest.param(_compared(url=f"{API}/{MERGE}...{HEAD}/"), NOT_IDENTIFIED, id="url-trailing-slash"),
        pytest.param(_compared(url=f"{API}/{MERGE}...{HEAD}\n"), NOT_IDENTIFIED, id="url-trailing-newline"),
        pytest.param(_compared(url=f"{API}/{MERGE}...{HEAD}.diff"), NOT_IDENTIFIED, id="url-suffix"),
        pytest.param(_compared(url=f"{API}/{MERGE}..{HEAD}"), NOT_IDENTIFIED, id="url-two-dot-range"),
        pytest.param(_compared(url=f"{API}/{MERGE}%2E%2E%2E{HEAD}"), NOT_IDENTIFIED, id="url-percent-encoded"),
        pytest.param(_compared(url=f"{API}/{MERGE.upper()}...{HEAD}"), NOT_IDENTIFIED, id="url-uppercase-sha"),
        pytest.param(_compared(url=f"{API}/{MERGE[:7]}...{HEAD[:7]}"), NOT_IDENTIFIED, id="url-abbreviated-shas"),
        pytest.param(_compared(url=None), NOT_IDENTIFIED, id="url-missing"),
        pytest.param(_compared(url=["x"]), NOT_IDENTIFIED, id="url-not-a-string"),
        pytest.param(_compared(html_url=None), NOT_IDENTIFIED, id="html-url-missing"),
        pytest.param(_compared(html_url=f"{WEB}/{MERGE}...{OTHER}"), NOT_IDENTIFIED, id="html-url-contradicts"),
        pytest.param(_compared(diff_url=f"{WEB}/{MERGE}...{OTHER}.diff"), NOT_IDENTIFIED, id="diff-url-contradicts"),
        pytest.param(_compared(patch_url=f"{WEB}/{MERGE}...{HEAD}"), NOT_IDENTIFIED, id="patch-url-contradicts"),
        pytest.param(
            _compared(permalink_url=f"{WEB}/{OWNER}:{MERGE[:7]}...{OWNER}:{OTHER[:7]}"),
            NOT_IDENTIFIED,
            id="permalink-names-another-head",
        ),
        pytest.param(
            _compared(permalink_url=f"{WEB}/{OWNER}:{MERGE[:6]}...{OWNER}:{HEAD[:7]}"),
            NOT_IDENTIFIED,
            id="permalink-sha-too-short",
        ),
        pytest.param(
            _compared(url=None, html_url=None, diff_url=None, patch_url=None),
            NOT_IDENTIFIED,
            id="only-abbreviated-permalink",
        ),
    ],
)
def test_compare_response_must_identify_the_requested_comparison(
    project: Path, tmp_path: Path, response: dict[str, Any], message: str
) -> None:
    responses = approved_world(project)
    responses[q(f"{BASE}/compare/{MERGE}...{HEAD}")] = response
    snapshot, _ = collected(project, responses)
    assert_omitted(project, tmp_path, snapshot, message)


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({}, id="as-returned"),
        pytest.param({"permalink_url": f"{WEB}/{OWNER}:{MERGE}...{OWNER}:{HEAD}"}, id="permalink-full-shas"),
        pytest.param({"permalink_url": None, "diff_url": None, "patch_url": None}, id="optional-urls-absent"),
        pytest.param(
            {
                "url": f"https://api.github.com/repos/{REPO.upper()}/compare/{MERGE}...{HEAD}",
                "html_url": f"https://github.com/{REPO.upper()}/compare/{MERGE}...{HEAD}",
            },
            id="repository-name-in-other-case",
        ),
    ],
)
def test_identified_ahead_comparison_variants_are_accepted(project: Path, fields: dict[str, Any]) -> None:
    responses = approved_world(project)
    responses[q(f"{BASE}/compare/{MERGE}...{HEAD}")] = {
        **comparison(MERGE, HEAD, "ahead"),
        **{k: v for k, v in fields.items() if v is not None},
    }
    for key in [k for k, v in fields.items() if v is None]:
        del responses[q(f"{BASE}/compare/{MERGE}...{HEAD}")][key]
    snapshot, _ = collected(project, responses)
    assert (entry(snapshot)["approved"], entry(snapshot)["evidence"]) == (True, "MERGED_PULL_REQUEST")


@pytest.mark.parametrize("status", ["behind", "diverged"])
def test_identified_comparison_that_does_not_reach_the_head_is_omitted(
    project: Path, tmp_path: Path, status: str
) -> None:
    snapshot, _ = collected(project, approved_world(project, compare=status))
    assert_omitted(project, tmp_path, snapshot, "not reachable from the integration head")


# -- base branches must be valid git branch names ---------------------------------------------

MALFORMED_BRANCHES = [
    " ", "\n", "dev\n", "\ndev", "dev\t", "de v", "dev\x00", "dev\x1f", "dev\x7f",
    "dev..x", "..", "/dev", "dev/", "dev//x", ".dev", "dev/.x", "dev.", "x/y.", "dev.lock", "dev.lock/x",
    "-dev", "HEAD", "@", "a~1", "a^", "a:b", "a?", "a*", "a[b", "a\\b", "a@{1}",
]  # fmt: skip


@pytest.mark.parametrize("base", MALFORMED_BRANCHES)
def test_malformed_base_branch_is_unknown_not_blocked(project: Path, tmp_path: Path, base: str) -> None:
    snapshot, _ = collected(project, approved_world(project, associated=[pull(7, BRANCH, base=base, merged_sha=MERGE)]))
    assert_omitted(project, tmp_path, snapshot, "no valid number or base branch")


def test_cli_reports_malformed_base_branch_with_exit_3(project: Path, tmp_path: Path) -> None:
    responses = approved_world(project, associated=[pull(7, BRANCH, base="dev\n", merged_sha=MERGE)])
    code, err, snapshot = cli_collect(project, tmp_path, responses)
    assert code == 3
    assert f"incomplete: svc: approvals.{TASK}: an associated pull request has no valid" in err
    assert entry(snapshot) is None


@pytest.mark.parametrize("base", ["main", "release/2026.11", "feat/x_y-z", "user/a.b/c", "hotfix+1", "dev-old", "a@b"])
def test_valid_other_base_branch_is_still_blocked(project: Path, tmp_path: Path, base: str) -> None:
    snapshot, _ = collected(project, approved_world(project, associated=[pull(7, BRANCH, base=base, merged_sha=MERGE)]))
    assert (entry(snapshot)["approved"], entry(snapshot)["evidence"]) == (False, "PR_NOT_INTO_INTEGRATION_BRANCH")
    assert snapshot["collection"]["complete"] is True
    assert planned(project, snapshot, tmp_path)["state"] == "BLOCKED"


@pytest.mark.parametrize("branch", ["dev.", "-dev", "dev/.x", "HEAD"])
def test_configured_integration_branch_must_be_a_valid_branch_name(project: Path, branch: str) -> None:
    """The manifest schema admits these; they are not branch names git could have."""
    manifest = project / "factory/project.yaml"
    manifest.write_text(manifest.read_text().replace("integration: dev,", f"integration: {branch!r},"))
    loaded = load_project(project).project
    assert loaded is not None
    transport = FakeTransport({})
    entries, errors = collect_approvals(loaded, GitHubApi(transport))
    assert entries == []
    assert errors == [("approvals", f"configured integration branch {branch!r} is not a valid branch name")]
    assert transport.requests == []


# -- API failures ---------------------------------------------------------------------------------


def test_server_error_omits_the_affected_artifact(project: Path) -> None:
    responses = approved_world(project)
    responses[page(f"{BASE}/commits/{FEATURE}/pulls")] = Response(502, {"message": "Bad Gateway"})
    snapshot, _ = collected(project, responses)
    assert entry(snapshot) is None
    assert entry(snapshot, PROFILE) is not None
    assert snapshot["collection"]["errors"] == [
        {"fact": f"approvals.{TASK}", "message": "HTTP 502 Bad Gateway", "repository": "svc"}
    ]


def test_rate_limit_stops_approval_collection(project: Path, tmp_path: Path) -> None:
    responses = approved_world(project)
    responses[q(f"{BASE}/contents/{PROFILE}", ref=HEAD)] = Response(429, {"message": "Too Many Requests"})
    snapshot, transport = collected(project, responses)
    assert snapshot["approvals"] == []
    assert snapshot["collection"]["complete"] is False
    assert not any(TASK in r for r in transport.requests)  # nothing requested after the limit
    assert planned(project, snapshot, tmp_path)["state"] == "UNKNOWN"


def test_cli_reports_incomplete_approvals_with_exit_3(project: Path, tmp_path: Path) -> None:
    responses = approved_world(project, user={"login": "example-human"})
    responses[f"{BASE}/pulls/7"] = Response(500, {"message": "Server Error"})
    out, err = io.StringIO(), io.StringIO()
    code = run(
        ["evidence", "collect", str(project), "-o", str(tmp_path / "s.json")],
        out=out,
        err=err,
        transport=FakeTransport(responses),
    )
    assert code == 3
    assert (
        "warning: human authorization unverifiable: the GitHub identity used for collection (example-human)"
        in err.getvalue()
    )
    assert f"incomplete: svc: approvals.{TASK}: HTTP 500 Server Error" in err.getvalue()


# -- scope and safety ---------------------------------------------------------------------------


def test_only_get_requests_to_the_planning_repository_and_user(project: Path) -> None:
    _, transport = collected(project, approved_world(project))
    assert all(r.startswith(f"{BASE}") or r == "user" for r in transport.requests)
    assert not any(r.startswith(f"{BASE}/pulls/") and r.endswith(("/merge", "/reviews")) for r in transport.requests)


def test_tasks_profiles_and_adrs_are_all_verified() -> None:
    loaded = load_project(THREE_REPO).project
    assert loaded is not None
    paths = approval_paths(loaded)
    assert "docs/adr/ADR-0001-contract-source.md" in paths
    assert "factory/profiles/svc.yaml" in paths
    assert "factory/tasks/SVC-IMPL-007.yaml" in paths
    assert not any(p.startswith("specs/") for p in paths)
