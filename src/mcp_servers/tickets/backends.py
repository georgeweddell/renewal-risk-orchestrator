"""Where tickets come from. The server's tools stay the same whichever backend is used."""

from __future__ import annotations

import json
import os
import re
from typing import Protocol

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_servers import mockdata

PRIORITIES = ("P1", "P2", "P3")
ACCOUNT_LABEL = "account:"
# GitHub can't backdate an issue, so seeded demo issues carry their original
# report/close dates in a hidden comment. Real issues fall back to GitHub's own timestamps.
DATE_MARKER = re.compile(r"<!--\s*rro:(reported_at|closed_at)=([^\s>]+)\s*-->")


class TicketsBackend(Protocol):
    def list_issues(self, account_id: str) -> list[dict]:
        """All issues (open and closed) for an account, newest first."""

    def get_issue(self, number: int) -> dict | None: ...


class MockTicketsBackend:
    """Reads the GitHub Issues-shaped `tickets` table seeded by `rro seed`."""

    def list_issues(self, account_id: str) -> list[dict]:
        with mockdata.session() as conn:
            rows = conn.execute(
                "SELECT * FROM tickets WHERE account_id=? ORDER BY created_at DESC", (account_id,)
            ).fetchall()
        return [self._issue(r) for r in rows]

    def get_issue(self, number: int) -> dict | None:
        with mockdata.session() as conn:
            row = conn.execute("SELECT * FROM tickets WHERE number=?", (number,)).fetchone()
        return self._issue(row) if row else None

    @staticmethod
    def _issue(row) -> dict:
        return {
            "number": row["number"],
            "account_id": row["account_id"],
            "title": row["title"],
            "body": row["body"],
            "state": row["state"],
            "priority": row["priority"],
            "labels": json.loads(row["labels"]),
            "created_at": row["created_at"],
            "closed_at": row["closed_at"],
            "url": row["url"],
        }


class GitHubTicketsBackend:
    """GitHub Issues in one repo. An issue belongs to an account through an
    `account:<slug>` label, and its priority is a P1/P2/P3 label."""

    def __init__(self, repo: str, token: str, client: httpx.Client | None = None):
        self.repo = repo
        self.client = client or httpx.Client(
            base_url="https://api.github.com",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )

    @classmethod
    def from_env(cls) -> GitHubTicketsBackend:
        repo, token = os.environ.get("GITHUB_TICKETS_REPO"), os.environ.get("GITHUB_TOKEN")
        if not repo or not token:
            raise ToolError("GITHUB_TICKETS_REPO and GITHUB_TOKEN must be set for the tickets server. Add them to .env.")
        return cls(normalise_repo(repo), token)

    def _get(self, path: str, **params) -> dict | list | None:
        try:
            response = self.client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise ToolError(f"Couldn't reach GitHub: {type(exc).__name__}") from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise ToolError(f"GitHub returned {response.status_code}: {response.json().get('message', '')}")
        return response.json()

    def list_issues(self, account_id: str) -> list[dict]:
        issues, page = [], 1
        while True:
            batch = self._get(
                f"/repos/{self.repo}/issues", labels=f"{ACCOUNT_LABEL}{account_id}", state="all", per_page=100, page=page
            )
            if batch is None:
                raise ToolError(f"GitHub repo {self.repo} not found, or the token can't read it.")
            issues += [self._issue(i) for i in batch if "pull_request" not in i]
            if len(batch) < 100:
                break
            page += 1
        return sorted(issues, key=lambda i: i["created_at"], reverse=True)

    def get_issue(self, number: int) -> dict | None:
        raw = self._get(f"/repos/{self.repo}/issues/{number}")
        return self._issue(raw) if raw and "pull_request" not in raw else None

    @staticmethod
    def _issue(raw: dict) -> dict:
        labels = [label["name"] for label in raw.get("labels", [])]
        body = raw.get("body") or ""
        dates = dict(DATE_MARKER.findall(body))
        return {
            "number": raw["number"],
            "account_id": next((l[len(ACCOUNT_LABEL):] for l in labels if l.startswith(ACCOUNT_LABEL)), None),
            "title": raw["title"],
            "body": DATE_MARKER.sub("", body).strip(),
            "state": raw["state"],
            "priority": next((l for l in labels if l in PRIORITIES), "P3"),
            "labels": labels,
            "created_at": dates.get("reported_at") or raw["created_at"],
            "closed_at": (dates.get("closed_at") or raw.get("closed_at")) if raw["state"] == "closed" else None,
            "url": raw["html_url"],
        }


def normalise_repo(value: str) -> str:
    """Accept "owner/repo" or a github.com URL."""
    return re.sub(r"^https?://github\.com/|\.git$|/+$", "", value.strip())


def load_backend() -> TicketsBackend:
    name = os.environ.get("TICKETS_BACKEND", "mock")
    if name == "mock":
        return MockTicketsBackend()
    if name == "github":
        return GitHubTicketsBackend.from_env()
    raise RuntimeError(f"Unknown TICKETS_BACKEND={name!r}; expected 'mock' or 'github'.")
