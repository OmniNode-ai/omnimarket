# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""App-authenticated, paginated pull request commit read."""

from pathlib import Path
from typing import Protocol

from omnimarket.github_api import rest_json_array, split_repo
from omnimarket.github_app_auth import resolve_app_installation_token_from_contract


class ProtocolPrCommitReader(Protocol):
    def list_commits(self, repo: str, pr_number: int) -> list[tuple[str, str]]: ...


class GitHubPrCommitReader:
    def __init__(self, contract_path: Path, *, per_page: int, max_commits: int) -> None:
        self._contract_path = contract_path
        self._per_page = per_page
        self._max_commits = max_commits

    def list_commits(self, repo: str, pr_number: int) -> list[tuple[str, str]]:
        owner, name = split_repo(repo)
        token = resolve_app_installation_token_from_contract(
            self._contract_path, org=owner, repositories=[name]
        )
        commits: list[tuple[str, str]] = []
        page = 1
        while True:
            rows = rest_json_array(
                "GET",
                f"/repos/{owner}/{name}/pulls/{pr_number}/commits?per_page={self._per_page}&page={page}",
                token=token,
            )
            for row in rows:
                sha, message = row["sha"], row["commit"]["message"]
                if not isinstance(sha, str) or not isinstance(message, str):
                    raise TypeError("commit SHA and message must be strings")
                commits.append((sha, message))
            if len(rows) < self._per_page or len(commits) >= self._max_commits:
                return commits
            page += 1
