# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GitHub App check-run posting boundary."""

from pathlib import Path
from typing import Protocol

from omnimarket.github_api import rest_json, split_repo
from omnimarket.github_app_auth import resolve_app_installation_token_from_contract


class ProtocolCheckRunPoster(Protocol):
    def post(
        self,
        *,
        repo: str,
        head_sha: str,
        name: str,
        conclusion: str,
        title: str,
        summary: str,
    ) -> None: ...


class GitHubCheckRunPoster:
    def __init__(self, contract_path: Path) -> None:
        self._contract_path = contract_path

    def post(
        self,
        *,
        repo: str,
        head_sha: str,
        name: str,
        conclusion: str,
        title: str,
        summary: str,
    ) -> None:
        owner, repository = split_repo(repo)
        token = resolve_app_installation_token_from_contract(
            self._contract_path, org=owner, repositories=[repository]
        )
        rest_json(
            "POST",
            f"/repos/{owner}/{repository}/check-runs",
            token=token,
            body={
                "name": name,
                "head_sha": head_sha,
                "status": "completed",
                "conclusion": conclusion,
                "output": {"title": title, "summary": summary},
            },
        )
