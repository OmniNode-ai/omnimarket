# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for node_ci_fix_effect [OMN-8994].

Covers: CiFixResult model, Wave 2 handler behaviour (successful fix, LLM failure,
invalid patch rejection, routing_policy resolution, dep-change guard, file allowlist).
"""

from __future__ import annotations

import re
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix import (
    HandlerCiFixEffect,
    ModelCiFixEndpointConfig,
    _apply_patch,
    _build_llm_request,
    _build_source_context,
    _count_net_changed_lines,
    _extract_candidate_context_paths,
    _extract_patch_files,
    _extract_patch_from_llm_response,
    _patch_within_allowlist,
    _resolve_routing_policy,
    _validate_git_style_unified_diff,
)
from omnimarket.nodes.node_ci_fix_effect.models.model_ci_fix_command import (
    ModelCiFixCommand,
)
from omnimarket.nodes.node_ci_fix_effect.models.model_ci_fix_result import CiFixResult

_CORR_ID = uuid.UUID("00000000-0000-4000-a000-000000000001")
_RUN_ID = "run-test-001"
_ROUTING_POLICY: dict[str, Any] = {
    "primary": "deepseek-r1-14b",
    "fallback": "qwen3-coder-30b",
    "fallback_allowed_roles": ["ci_fixer"],
    "max_tokens": 8192,
    "temperature": 0.2,
    "ci_override": {"primary": "deepseek-r1-14b"},
}

_VALID_PATCH = """\
diff --git a/src/omnimarket/foo.py b/src/omnimarket/foo.py
--- a/src/omnimarket/foo.py
+++ b/src/omnimarket/foo.py
@@ -1,3 +1,3 @@
 def bar():
-    return None
+    return 42
"""


def _cmd(**overrides: Any) -> ModelCiFixCommand:
    defaults: dict[str, Any] = {
        "pr_number": 333,
        "repo": "OmniNode-ai/omnimarket",
        "run_id_github": "12345678",
        "failing_job_name": "test (3.12)",
        "correlation_id": _CORR_ID,
        "run_id": _RUN_ID,
        "routing_policy": _ROUTING_POLICY,
    }
    return ModelCiFixCommand(**{**defaults, **overrides})


# ---------------------------------------------------------------------------
# CiFixResult model tests
# ---------------------------------------------------------------------------


class TestCiFixResult:
    def _make(self, **overrides: Any) -> CiFixResult:
        defaults: dict[str, Any] = {
            "pr_number": 42,
            "repo": "OmniNode-ai/omnimarket",
            "run_id_github": "99887766",
            "failing_job_name": "test (3.12)",
            "correlation_id": _CORR_ID,
            "patch_applied": True,
            "local_tests_passed": True,
            "is_noop": False,
        }
        return CiFixResult(**{**defaults, **overrides})

    @pytest.mark.unit
    def test_full_success_fields(self) -> None:
        r = self._make()
        assert r.patch_applied is True
        assert r.local_tests_passed is True
        assert r.is_noop is False
        assert r.error is None
        assert r.elapsed_seconds == 0.0

    @pytest.mark.unit
    def test_noop_result(self) -> None:
        r = self._make(patch_applied=False, local_tests_passed=False, is_noop=True)
        assert r.is_noop is True
        assert r.patch_applied is False
        assert r.local_tests_passed is False

    @pytest.mark.unit
    def test_error_field(self) -> None:
        r = self._make(
            patch_applied=False,
            local_tests_passed=False,
            is_noop=False,
            error="timeout",
        )
        assert r.error == "timeout"

    @pytest.mark.unit
    def test_frozen(self) -> None:
        r = self._make()
        with pytest.raises(ValidationError):
            r.patch_applied = False  # type: ignore[misc]

    @pytest.mark.unit
    def test_extra_field_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            CiFixResult(
                pr_number=1,
                repo="r/r",
                run_id_github="gh-run",
                failing_job_name="job",
                correlation_id=_CORR_ID,
                patch_applied=False,
                local_tests_passed=False,
                is_noop=True,
                unexpected_field="bad",
            )

    @pytest.mark.unit
    def test_serialization_round_trip(self) -> None:
        r = self._make()
        restored = CiFixResult.model_validate(r.model_dump())
        assert restored == r


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------


class TestPureHelpers:
    @pytest.mark.unit
    def test_count_net_changed_lines(self) -> None:
        assert _count_net_changed_lines(_VALID_PATCH) == 2

    @pytest.mark.unit
    def test_extract_patch_files_src(self) -> None:
        files = _extract_patch_files(_VALID_PATCH)
        assert files == ["src/omnimarket/foo.py"]

    @pytest.mark.unit
    def test_extract_patch_files_diff_git(self) -> None:
        patch = """\
