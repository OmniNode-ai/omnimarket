# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain test for node_ci_fix_effect.

Command -> CI log fetch -> LLM patch -> git apply -> changed-file read ->
test gate -> commit -> CiFixResult, with every subprocess call bounded by the
duration the node's contract declares under ``timeouts``.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from omnimarket.handlers.node_timeouts_reader import load_node_timeouts
from omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix import (
    HandlerCiFixEffect,
    ModelCiFixEndpointConfig,
)
from omnimarket.nodes.node_ci_fix_effect.models.model_ci_fix_command import (
    ModelCiFixCommand,
)
from omnimarket.nodes.node_ci_fix_effect.models.model_ci_fix_result import CiFixResult

_HANDLER = "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix"
_PATCH = """\
diff --git a/src/omnimarket/foo.py b/src/omnimarket/foo.py
--- a/src/omnimarket/foo.py
+++ b/src/omnimarket/foo.py
@@ -1,3 +1,3 @@
 def bar():
-    return None
+    return 42
"""


@pytest.mark.asyncio
@pytest.mark.unit
async def test_golden_chain_bounds_every_subprocess_by_the_contract() -> None:
    seen: dict[str, float] = {}

    async def fake_run(
        args: list[str], *, cwd: str | None = None, timeout: float, label: str = ""
    ) -> tuple[int, str, str]:
        seen[label] = timeout
        if label == "git diff --name-only":
            return 0, "src/omnimarket/foo.py\n", ""
        return 0, "", ""

    response = MagicMock()
    response.generated_text = f"Root cause: missing return.\n\n```diff\n{_PATCH}\n```"
    provider = MagicMock()
    provider.generate_async = AsyncMock(return_value=response)
    endpoint = ModelCiFixEndpointConfig(
        base_url="http://test-llm:8001", model_id="m", timeout_seconds=120.0
    )
    request = ModelCiFixCommand(
        pr_number=333,
        repo="OmniNode-ai/omnimarket",
        run_id_github="12345678",
        failing_job_name="test (3.12)",
        correlation_id=uuid.UUID("00000000-0000-4000-a000-000000000001"),
        run_id="run-golden-001",
        routing_policy={
            "primary": "deepseek-r1-14b",
            "fallback": "qwen3-coder-30b",
            "fallback_allowed_roles": ["ci_fixer"],
            "max_tokens": 8192,
            "temperature": 0.2,
            "ci_override": {"primary": "deepseek-r1-14b"},
        },
    )
    overrides: dict[str, Any] = {
        "_fetch_ci_log": AsyncMock(return_value="E AssertionError: got None\n"),
        "_resolve_llm_provider": MagicMock(return_value=(provider, endpoint)),
        "_resolve_pr_worktree": AsyncMock(return_value="/tmp/fake_worktree"),
        "_run_subprocess": fake_run,
    }
    with (
        patch(
            "omnimarket.inference.secret_store_resolver.resolve_api_key_async",
            return_value=SecretStr("fake-github-token-for-test"),
        ),
        patch.multiple(_HANDLER, **overrides),
    ):
        result = await HandlerCiFixEffect().handle(request)

    assert isinstance(result, CiFixResult)
    assert result.patch_applied is True
    assert result.local_tests_passed is True
    assert result.error is None
    timeouts = load_node_timeouts("node_ci_fix_effect")
    assert seen["git apply --check"] == timeouts.get("git_patch_s")
    assert seen["git diff --name-only"] == timeouts.get("git_inspect_s")
    assert seen["uv run pytest"] == timeouts.get("test_run_s")
