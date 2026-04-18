# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_ci_fix_effect [OMN-8994].

EFFECT node. Receives ModelCiFixCommand, fetches failing CI log, routes to LLM
(deepseek-r1-14b primary), parses unified diff, applies patch, runs test gate.
Model routing: primary=deepseek-r1-14b, fallback=qwen3-coder-30b per contract.yaml.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from uuid import uuid4

from omnibase_compat.routing.model_routing_policy import ModelRoutingPolicy
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_infra.adapters.llm.adapter_llm_provider_openai import (
    AdapterLlmProviderOpenai,
)
from omnibase_infra.adapters.llm.model_llm_adapter_request import ModelLlmAdapterRequest
from omnibase_infra.errors import (
    InfraConnectionError,
    InfraTimeoutError,
    InfraUnavailableError,
)
from pydantic import ValidationError as _PydanticValidationError

from omnimarket.nodes.node_ci_fix_effect.models.model_ci_fix_command import (
    ModelCiFixCommand,
)
from omnimarket.nodes.node_ci_fix_effect.models.model_ci_fix_result import CiFixResult

_log = logging.getLogger(__name__)

_CI_LOG_MAX_CHARS = 20_000
_PATCH_MAX_NET_LINES = 100
_FILE_ALLOWLIST_PATTERNS = (re.compile(r"^src/"), re.compile(r"^tests/"))
_DEP_CHANGE_PATTERNS = ("pyproject.toml", "uv.lock", "requirements", "package.json")
_DIFF_BLOCK_RE = re.compile(
    r"```(?:diff|patch)?\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)
_HUNK_HEADER_RE = re.compile(r"^@@.*?@@", re.MULTILINE)


def _resolve_routing_policy(request: ModelCiFixCommand) -> ModelRoutingPolicy:
    """Deserialize and validate routing_policy from ModelCiFixCommand. Fail-loud."""
    if not request.routing_policy:
        raise ValueError(
            f"routing_policy is empty on command for {request.repo}#{request.pr_number}. "
            "Triage orchestrator must always set routing_policy."
        )
    try:
        return ModelRoutingPolicy.model_validate(request.routing_policy)
    except _PydanticValidationError as exc:
        raise ValueError(
            f"routing_policy schema invalid for {request.repo}#{request.pr_number}: {exc}"
        ) from exc


_LLM_SYSTEM_PROMPT = (
    "You are a CI failure analyst. "
    "Given a failing CI log, identify the root cause and propose a minimal fix "
    "as a unified diff. Output the patch inside a ```diff block. "
    "Only touch files under src/ or tests/. "
    "Keep changes minimal — do not refactor."
)


def _resolve_llm_provider(primary_model: str) -> AdapterLlmProviderOpenai:
    base_url = os.environ.get("LLM_CODER_FAST_URL", "")
    if not base_url:
        raise ValueError("LLM endpoint not configured. Set LLM_CODER_FAST_URL env var.")
    return AdapterLlmProviderOpenai(
        base_url=base_url,
        default_model=primary_model,
        provider_name="ci-fixer",
        provider_type="local",
        max_timeout_seconds=120.0,
    )


def _count_net_changed_lines(patch: str) -> int:
    added = sum(
        1
        for ln in patch.splitlines()
        if ln.startswith("+") and not ln.startswith("+++")
    )
    removed = sum(
        1
        for ln in patch.splitlines()
        if ln.startswith("-") and not ln.startswith("---")
    )
    return added + removed


def _extract_patch_files(patch: str) -> list[str]:
    return [
        line[6:].strip() for line in patch.splitlines() if line.startswith("+++ b/")
    ]


def _patch_within_allowlist(patch: str) -> bool:
    files = _extract_patch_files(patch)
    if not files:
        return False
    return all(any(p.match(f) for p in _FILE_ALLOWLIST_PATTERNS) for f in files)


async def _run_subprocess(
    args: list[str],
    *,
    cwd: str | None = None,
    timeout: float = 60.0,
    label: str = "",
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise ValueError(
            f"Subprocess timed out after {timeout}s: {label or args[0]}"
        ) from exc
    rc = proc.returncode if proc.returncode is not None else -1
    return rc, stdout_b.decode(errors="replace"), stderr_b.decode(errors="replace")


async def _fetch_ci_log(repo: str, run_id_github: str) -> str:
    rc, stdout, stderr = await _run_subprocess(
        ["gh", "run", "view", run_id_github, "--repo", repo, "--log-failed"],
        timeout=60.0,
        label="gh run view --log-failed",
    )
    if rc != 0:
        raise ValueError(
            f"gh run view failed for {repo} run={run_id_github} (rc={rc}): {stderr[:500]}"
        )
    return stdout


async def _resolve_pr_worktree(repo: str, pr_number: int) -> str | None:
    rc, stdout, _stderr = await _run_subprocess(
        ["gh", "pr", "view", str(pr_number), "--repo", repo, "--json", "headRefName"],
        timeout=30.0,
        label="gh pr view headRefName",
    )
    if rc != 0:
        return None
    try:
        data = json.loads(stdout)
        head_ref: str = data.get("headRefName", "")
    except (json.JSONDecodeError, AttributeError):
        return None
    if not head_ref:
        return None

    rc2, wt_out, _ = await _run_subprocess(
        ["git", "worktree", "list", "--porcelain"],
        timeout=15.0,
        label="git worktree list",
    )
    if rc2 != 0:
        return None
    for block in wt_out.split("\n\n"):
        lines = block.strip().splitlines()
        path = ""
        branch = ""
        for line in lines:
            if line.startswith("worktree "):
                path = line[9:].strip()
            elif line.startswith("branch "):
                branch = line[7:].strip()
        if branch.endswith(f"/{head_ref}") or branch == head_ref:
            return path
    return None


async def _apply_patch(patch: str, worktree_path: str) -> None:
    import os as _os
    import tempfile

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".patch", delete=False, encoding="utf-8"
    ) as f:
        f.write(patch)
        patch_file = f.name

    try:
        rc, _out, stderr = await _run_subprocess(
            ["patch", "-p1", "--input", patch_file],
            cwd=worktree_path,
            timeout=30.0,
            label="patch -p1",
        )
        if rc != 0:
            raise ValueError(f"patch -p1 failed (rc={rc}): {stderr[:500]}")
    finally:
        with contextlib.suppress(OSError):
            _os.unlink(patch_file)


async def _git_diff_changed_files(worktree_path: str) -> list[str]:
    rc, stdout, _stderr = await _run_subprocess(
        ["git", "diff", "--name-only"],
        cwd=worktree_path,
        timeout=15.0,
        label="git diff --name-only",
    )
    if rc != 0:
        return []
    return [ln.strip() for ln in stdout.splitlines() if ln.strip()]


async def _git_checkout_restore(worktree_path: str) -> None:
    await _run_subprocess(
        ["git", "checkout", "--", "."],
        cwd=worktree_path,
        timeout=15.0,
        label="git checkout -- .",
    )


async def _git_commit(worktree_path: str, message: str) -> bool:
    rc_add, _, _ = await _run_subprocess(
        ["git", "add", "-u"],
        cwd=worktree_path,
        timeout=15.0,
        label="git add -u",
    )
    if rc_add != 0:
        return False
    rc_commit, _, _ = await _run_subprocess(
        ["git", "commit", "-m", message],
        cwd=worktree_path,
        timeout=15.0,
        label="git commit",
    )
    return rc_commit == 0


async def _run_tests(worktree_path: str) -> bool:
    rc, _out, _err = await _run_subprocess(
        ["uv", "run", "pytest", "tests/", "-x", "--tb=short"],
        cwd=worktree_path,
        timeout=300.0,
        label="uv run pytest",
    )
    return rc == 0


class HandlerCiFixEffect:
    """EFFECT: diagnose failing CI job via LLM, apply patch, run test gate."""

    async def handle(self, request: ModelCiFixCommand) -> ModelHandlerOutput:  # type: ignore[type-arg]
        """Attempt CI fix. Returns CiFixResult with patch_applied/local_tests_passed."""
        t0 = time.monotonic()
        _log.info(
            "CI fix attempt: %s#%s job=%r run=%s",
            request.repo,
            request.pr_number,
            request.failing_job_name,
            request.run_id_github,
        )

        error_msg: str | None = None
        patch_applied = False
        local_tests_passed = False
        is_noop = False

        try:
            policy = _resolve_routing_policy(request)
            ci_log = await _fetch_ci_log(request.repo, request.run_id_github)

            if len(ci_log) > _CI_LOG_MAX_CHARS:
                raise ValueError(
                    f"CI log too large ({len(ci_log)} chars > {_CI_LOG_MAX_CHARS}). "
                    "Manual triage required."
                )

            lower_log = ci_log.lower()
            for dep_pat in _DEP_CHANGE_PATTERNS:
                if dep_pat in lower_log:
                    raise ValueError(
                        f"CI log contains dependency-change pattern '{dep_pat}'. "
                        "Dep changes require human review."
                    )

            primary_model = policy.primary
            provider = _resolve_llm_provider(primary_model)
            user_prompt = (
                f"Failing job: {request.failing_job_name}\n"
                f"Repository: {request.repo}\n\n"
                f"CI LOG:\n{ci_log[:_CI_LOG_MAX_CHARS]}"
            )
            llm_request = ModelLlmAdapterRequest(
                prompt=f"{_LLM_SYSTEM_PROMPT}\n\n{user_prompt}",
                model_name=primary_model,
                max_tokens=policy.max_tokens,
                temperature=policy.temperature,
            )
            response = await provider.generate_async(llm_request)
            llm_text = response.generated_text

            m = _DIFF_BLOCK_RE.search(llm_text)
            if not m:
                raise ValueError(
                    "LLM response did not contain a valid ```diff block. "
                    f"Response preview: {llm_text[:300]}"
                )
            patch = m.group(1).strip()
            if not _HUNK_HEADER_RE.search(patch):
                raise ValueError(
                    "Extracted block does not look like a unified diff (no @@ hunk headers)."
                )

            net_lines = _count_net_changed_lines(patch)
            if net_lines > _PATCH_MAX_NET_LINES:
                raise ValueError(
                    f"Patch too large: {net_lines} net changed lines > {_PATCH_MAX_NET_LINES} limit."
                )

            if not _patch_within_allowlist(patch):
                error_msg = "Patch references files outside src/ or tests/ allowlist. Skipping apply."
                _log.warning(
                    "CI fix allowlist violation for %s#%s",
                    request.repo,
                    request.pr_number,
                )
                is_noop = True
            else:
                worktree_path = await _resolve_pr_worktree(
                    request.repo, request.pr_number
                )
                if worktree_path is None:
                    _log.warning(
                        "No worktree found for %s#%s — cannot apply patch",
                        request.repo,
                        request.pr_number,
                    )
                    is_noop = True
                else:
                    await _apply_patch(patch, worktree_path)

                    changed_files = await _git_diff_changed_files(worktree_path)
                    unexpected = [
                        f
                        for f in changed_files
                        if not any(p.match(f) for p in _FILE_ALLOWLIST_PATTERNS)
                    ]
                    if unexpected:
                        await _git_checkout_restore(worktree_path)
                        raise ValueError(
                            f"Patch modified files outside allowlist: {unexpected}. Reverted."
                        )

                    if not changed_files:
                        _log.info(
                            "Patch produced no diff for %s#%s — is_noop=True",
                            request.repo,
                            request.pr_number,
                        )
                        is_noop = True
                    else:
                        tests_ok = await _run_tests(worktree_path)
                        if not tests_ok:
                            await _git_checkout_restore(worktree_path)
                            raise ValueError(
                                "pytest gate failed after patch application. Reverted changes."
                            )

                        commit_msg = (
                            f"fix(ci): auto-fix {request.failing_job_name} "
                            f"[{request.repo}#{request.pr_number}] [OMN-8994]"
                        )
                        committed = await _git_commit(worktree_path, commit_msg)
                        patch_applied = committed
                        local_tests_passed = tests_ok

        except (
            ValueError,
            InfraConnectionError,
            InfraTimeoutError,
            InfraUnavailableError,
        ) as exc:
            _log.warning(
                "CI fix rejected for %s#%s: %s",
                request.repo,
                request.pr_number,
                exc,
            )
            if error_msg is None:
                error_msg = str(exc)
            is_noop = not patch_applied

        elapsed = time.monotonic() - t0
        result = CiFixResult(
            pr_number=request.pr_number,
            repo=request.repo,
            run_id_github=request.run_id_github,
            failing_job_name=request.failing_job_name,
            correlation_id=request.correlation_id,
            patch_applied=patch_applied,
            local_tests_passed=local_tests_passed,
            is_noop=is_noop,
            error=error_msg,
            elapsed_seconds=elapsed,
        )
        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=request.correlation_id,
            handler_id="node_ci_fix_effect",
            events=(result,),
        )
