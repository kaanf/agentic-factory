"""Approval provenance for task, profile and ADR files, read from merge records. Read-only.

For each file the collector looks at the content on the planning repository's integration head
and asks GitHub which pull request put exactly that content there and who merged it. It records
facts; the planner judges them (approver list, staleness against the local file).

What this can establish: the content at hash H reached the integration branch through one merged
pull request, merged by GitHub login L. What it cannot establish: that a human made that merge.
While agents can act with the approver's GitHub credentials, a merge by an approver's login proves
nothing about human intent. Every collected approval is therefore marked ``humanAuthorization:
unverifiable``, and the planner never lets such an approval pass.

Evidence that is missing, contradictory or ambiguous omits the file's approval entirely, so the
planner reports it as UNKNOWN. Definite negative findings (direct push, unmerged PR, ...) are
written with ``approved: false``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from datetime import datetime
from typing import Any

from factory.github import GitHubApi, GitHubError, path_of, segment
from factory.model import Project, Repository

UNVERIFIABLE = "unverifiable"
_SHA = re.compile(r"[0-9a-f]{40}")
# Characters and sequences git forbids anywhere in a ref name (git check-ref-format rules 3-5, 8-9).
_REFNAME_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{|//")


class _Unresolved(GitHubError):
    """The records were readable but missing, contradictory or ambiguous."""


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _is_sha(value: Any) -> bool:
    # fullmatch: with match(), "$" also accepts a trailing newline.
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _is_branch(value: Any) -> bool:
    """Whether ``value`` is a name git accepts for a branch (``git check-ref-format --branch``)."""
    return (
        isinstance(value, str)
        and value not in ("", "@", "HEAD")
        and _REFNAME_FORBIDDEN.search(value) is None
        and not value.startswith(("/", "-"))
        and not value.endswith(("/", ".", ".lock"))
        and not any(part.startswith(".") or part.endswith(".lock") for part in value.split("/"))
    )


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _merged_at(pr: dict[str, Any], what: str) -> str | None:
    """The merge time of a pull request record, or None if the record shows it was not merged.

    Only an explicit ``merged_at: null`` on a record with a valid state shows that. An absent,
    malformed or contradictory merge status is not evidence either way.
    """
    if "merged_at" not in pr:
        raise _Unresolved(f"{what} has no merge status")
    merged_at, state, merged = pr["merged_at"], pr.get("state"), pr.get("merged")
    if state not in ("open", "closed"):
        raise _Unresolved(f"{what} has no valid state")
    if merged_at is not None:
        try:
            if not isinstance(merged_at, str) or datetime.fromisoformat(merged_at).tzinfo is None:
                raise ValueError
        except ValueError:
            raise _Unresolved(f"{what} has a malformed merge time {merged_at!r}") from None
    # Only detail records carry ``merged``; where present it must agree with ``merged_at``.
    if (merged is not None and merged is not (merged_at is not None)) or (state == "open" and merged_at is not None):
        raise _Unresolved(f"{what} has a contradictory merge status (state {state!r}, merged {merged!r})")
    return merged_at


def credential_isolation(api: GitHubApi, approvers: tuple[str, ...]) -> dict[str, Any]:
    """Describe why merges cannot be attributed to a human. There is no 'verified' outcome in v1."""
    try:
        login = api.get("user").get("login")
    except (GitHubError, AttributeError):
        login = None
    identity = login if isinstance(login, str) else None
    if identity is not None and identity in approvers:
        return {
            "identity": identity,
            "message": f"the GitHub identity used for collection ({identity}) is an approver: agent and human "
            "credentials are shared, so a merge by an approver's login cannot be attributed to a human",
            "status": "shared",
        }
    return {
        "identity": identity,
        "message": "there is no evidence that agent and human GitHub credentials are separated, so a merge by an "
        "approver's login cannot be attributed to a human",
        "status": "unverified",
    }


class _ApprovalCollector:
    def __init__(self, api: GitHubApi, repo: Repository, project: Project) -> None:
        self.api = api
        self.repo = repo
        self.project = project
        self.errors: list[tuple[str, str]] = []
        self.base = f"repos/{repo.github}"
        self.directory = project.manifest.planning_directory
        self.commits: set[str] = set()  # SHAs whose commits GitHub has shown to exist

    def _error(self, fact: str, message: str) -> None:
        self.errors.append((fact, message))

    def _head(self) -> str:
        ref = self.api.get(f"{self.base}/git/ref/heads/{path_of(self.repo.integration_branch)}")
        if ref["object"]["type"] != "commit":
            raise GitHubError(f"refs/heads/{self.repo.integration_branch} does not point to a commit")
        sha: str = ref["object"]["sha"]
        if not _is_sha(sha):
            raise _Unresolved(f"refs/heads/{self.repo.integration_branch} has a malformed commit SHA {sha!r}")
        return sha

    def _commit_exists(self, sha: str) -> None:
        """Raise unless GitHub shows that commit ``sha`` exists in the repository."""
        if sha in self.commits:
            return
        try:
            record = self.api.get(f"{self.base}/git/commits/{segment(sha)}")
        except GitHubError as exc:
            if exc.status == 404:
                raise _Unresolved(f"commit {sha} does not exist or is not accessible") from exc
            raise
        tree = record.get("tree") if isinstance(record, dict) else None
        if (
            not isinstance(record, dict)
            or record.get("sha") != sha
            or not isinstance(tree, dict)
            or not _is_sha(tree.get("sha"))
        ):
            raise _Unresolved(f"the commit record requested for {sha} describes another commit")
        self.commits.add(sha)

    def _file(self, path: str, ref: str) -> bytes | None:
        """The file's bytes at commit ``ref``, or None if the commit exists and the file is not in it."""
        try:
            body = self.api.get(f"{self.base}/contents/{path_of(path)}", ref=ref)
        except GitHubError as exc:
            if exc.status == 404:
                # The contents API answers 404 for a missing commit too: only an existing one proves absence.
                self._commit_exists(ref)
                return None
            raise
        if not isinstance(body, dict) or body.get("type") != "file" or body.get("encoding") != "base64":
            raise _Unresolved(f"{path} at {ref} is not a regular file readable through the contents API")
        try:
            data = base64.b64decode(body["content"])
        except (binascii.Error, TypeError) as exc:
            raise _Unresolved(f"{path} at {ref}: undecodable content") from exc
        if git_blob_sha(data) != body.get("sha"):
            raise _Unresolved(f"{path} at {ref}: content does not match its blob SHA")
        return data

    def _identity(self, pr: Any, what: str) -> tuple[int, str]:
        """The number and base branch of a pull request record, which must target the planning repository."""
        if not isinstance(pr, dict):
            raise _Unresolved(f"{what} is not a pull request record")
        number, base = pr.get("number"), pr.get("base")
        valid_number = isinstance(number, int) and not isinstance(number, bool) and number > 0
        if not valid_number or not isinstance(base, dict) or not _is_branch(base.get("ref")):
            raise _Unresolved(f"{what} has no valid number or base branch")
        full_name = base["repo"].get("full_name") if isinstance(base.get("repo"), dict) else None
        if not isinstance(full_name, str) or full_name.lower() != self.repo.github.lower():
            raise _Unresolved(f"{what} targets repository {full_name!r}, not {self.repo.github}")
        return number, base["ref"]

    def _names_comparison(self, value: Any, prefix: str, suffix: str) -> bool:
        """Whether ``value`` is exactly ``prefix + <planning repository> + suffix``.

        Built and compared as a whole string, never parsed or followed. GitHub returns the repository
        name in its canonical case, so only that part is compared case-insensitively.
        """
        return (
            isinstance(value, str)
            and len(value) > len(prefix) + len(suffix)
            and value.startswith(prefix)
            and value.endswith(suffix)
            and value[len(prefix) : -len(suffix)].lower() == self.repo.github.lower()
        )

    def _identify_comparison(self, c: dict[str, Any], sha: str, head: str, what: str) -> None:
        """Raise unless the response's own URLs name exactly this repository, base, head and direction.

        Which commits a response lists says nothing about which head it compared against: the head of
        one comparison appears in the commits of many others.
        """
        spec = f"/compare/{sha}...{head}"
        required = {"url": ("https://api.github.com/repos/", spec), "html_url": ("https://github.com/", spec)}
        optional = {
            "diff_url": ("https://github.com/", f"{spec}.diff"),
            "patch_url": ("https://github.com/", f"{spec}.patch"),
        }
        for field, (prefix, suffix) in [*required.items(), *((f, v) for f, v in optional.items() if f in c)]:
            if not self._names_comparison(c.get(field), prefix, suffix):
                raise _Unresolved(f"{what} does not identify that comparison ({field} is {c.get(field)!r})")
        # The permalink abbreviates both SHAs, so it can only contradict, never identify.
        if "permalink_url" in c:
            owner = re.escape(self.repo.github.split("/")[0])
            pattern = (
                rf"https://github\.com/(?i:{re.escape(self.repo.github)})/compare/"
                rf"(?i:{owner}):([0-9a-f]{{7,40}})\.\.\.(?i:{owner}):([0-9a-f]{{7,40}})"
            )
            match = re.fullmatch(pattern, c["permalink_url"]) if isinstance(c["permalink_url"], str) else None
            if match is None or not sha.startswith(match[1]) or not head.startswith(match[2]):
                raise _Unresolved(f"{what} does not identify that comparison (permalink_url is {c['permalink_url']!r})")

    def _reachable(self, sha: str, head: str) -> bool:
        """Whether ``sha`` is ``head`` or an ancestor of it, from a comparison of exactly those two commits."""
        what = f"comparison of {sha}...{head}"
        c = self.api.get(f"{self.base}/compare/{segment(sha)}...{segment(head)}")
        if not isinstance(c, dict):
            raise _Unresolved(f"{what} is malformed")
        self._identify_comparison(c, sha, head, what)
        base_sha = c["base_commit"].get("sha") if isinstance(c.get("base_commit"), dict) else None
        if base_sha != sha:
            raise _Unresolved(f"{what} describes base {base_sha!r}")
        status = c.get("status")
        if status not in ("identical", "ahead", "behind", "diverged"):
            raise _Unresolved(f"unexpected compare status {status!r}")
        ahead, behind, commits = c.get("ahead_by"), c.get("behind_by"), c.get("commits")
        merge_base = c["merge_base_commit"].get("sha") if isinstance(c.get("merge_base_commit"), dict) else None
        listed = [x.get("sha") if isinstance(x, dict) else None for x in commits] if isinstance(commits, list) else None
        if (
            not _count(ahead)
            or not _count(behind)
            or not _count(c.get("total_commits"))
            or c["total_commits"] != ahead
            or not _is_sha(merge_base)
            or listed is None
            or not all(_is_sha(x) for x in listed)
        ):
            raise _Unresolved(f"{what} is malformed")
        # Status, counts and merge base must tell one story (merge base = base exactly when head contains base).
        coherent = {
            "identical": sha == head and ahead == behind == 0 and merge_base == sha,
            "ahead": sha != head and ahead > 0 and behind == 0 and merge_base == sha,
            "behind": sha != head and ahead == 0 and behind > 0 and merge_base == head,
            "diverged": ahead > 0 and behind > 0 and merge_base not in (sha, head),
        }[status]
        if not coherent:
            raise _Unresolved(
                f"{what} is contradictory (status {status!r}, ahead_by {ahead}, behind_by {behind}, "
                f"merge base {merge_base})"
            )
        if ahead:
            # Consistency only: the head was identified from the URLs above. Without pagination GitHub
            # lists at most 250 commits, and a partial list cannot be checked.
            if len(listed) != ahead:
                raise _Unresolved(f"{what} lists {len(listed)} of {ahead} commits, so it cannot be checked")
            if head not in listed:
                raise _Unresolved(f"{what} does not include the integration head")
        elif listed:
            raise _Unresolved(f"{what} is malformed")
        return status in ("ahead", "identical")

    def _verify(self, path: str, head: str) -> dict[str, Any]:
        repo_path = f"{self.directory}/{path}" if self.directory else path
        content = self._file(repo_path, head)
        if content is None:
            return {
                "approved": False,
                "evidence": "NOT_ON_INTEGRATION_BRANCH",
                "humanAuthorization": UNVERIFIABLE,
                "path": path,
                "sha256": self.project.digests[path],
            }
        digest = hashlib.sha256(content).hexdigest()

        latest = self.api.get(f"{self.base}/commits", path=repo_path, sha=head, per_page=1)
        if not isinstance(latest, list) or len(latest) != 1:
            raise _Unresolved(f"no commit on {self.repo.integration_branch} is recorded as changing {repo_path}")
        commit: str = latest[0]["sha"]
        if not _is_sha(commit):
            raise _Unresolved(f"the latest commit recorded for {repo_path} has a malformed SHA {commit!r}")
        entry: dict[str, Any] = {
            "approved": False,
            "commit": commit,
            "humanAuthorization": UNVERIFIABLE,
            "path": path,
            "sha256": digest,
        }

        associated = self.api.get_list(f"{self.base}/commits/{segment(commit)}/pulls")
        branch = self.repo.integration_branch
        into = [p for p in associated if self._identity(p, "an associated pull request")[1] == branch]
        # Every record into the branch must have a definite merge status before any is judged.
        merged = [p for p in into if _merged_at(p, f"pull request #{p['number']}") is not None]
        if not associated:
            return {**entry, "evidence": "NO_PULL_REQUEST"}
        if not into:
            return {**entry, "evidence": "PR_NOT_INTO_INTEGRATION_BRANCH"}
        if not merged:
            return {**entry, "evidence": "PR_NOT_MERGED"}
        if len(merged) > 1:
            numbers = ", ".join(f"#{p['number']}" for p in sorted(merged, key=lambda p: p["number"]))
            raise _Unresolved(f"commit {commit} belongs to several merged pull requests ({numbers})")

        listed = merged[0]
        number: int = listed["number"]
        what = f"pull request #{number}"
        pr = self.api.get(f"{self.base}/pulls/{number}")
        returned, base_ref = self._identity(pr, what)
        if returned != number:
            raise _Unresolved(f"requested {what}, but GitHub returned #{returned}")
        if pr.get("merged") is not True or base_ref != branch or _merged_at(pr, what) is None:
            raise _Unresolved(f"{what} is listed as merged into the integration branch, but is not")
        if pr["merged_at"] != listed["merged_at"]:
            raise _Unresolved(f"{what} reports two different merge times")
        merge_sha = pr.get("merge_commit_sha")
        merged_by = pr["merged_by"].get("login") if isinstance(pr.get("merged_by"), dict) else None
        if not _is_sha(merge_sha) or not isinstance(merged_by, str) or not merged_by:
            raise _Unresolved(f"{what} has no recorded merge commit or merge actor")
        if listed.get("merge_commit_sha") != merge_sha:
            raise _Unresolved(f"{what} reports two different merge commits")
        if not self._reachable(merge_sha, head):
            raise _Unresolved(f"merge commit {merge_sha} of #{number} is not reachable from the integration head")
        at_merge = self._file(repo_path, merge_sha)
        if at_merge is None or hashlib.sha256(at_merge).hexdigest() != digest:
            raise _Unresolved(f"{repo_path} at merge commit {merge_sha} differs from the integration head")

        entry |= {"mergeCommit": merge_sha, "mergedBy": merged_by, "pullRequest": number}
        record = self.api.get(f"{self.base}/commits/{segment(merge_sha)}")
        if not isinstance(record, dict) or record.get("sha") != merge_sha:
            raise _Unresolved(f"the commit record requested for {merge_sha} describes another commit")
        parents = record.get("parents")
        if (
            not isinstance(parents, list)
            or not parents
            or not isinstance(parents[0], dict)
            or not _is_sha(parents[0].get("sha"))
        ):
            # Without the first parent nothing shows that this PR introduced the content.
            raise _Unresolved(f"merge commit {merge_sha} of #{number} has no valid first parent")
        before = self._file(repo_path, parents[0]["sha"])
        if before is not None and hashlib.sha256(before).hexdigest() == digest:
            # The content was already on the integration branch: this PR did not introduce it.
            return {**entry, "evidence": "CONTENT_PRESENT_BEFORE_MERGE"}
        return {**entry, "approved": True, "evidence": "MERGED_PULL_REQUEST"}

    def collect(self, paths: list[str]) -> list[dict[str, Any]]:
        if not _is_branch(self.repo.integration_branch):
            self._error(
                "approvals",
                f"configured integration branch {self.repo.integration_branch!r} is not a valid branch name",
            )
            return []
        try:
            identity = self.api.get(self.base)
            if str(identity.get("full_name", "")).lower() != self.repo.github.lower():
                raise _Unresolved(
                    f"'{self.repo.github}' resolves to '{identity.get('full_name')}' (renamed or transferred)"
                )
            head = self._head()
        except (GitHubError, KeyError, TypeError) as exc:
            self._error("approvals", getattr(exc, "message", f"unexpected response shape: {exc}"))
            return []

        entries = []
        for path in paths:
            try:
                entries.append(self._verify(path, head))
            except GitHubError as exc:
                self._error(f"approvals.{path}", exc.message)
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                self._error(f"approvals.{path}", f"unexpected response shape: {exc}")

        try:
            final = self._head()
        except (GitHubError, KeyError, TypeError) as exc:
            final = None
            self._error("approvals", getattr(exc, "message", f"unexpected response shape: {exc}"))
        if final != head:
            if final is not None:
                self._error("approvals", f"integration head moved from {head} to {final} during collection")
            return []
        return entries


def approval_paths(project: Project) -> list[str]:
    files = [t.file for t in project.tasks.values()]
    files += [p.file for p in project.profiles.values()]
    files += [a.file for a in project.adrs.values()]
    return sorted(set(files))


def collect_approvals(project: Project, api: GitHubApi) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """Approval entries for every task, profile and ADR file, and (fact, message) for each one not collected."""
    repo = project.manifest.repository(project.manifest.planning_repository)
    assert repo is not None  # guaranteed by project validation
    collector = _ApprovalCollector(api, repo, project)
    entries = collector.collect(approval_paths(project))
    return entries, collector.errors
