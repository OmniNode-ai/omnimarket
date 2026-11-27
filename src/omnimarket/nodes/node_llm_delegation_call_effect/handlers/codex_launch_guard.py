# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Launch-side checks for Codex attempts: the signed-in login guard and the argv."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from ..models.model_codex_harness_attempt import (
    ModelCodexHarnessPolicy,
    ModelCodexHarnessReceipt,
    ModelCodexHarnessRequest,
)

_API_KEY_VARIABLES = ("OPENAI_API_KEY", "CODEX_API_KEY")


def uses_api_key(environment: Mapping[str, str]) -> bool:
    """True when the host would run Codex on an API key rather than a ChatGPT sign-in.

    Reads only the shape of the login: the ``auth_mode`` marker and whether a key field
    is present in ``auth.json``, and whether a key variable is set. No value is kept.
    """
    if any(environment.get(name) for name in _API_KEY_VARIABLES):
        return True
    home = environment.get("CODEX_HOME") or str(Path.home() / ".codex")
    try:
        auth = json.loads((Path(home) / "auth.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(auth, dict):
        return False
    return auth.get("auth_mode") == "apikey" or bool(auth.get("OPENAI_API_KEY"))


def api_key_refusal(
    request: ModelCodexHarnessRequest, wall_seconds: float
) -> ModelCodexHarnessReceipt:
    return ModelCodexHarnessReceipt(
        wall_seconds=wall_seconds,
        model=request.model or "",
        exit_cause="api_key_login_refused",
    )


def codex_argv(
    policy: ModelCodexHarnessPolicy,
    request: ModelCodexHarnessRequest,
    workdir: Path,
    answer_file: Path,
) -> list[str]:
    """The codex exec command line, with the login method pinned to ChatGPT."""
    return [
        policy.executable,
        "exec",
        "-C",
        str(workdir.resolve()),
        "-c",
        'forced_login_method="chatgpt"',
        "-c",
        f"model_reasoning_effort={json.dumps(policy.reasoning_effort)}",
        "-c",
        f"approval_policy={json.dumps(policy.approval_policy)}",
        "-s",
        policy.sandbox,
        "-c",
        f"sandbox_workspace_write.network_access={str(policy.network_access).lower()}",
        "--skip-git-repo-check",
        "--json",
        *(("-m", request.model) if request.model is not None else ()),
        "-o",
        str(answer_file),
        "-",
    ]
