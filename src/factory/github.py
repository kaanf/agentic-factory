"""Read-only GitHub REST access through the authenticated GitHub CLI.

Only GET requests to ``repos/...`` paths and ``user`` (the identity the CLI is using) on the
CLI's default host are possible: there is no code path that writes to GitHub. Callers receive parsed JSON or a ``GitHubError``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote, urlencode

API_VERSION = "2022-11-28"
PAGE_SIZE = 100
_PATH = re.compile(r"^(?:user|repos/[A-Za-z0-9-]+/[A-Za-z0-9._-]+(/[^?#\s]*)?(\?[^#\s]*)?)$")
_DOT_SEGMENT = re.compile(r"(^|/)(\.|%2e){1,2}(/|\?|$)", re.IGNORECASE)


class GitHubError(Exception):
    """A request failed. ``status`` is the HTTP status, or None if no response was received."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class RateLimitError(GitHubError):
    """GitHub refused the request because a primary or secondary rate limit was hit."""


@dataclass(frozen=True)
class Response:
    status: int
    body: Any
    headers: dict[str, str] = field(default_factory=dict)


class Transport(Protocol):
    def get(self, path: str) -> Response: ...


def segment(value: str) -> str:
    """Quote a value for use as one URL path segment."""
    return quote(value, safe="")


def path_of(value: str) -> str:
    """Quote a slash-separated value (branch or file path) keeping its slashes."""
    return "/".join(segment(part) for part in value.split("/"))


def _parse_include(output: str) -> Response:
    """Parse ``gh api --include`` output: status line, headers, blank line, body."""
    text = output.replace("\r\n", "\n")
    head, _, body = text.partition("\n\n")
    lines = head.split("\n")
    match = re.match(r"^HTTP/\S+\s+(\d{3})", lines[0])
    if match is None:
        raise GitHubError("unrecognised response from gh")
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    try:
        parsed = json.loads(body) if body.strip() else None
    except json.JSONDecodeError as exc:
        raise GitHubError(f"response body is not JSON: {exc.msg}", int(match.group(1))) from exc
    return Response(int(match.group(1)), parsed, headers)


class GhCliTransport:
    """Runs ``gh api --method GET``. Authentication is whatever the GitHub CLI already holds."""

    def __init__(self, executable: str | None = None, timeout: float = 60.0) -> None:
        found = executable or shutil.which("gh")
        if found is None:
            raise GitHubError("the GitHub CLI (gh) is not installed or not on PATH")
        self.executable = found
        self.timeout = timeout

    def get(self, path: str) -> Response:
        if not _PATH.match(path) or _DOT_SEGMENT.search(path.split("?", 1)[0]):
            raise GitHubError(f"refusing request outside repos/ and user: {path}")
        command = [
            self.executable,
            "api",
            "--method",
            "GET",
            "--include",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            f"X-GitHub-Api-Version: {API_VERSION}",
            path,
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise GitHubError(f"gh api timed out after {self.timeout:.0f}s") from exc
        if not result.stdout.startswith("HTTP/"):
            raise GitHubError(f"gh api failed: {result.stderr.strip() or 'no output'}")
        return _parse_include(result.stdout)


def _is_rate_limited(response: Response) -> bool:
    if response.status == 429:
        return True
    if response.status == 403:
        if response.headers.get("x-ratelimit-remaining") == "0":
            return True
        message = response.body.get("message", "") if isinstance(response.body, dict) else ""
        return "rate limit" in message.lower()
    return False


class GitHubApi:
    """JSON GET helpers with explicit error and pagination handling."""

    def __init__(self, transport: Transport, max_pages: int = 10) -> None:
        self.transport = transport
        self.max_pages = max_pages
        self.rate_limited = False

    def get(self, path: str, /, **params: str | int) -> Any:
        if self.rate_limited:
            raise RateLimitError("not requested: GitHub rate limit was reached earlier in this collection")
        url = f"{path}?{urlencode(sorted(params.items()))}" if params else path
        response = self.transport.get(url)
        if _is_rate_limited(response):
            self.rate_limited = True
            raise RateLimitError(f"GitHub rate limit reached (HTTP {response.status})", response.status)
        if not 200 <= response.status < 300:
            message = response.body.get("message", "") if isinstance(response.body, dict) else ""
            raise GitHubError(f"HTTP {response.status} {message}".strip(), response.status)
        return response.body

    def get_list(self, path: str, /, key: str | None = None, **params: str | int) -> list[Any]:
        """Fetch every page of a list endpoint. Raises if completeness cannot be confirmed.

        ``key`` names the array inside an object response (e.g. ``check_runs``); such responses
        carry ``total_count``, which must match the number of items collected.
        """
        items: list[Any] = []
        total: int | None = None
        for page in range(1, self.max_pages + 1):
            body = self.get(path, per_page=PAGE_SIZE, page=page, **params)
            if key is None:
                if not isinstance(body, list):
                    raise GitHubError(f"expected a list from {path}")
                batch = body
            else:
                if not isinstance(body, dict) or not isinstance(body.get(key), list):
                    raise GitHubError(f"expected '{key}' in response from {path}")
                batch = body[key]
                total = body.get("total_count", total)
            items.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
        else:
            raise GitHubError(f"more than {self.max_pages * PAGE_SIZE} items at {path}; result would be incomplete")
        if total is not None and total != len(items):
            raise GitHubError(f"{path} reported {total} items but returned {len(items)}")
        return items