diff --git a/.github/workflows/ci_tests_gate.yml b/.github/workflows/ci_tests_gate.yml
--- a/.github/workflows/ci_tests_gate.yml
+++ b/.github/workflows/ci_tests_gate.yml
@@ -1,1 +1,1 @@
-old
+new
"""
        assert _extract_patch_files(patch) == [".github/workflows/ci_tests_gate.yml"]

    @pytest.mark.unit
    def test_extract_patch_files_plain_paths(self) -> None:
        patch = """\
--- .github/workflows/ci_tests_gate.yml
+++ .github/workflows/ci_tests_gate.yml
@@ -1,1 +1,1 @@
-old
+new
"""
        assert _extract_patch_files(patch) == [".github/workflows/ci_tests_gate.yml"]

    @pytest.mark.unit
    def test_patch_within_allowlist_src(self) -> None:
        assert _patch_within_allowlist(_VALID_PATCH) is True

    @pytest.mark.unit
    def test_patch_within_allowlist_tests(self) -> None:
        tests_patch = _VALID_PATCH.replace("src/omnimarket/foo.py", "tests/test_foo.py")
        assert _patch_within_allowlist(tests_patch) is True

    @pytest.mark.unit
    def test_patch_within_allowlist_github_workflow(self) -> None:
        workflow_patch = _VALID_PATCH.replace(
            "src/omnimarket/foo.py", ".github/workflows/ci_tests_gate.yml"
        )
        assert _patch_within_allowlist(workflow_patch) is True

    @pytest.mark.unit
    def test_patch_outside_allowlist(self) -> None:
        bad_patch = _VALID_PATCH.replace("src/omnimarket/foo.py", "pyproject.toml")
        assert _patch_within_allowlist(bad_patch) is False

    @pytest.mark.unit
    def test_patch_no_files(self) -> None:
        assert _patch_within_allowlist("no diff files here") is False

    @pytest.mark.unit
    def test_extract_candidate_context_paths_from_ci_log(self) -> None:
        paths = _extract_candidate_context_paths(
            "FAILED tests/test_foo.py:12 and src/omnimarket/foo.py:9 "
            "plus pyproject.toml",
            (re.compile(r"^src/"), re.compile(r"^tests/")),
        )
        assert paths == ["tests/test_foo.py", "src/omnimarket/foo.py"]

    @pytest.mark.unit
    def test_strict_diff_validation_requires_diff_git_header(self) -> None:
        errors = _validate_git_style_unified_diff(
            "--- a/src/foo.py\n+++ b/src/foo.py\n@@ -1 +1 @@\n-old\n+new"
        )
        assert any("diff --git" in err for err in errors)

    @pytest.mark.unit
    def test_extract_patch_from_llm_response_validates_strict_shape(self) -> None:
        text = f"```diff\n{_VALID_PATCH}\n```"
        assert _extract_patch_from_llm_response(text) == _VALID_PATCH.strip()


class TestSourceContext:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_build_source_context_reads_worktree_file(
        self, tmp_path: Any
    ) -> None:
        source = tmp_path / "src" / "omnimarket" / "foo.py"
        source.parent.mkdir(parents=True)
        source.write_text("def bar():\n    return 42\n", encoding="utf-8")

        context = await _build_source_context(
            repo="OmniNode-ai/omnimarket",
            pr_number=333,
            ci_log="FAILED src/omnimarket/foo.py:12",
            worktree_path=str(tmp_path),
            allowlist_patterns=(re.compile(r"^src/"),),
        )

        assert "--- src/omnimarket/foo.py (worktree, current contents) ---" in context
        assert "return 42" in context


class TestApplyPatch:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_apply_patch_preflights_before_mutating(self) -> None:
        """Failed git-apply and patch dry-run reports diagnostics without apply."""
        calls: list[list[str]] = []

        async def fake_run(args: list[str], **_kwargs: Any) -> tuple[int, str, str]:
            calls.append(args)
            if args[:3] == ["git", "apply", "--check"]:
                return 1, "", "bad context"
            if args[:4] == ["patch", "-t", "-C", "-p1"]:
                return 1, "checking file Oops", "Hunk FAILED"
            if args[:4] == ["patch", "-t", "-C", "-p0"]:
                return 1, "checking file Oops", "Hunk FAILED"
            raise AssertionError(f"unexpected subprocess call: {args}")

        with (
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._run_subprocess",
                side_effect=fake_run,
            ),
            pytest.raises(ValueError, match="patch preflight failed") as exc,
        ):
            await _apply_patch(_VALID_PATCH, "/tmp/worktree")

        assert calls == [
            ["git", "apply", "--check", calls[0][3]],
            ["patch", "-t", "-C", "-p1", "-i", calls[1][5]],
            ["patch", "-t", "-C", "-p0", "-i", calls[2][5]],
        ]
        assert "bad context" in str(exc.value)
        assert "Hunk FAILED" in str(exc.value)


# ---------------------------------------------------------------------------
# HandlerCiFixEffect Wave 2 integration tests (mocked I/O)
# ---------------------------------------------------------------------------


def _make_llm_response(patch_text: str) -> MagicMock:
    resp = MagicMock()
    resp.generated_text = (
        f"Root cause: missing return value.\n\n```diff\n{patch_text}\n```"
    )
    return resp


def _provider_resolution(
    provider: MagicMock,
    model_id: str = "Corianas/DeepSeek-R1-Distill-Qwen-14B-AWQ",
) -> tuple[MagicMock, ModelCiFixEndpointConfig]:
    return (
        provider,
        ModelCiFixEndpointConfig(
            base_url="http://test-llm:8001",
            model_id=model_id,
            timeout_seconds=120.0,
        ),
    )


@pytest.fixture
def handler() -> HandlerCiFixEffect:
    return HandlerCiFixEffect()


class TestHandlerSuccessfulFix:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_successful_fix_sets_patch_applied(
        self, handler: HandlerCiFixEffect
    ) -> None:
        """Valid log + valid diff + passing tests → patch_applied=True, local_tests_passed=True."""
        llm_resp = _make_llm_response(_VALID_PATCH)

        with (
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._fetch_ci_log",
                new=AsyncMock(return_value="E AssertionError: expected 42 got None\n"),
            ),
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._resolve_llm_provider",
            ) as mock_provider_cls,
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._resolve_pr_worktree",
                new=AsyncMock(return_value="/tmp/fake_worktree"),
            ),
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._apply_patch",
                new=AsyncMock(),
            ),
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._git_diff_changed_files",
                new=AsyncMock(return_value=["src/omnimarket/foo.py"]),
            ),
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._run_tests",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._git_commit",
                new=AsyncMock(return_value=True),
            ),
        ):
            mock_provider = MagicMock()
            mock_provider.generate_async = AsyncMock(return_value=llm_resp)
            mock_provider_cls.return_value = _provider_resolution(mock_provider)

            output = await handler.handle(_cmd())

        assert len(output.events) == 1
        evt = output.events[0]
        assert isinstance(evt, CiFixResult)
        assert evt.patch_applied is True
        assert evt.local_tests_passed is True
        assert evt.is_noop is False
        assert evt.error is None
        assert evt.elapsed_seconds >= 0.0
        llm_request = mock_provider.generate_async.await_args.args[0]
        if "timeout_seconds" in type(llm_request).model_fields:
            assert llm_request.timeout_seconds == 120.0

    @pytest.mark.unit
    def test_build_llm_request_uses_timeout_when_supported(self) -> None:
        llm_request = _build_llm_request(
            prompt="prompt",
            model_name="model",
            max_tokens=128,
            temperature=0.0,
            timeout_seconds=120.0,
        )

        assert llm_request.model_name == "model"
        if "timeout_seconds" in type(llm_request).model_fields:
            assert llm_request.timeout_seconds == 120.0

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_result_carries_correct_metadata(
        self, handler: HandlerCiFixEffect
    ) -> None:
        """Successful fix result carries all command metadata."""
        llm_resp = _make_llm_response(_VALID_PATCH)

        with (
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._fetch_ci_log",
                new=AsyncMock(return_value="E AssertionError\n"),
            ),
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._resolve_llm_provider",
            ) as mock_provider_cls,
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._resolve_pr_worktree",
                new=AsyncMock(return_value="/tmp/fake_worktree"),
            ),
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._apply_patch",
                new=AsyncMock(),
            ),
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._git_diff_changed_files",
                new=AsyncMock(return_value=["src/omnimarket/foo.py"]),
            ),
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._run_tests",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._git_commit",
                new=AsyncMock(return_value=True),
            ),
        ):
            mock_provider = MagicMock()
            mock_provider.generate_async = AsyncMock(return_value=llm_resp)
            mock_provider_cls.return_value = _provider_resolution(mock_provider)

            output = await handler.handle(_cmd())

        evt = output.events[0]
        assert isinstance(evt, CiFixResult)
        assert evt.pr_number == 333
        assert evt.repo == "OmniNode-ai/omnimarket"
        assert evt.run_id_github == "12345678"
        assert evt.failing_job_name == "test (3.12)"
        assert evt.correlation_id == _CORR_ID

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_handler_output_result_is_none(
        self, handler: HandlerCiFixEffect
    ) -> None:
        """Effect handler output.result is None (events carry the payload)."""
        with (
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._fetch_ci_log",
                new=AsyncMock(side_effect=ValueError("gh run view failed")),
            ),
        ):
            output = await handler.handle(_cmd())
        assert output.result is None


class TestHandlerLlmFailure:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_llm_failure_returns_error_result(
        self, handler: HandlerCiFixEffect
    ) -> None:
        """LLM call failure (ValueError) → patch_applied=False, error set, no exception raised."""
        with (
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._fetch_ci_log",
                new=AsyncMock(return_value="E AssertionError\n"),
            ),
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._resolve_llm_provider",
            ) as mock_provider_cls,
        ):
            mock_provider = MagicMock()
            mock_provider.generate_async = AsyncMock(
                side_effect=ValueError("LLM endpoint not configured")
            )
            mock_provider_cls.return_value = _provider_resolution(mock_provider)

            output = await handler.handle(_cmd())

        evt = output.events[0]
        assert isinstance(evt, CiFixResult)
        assert evt.patch_applied is False
        assert evt.local_tests_passed is False

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_llm_no_diff_block_returns_error(
        self, handler: HandlerCiFixEffect
    ) -> None:
        """LLM response with no ```diff block → ValueError, patch_applied=False, error set."""
        llm_resp = MagicMock()
        llm_resp.generated_text = "I cannot determine the fix from the log."

        with (
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._fetch_ci_log",
                new=AsyncMock(return_value="E AssertionError\n"),
            ),
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._resolve_llm_provider",
            ) as mock_provider_cls,
        ):
            mock_provider = MagicMock()
            mock_provider.generate_async = AsyncMock(return_value=llm_resp)
            mock_provider_cls.return_value = _provider_resolution(mock_provider)

            output = await handler.handle(_cmd())

        evt = output.events[0]
        assert isinstance(evt, CiFixResult)
        assert evt.patch_applied is False
        assert "diff block" in (evt.error or "")

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_malformed_patch_retried_with_validation_failure_and_context(
        self, handler: HandlerCiFixEffect
    ) -> None:
        """Malformed first patch triggers a retry prompt with validation diagnostics and file context."""
        bad_resp = MagicMock()
        bad_resp.generated_text = (
            "```diff\n--- a/src/foo.py\n+++ b/src/foo.py\nno hunk\n```"
        )
        good_resp = _make_llm_response(_VALID_PATCH)

        with (
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._fetch_ci_log",
                new=AsyncMock(return_value="FAILED src/omnimarket/foo.py:12\n"),
            ),
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._resolve_pr_worktree",
                new=AsyncMock(return_value="/tmp/fake_worktree"),
            ),
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._build_source_context",
                new=AsyncMock(
                    return_value=(
                        "SOURCE CONTEXT:\n"
                        "--- src/omnimarket/foo.py (worktree, current contents) ---\n"
                        "def bar():\n    return None\n"
                    )
                ),
            ),
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._resolve_llm_provider",
            ) as mock_provider_cls,
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._apply_patch",
                new=AsyncMock(),
            ),
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._git_diff_changed_files",
                new=AsyncMock(return_value=["src/omnimarket/foo.py"]),
            ),
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._run_tests",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._git_commit",
                new=AsyncMock(return_value=True),
            ),
        ):
            mock_provider = MagicMock()
            mock_provider.generate_async = AsyncMock(side_effect=[bad_resp, good_resp])
            mock_provider_cls.return_value = _provider_resolution(mock_provider)

            output = await handler.handle(_cmd())

        evt = output.events[0]
        assert isinstance(evt, CiFixResult)
        assert evt.patch_applied is True
        assert mock_provider.generate_async.await_count == 2
        second_prompt = mock_provider.generate_async.await_args_list[1].args[0].prompt
        assert "SOURCE CONTEXT" in second_prompt
        assert "Previous patch validation failed" in second_prompt
        assert "diff --git" in second_prompt


class TestHandlerInvalidPatchRejection:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_malformed_diff_no_hunk_headers(
        self, handler: HandlerCiFixEffect
    ) -> None:
        """Diff block without @@ hunk headers → rejected, patch_applied=False."""
        malformed = "--- a/src/foo.py\n+++ b/src/foo.py\nno hunk header here"
        llm_resp = _make_llm_response(malformed)

        with (
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._fetch_ci_log",
                new=AsyncMock(return_value="E AssertionError\n"),
            ),
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._resolve_llm_provider",
            ) as mock_provider_cls,
        ):
            mock_provider = MagicMock()
            mock_provider.generate_async = AsyncMock(return_value=llm_resp)
            mock_provider_cls.return_value = _provider_resolution(mock_provider)

            output = await handler.handle(_cmd())

        evt = output.events[0]
        assert isinstance(evt, CiFixResult)
        assert evt.patch_applied is False
        assert "unified diff" in (evt.error or "")

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_patch_too_large_rejected(self, handler: HandlerCiFixEffect) -> None:
        """Patch with > 100 net changed lines → rejected."""
        # Construct a patch with 101 additions
        big_lines = "\n".join(f"+    line_{i} = {i}" for i in range(101))
        big_patch = (
            "diff --git a/src/omnimarket/foo.py b/src/omnimarket/foo.py\n"
            "--- a/src/omnimarket/foo.py\n"
            "+++ b/src/omnimarket/foo.py\n"
            "@@ -1,1 +1,101 @@\n"
            f"{big_lines}\n"
        )
        llm_resp = _make_llm_response(big_patch)

        with (
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._fetch_ci_log",
                new=AsyncMock(return_value="E AssertionError\n"),
            ),
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._resolve_llm_provider",
            ) as mock_provider_cls,
        ):
            mock_provider = MagicMock()
            mock_provider.generate_async = AsyncMock(return_value=llm_resp)
            mock_provider_cls.return_value = _provider_resolution(mock_provider)

            output = await handler.handle(_cmd())

        evt = output.events[0]
        assert isinstance(evt, CiFixResult)
        assert evt.patch_applied is False
        assert "too large" in (evt.error or "")

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_patch_outside_allowlist_is_noop(
        self, handler: HandlerCiFixEffect
    ) -> None:
        """Patch touching files outside src/ or tests/ → is_noop=True, patch not applied."""
        bad_patch = (
            "diff --git a/pyproject.toml b/pyproject.toml\n"
            "--- a/pyproject.toml\n"
            "+++ b/pyproject.toml\n"
            "@@ -1,1 +1,1 @@\n"
            "-name = 'old'\n"
            "+name = 'new'\n"
        )
        llm_resp = _make_llm_response(bad_patch)

        with (
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._fetch_ci_log",
                new=AsyncMock(return_value="E AssertionError\n"),
            ),
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._resolve_llm_provider",
            ) as mock_provider_cls,
        ):
            mock_provider = MagicMock()
            mock_provider.generate_async = AsyncMock(return_value=llm_resp)
            mock_provider_cls.return_value = _provider_resolution(mock_provider)

            output = await handler.handle(_cmd())

        evt = output.events[0]
        assert isinstance(evt, CiFixResult)
        assert evt.patch_applied is False
        assert evt.is_noop is True


class TestHandlerRoutingPolicyResolution:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_invalid_routing_policy_schema_returns_error(
        self, handler: HandlerCiFixEffect
    ) -> None:
        """routing_policy with invalid schema (primary=int) → resolve_routing_policy raises ValueError."""
        # primary must be a str; passing int causes ValidationError which resolve wraps as ValueError
        invalid_cmd = _cmd(routing_policy={"primary": 123})  # invalid schema
        with (
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._fetch_ci_log",
                new=AsyncMock(return_value="E AssertionError\n"),
            ),
        ):
            output = await handler.handle(invalid_cmd)

        evt = output.events[0]
        assert isinstance(evt, CiFixResult)
        assert evt.patch_applied is False

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_valid_routing_policy_resolves_primary_model(
        self, handler: HandlerCiFixEffect
    ) -> None:
        """Valid routing_policy resolves primary model and passes it to LLM provider."""
        captured: list[str] = []

        def _mock_provider(
            primary_model: str,
        ) -> tuple[MagicMock, ModelCiFixEndpointConfig]:
            captured.append(primary_model)
            mock = MagicMock()
            mock.generate_async = AsyncMock(
                side_effect=ValueError("no endpoint configured")
            )
            return _provider_resolution(mock)

        with (
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._fetch_ci_log",
                new=AsyncMock(return_value="E AssertionError\n"),
            ),
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._resolve_llm_provider",
                side_effect=_mock_provider,
            ),
        ):
            await handler.handle(_cmd())

        assert captured == ["deepseek-r1-14b"]

    @pytest.mark.unit
    def test_resolve_routing_policy_direct(self) -> None:
        """_resolve_routing_policy parses routing_policy dict from ModelCiFixCommand."""
        policy = _resolve_routing_policy(_cmd())
        assert policy.primary == "deepseek-r1-14b"
        assert policy.fallback == "qwen3-coder-30b"


class TestHandlerDepChangeGuard:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_log_with_pyproject_toml_rejected(
        self, handler: HandlerCiFixEffect
    ) -> None:
        """CI log mentioning pyproject.toml → dep-change guard fires, patch_applied=False."""
        with (
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._fetch_ci_log",
                new=AsyncMock(
                    return_value="ERROR: pyproject.toml dependency conflict detected\n"
                ),
            ),
        ):
            output = await handler.handle(_cmd())

        evt = output.events[0]
        assert isinstance(evt, CiFixResult)
        assert evt.patch_applied is False
        assert "pyproject.toml" in (evt.error or "")

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_log_with_uv_lock_rejected(self, handler: HandlerCiFixEffect) -> None:
        """CI log mentioning uv.lock → dep-change guard fires."""
        with (
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._fetch_ci_log",
                new=AsyncMock(return_value="ERROR: uv.lock out of sync\n"),
            ),
        ):
            output = await handler.handle(_cmd())

        evt = output.events[0]
        assert isinstance(evt, CiFixResult)
        assert evt.patch_applied is False
        assert "uv.lock" in (evt.error or "")

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_log_too_large_rejected(self, handler: HandlerCiFixEffect) -> None:
        """CI log > 20K chars → size guard fires before LLM call."""
        big_log = "x" * 20_001
        with (
            patch(
                "omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix._fetch_ci_log",
                new=AsyncMock(return_value=big_log),
            ),
        ):
            output = await handler.handle(_cmd())

        evt = output.events[0]
        assert isinstance(evt, CiFixResult)
        assert evt.patch_applied is False
        assert "too large" in (evt.error or "")
