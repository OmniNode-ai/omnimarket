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
import logging
import os
import re
import time
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

import yaml
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
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic import ValidationError as _PydanticValidationError

from omnimarket.github_api import GitHubApiError, rest_json, split_repo
from omnimarket.nodes.node_ci_fix_effect.models.model_ci_fix_command import (
    ModelCiFixCommand,
)
from omnimarket.nodes.node_ci_fix_effect.models.model_ci_fix_result import CiFixResult

_log = logging.getLogger(__name__)

_CI_LOG_MAX_CHARS = 20_000
_PATCH_MAX_NET_LINES = 100
_DEP_CHANGE_PATTERNS = ("pyproject.toml", "uv.lock", "requirements", "package.json")
_DIFF_BLOCK_RE = re.compile(
    r"```(?:diff|patch)?\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)
_HUNK_HEADER_RE = re.compile(r"^@@.*?@@", re.MULTILINE)
_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"


class ModelCiFixEndpointConfig(BaseModel):
    """Contract-owned OpenAI-compatible endpoint for a semantic model key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str = Field(..., min_length=1)
    model_id: str = Field(..., min_length=1)
    timeout_seconds: float = Field(default=120.0, gt=0)


class ModelCiFixRoutingConfig(BaseModel):
    """Typed subset of node_ci_fix_effect contract model_routing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    primary: str = Field(..., min_length=1)
    fallback: str | None = None
    fallback_allowed_roles: list[str] = Field(default_factory=list)
    max_tokens: int = Field(default=8192, gt=0)
    transport: str = Field(default="http")
    patch_allowlist_patterns: list[str] = Field(..., min_length=1)
    model_endpoints: dict[str, ModelCiFixEndpointConfig]
    ci_override: dict[str, str] = Field(default_factory=dict)

    @field_validator("patch_allowlist_patterns")
    @classmethod
    def _validate_patch_allowlist_patterns(cls, value: list[str]) -> list[str]:
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(
                    f"invalid patch allowlist regex {pattern!r}: {exc}"
                ) from exc
        return value


def _load_contract_routing_config(
    role: str = "ci_fixer",
) -> ModelCiFixRoutingConfig:
    with _CONTRACT_PATH.open(encoding="utf-8") as f:
        contract = yaml.safe_load(f)
    try:
        raw = contract["model_routing"][role]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"{_CONTRACT_PATH} missing model_routing.{role} config"
        ) from exc
    try:
        return ModelCiFixRoutingConfig.model_validate(raw)
    except _PydanticValidationError as exc:
        raise ValueError(
            f"{_CONTRACT_PATH} model_routing.{role} schema invalid: {exc}"
        ) from exc


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


def _build_llm_system_prompt(allowed_patterns: list[str]) -> str:
    allowed = ", ".join(allowed_patterns)
    return (
        "You are a CI failure analyst. "
        "Given a failing CI log, identify the root cause and propose a minimal fix "
        "as a git-style unified diff. Output only one patch inside a ```diff block. "
        "Every changed file must start with a diff --git a/<path> b/<path> header "
        "followed by --- a/<path> and +++ b/<path>. "
        "Only touch files matching these repo-relative regex patterns: "
        f"{allowed}. "
        "Keep changes minimal — do not refactor."
    )


def _build_llm_request(
    *,
    prompt: str,
    model_name: str,
    max_tokens: int,
    temperature: float,
    timeout_seconds: float,
) -> ModelLlmAdapterRequest:
    request_data: dict[str, object] = {
        "prompt": prompt,
        "model_name": model_name,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if "timeout_seconds" in ModelLlmAdapterRequest.model_fields:
        request_data["timeout_seconds"] = timeout_seconds
    return ModelLlmAdapterRequest.model_validate(request_data)


@lru_cache(maxsize=1)
def _load_patch_allowlist_patterns() -> tuple[re.Pattern[str], ...]:
    routing = _load_contract_routing_config()
    return tuple(re.compile(pattern) for pattern in routing.patch_allowlist_patterns)


def _resolve_llm_provider(
    primary_model: str,
) -> tuple[AdapterLlmProviderOpenai, ModelCiFixEndpointConfig]:
    routing = _load_contract_routing_config()
    endpoint = routing.model_endpoints.get(primary_model)
    if endpoint is None:
        known = ", ".join(sorted(routing.model_endpoints))
        raise ValueError(
            f"model key {primary_model!r} is not declared in "
            f"{_CONTRACT_PATH} model_routing.ci_fixer.model_endpoints "
            f"(known: {known})"
        )
    return (
        AdapterLlmProviderOpenai(
            base_url=endpoint.base_url,
            default_model=endpoint.model_id,
            provider_name="ci-fixer",
            provider_type="local",
            max_timeout_seconds=endpoint.timeout_seconds,
        ),
        endpoint,
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
    files: list[str] = []
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                files.append(_normalize_patch_path(parts[3]))
        elif line.startswith("+++ "):
            files.append(_normalize_patch_path(line[4:].strip()))
    return [path for path in dict.fromkeys(files) if path and path != "/dev/null"]


def _normalize_patch_path(path: str) -> str:
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _patch_within_allowlist(
    patch: str, patterns: tuple[re.Pattern[str], ...] | None = None
) -> bool:
    allowlist = patterns or _load_patch_allowlist_patterns()
    files = _extract_patch_files(patch)
    if not files:
        return False
    return all(any(p.match(f) for p in allowlist) for f in files)


def _patch_disallowed_files(
    patch: str, patterns: tuple[re.Pattern[str], ...] | None = None
) -> list[str]:
    allowlist = patterns or _load_patch_allowlist_patterns()
    files = _extract_patch_files(patch)
    return [f for f in files if not any(p.match(f) for p in allowlist)]


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


async def _fetch_ci_log(
    repo: str, run_id_github: str, failing_job_name: str | None = None
) -> str:
    owner, repo_name = split_repo(repo)

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    def _download_job_log(job_id: int) -> str:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{owner}/{repo_name}/actions/jobs/{job_id}/logs",
            headers={
                "Authorization": f"Bearer {os.environ['GH_PAT']}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="GET",
        )
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=60) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code not in {301, 302, 303, 307, 308}:
                detail = exc.read().decode("utf-8", errors="replace").strip()
                raise GitHubApiError(detail or str(exc)) from exc
            location = exc.headers.get("Location", "").strip()
            if not location:
                raise GitHubApiError(
                    "missing redirect location for GitHub job log"
                ) from exc
            # GitHub returns a signed blob-storage URL; fetch it without GitHub auth headers.
            with urllib.request.urlopen(location, timeout=60) as resp:
                return resp.read().decode("utf-8", errors="replace")

    def _download_log() -> str:
        jobs_payload = rest_json(
            "GET",
            f"/repos/{owner}/{repo_name}/actions/runs/{run_id_github}/jobs",
        )
        jobs = jobs_payload.get("jobs")
        if not isinstance(jobs, list):
            raise ValueError(f"missing jobs payload for {repo} run={run_id_github}")

        failed_jobs = [
            job
            for job in jobs
            if isinstance(job, dict)
            and str(job.get("conclusion", "")).lower() == "failure"
        ]
        if not failed_jobs:
            raise ValueError(f"no failed jobs found for {repo} run={run_id_github}")

        job = failed_jobs[0]
        if failing_job_name:
            normalized_target = failing_job_name.strip().lower()
            for candidate in failed_jobs:
                candidate_name = str(candidate.get("name", "")).strip().lower()
                if candidate_name == normalized_target:
                    job = candidate
                    break
        job_id = job.get("id")
        if not isinstance(job_id, int):
            raise ValueError(
                f"failed job missing integer id for {repo} run={run_id_github}"
            )
        return _download_job_log(job_id)

    try:
        return await asyncio.to_thread(_download_log)
    except (GitHubApiError, ValueError) as exc:
        raise ValueError(
            f"GitHub Actions log fetch failed for {repo} run={run_id_github}: {exc}"
        ) from exc


async def _resolve_pr_worktree(repo: str, pr_number: int) -> str | None:
    owner, repo_name = split_repo(repo)
    try:
        data = await asyncio.to_thread(
            rest_json,
            "GET",
            f"/repos/{owner}/{repo_name}/pulls/{pr_number}",
        )
    except GitHubApiError:
        return None
    head = data.get("head")
    head_ref = str(head.get("ref", "")) if isinstance(head, dict) else ""
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
        patch_files = _extract_patch_files(patch)
        rc_git_check, git_check_out, git_check_err = await _run_subprocess(
            ["git", "apply", "--check", patch_file],
            cwd=worktree_path,
            timeout=30.0,
            label="git apply --check",
        )
        if rc_git_check == 0:
            rc_git_apply, git_apply_out, git_apply_err = await _run_subprocess(
                ["git", "apply", patch_file],
                cwd=worktree_path,
                timeout=30.0,
                label="git apply",
            )
            if rc_git_apply == 0:
                return
            raise ValueError(
                "git apply failed "
                f"(files={patch_files}, rc={rc_git_apply}): "
                f"stdout={git_apply_out[:500]!r} stderr={git_apply_err[:500]!r}"
            )

        dry_run_failures: list[str] = []
        for strip_count in (1, 0):
            rc_dry, dry_out, dry_err = await _run_subprocess(
                ["patch", "-t", "-C", f"-p{strip_count}", "-i", patch_file],
                cwd=worktree_path,
                timeout=30.0,
                label=f"patch -p{strip_count} --check",
            )
            if rc_dry != 0:
                dry_run_failures.append(
                    f"p{strip_count}: rc={rc_dry} "
                    f"stdout={dry_out[:500]!r} stderr={dry_err[:500]!r}"
                )
                continue

            rc_patch, patch_out, patch_err = await _run_subprocess(
                ["patch", "-t", f"-p{strip_count}", "-i", patch_file],
                cwd=worktree_path,
                timeout=30.0,
                label=f"patch -p{strip_count}",
            )
            if rc_patch == 0:
                return
            raise ValueError(
                "patch failed "
                f"(files={patch_files}, strip_count={strip_count}, rc={rc_patch}): "
                f"stdout={patch_out[:500]!r} stderr={patch_err[:500]!r}"
            )

        raise ValueError(
            "patch preflight failed "
            f"(files={patch_files}): "
            f"git_apply_check_rc={rc_git_check} "
            f"git_apply_check_stdout={git_check_out[:500]!r} "
            f"git_apply_check_stderr={git_check_err[:500]!r}; "
            f"patch_checks={'; '.join(dry_run_failures)}; "
            f"patch_preview={patch[:500]!r}"
        )
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
            _log.info(
                "CI fix phase=fetch_ci_log start repo=%s pr=%s run=%s job=%r",
                request.repo,
                request.pr_number,
                request.run_id_github,
                request.failing_job_name,
            )
            phase_t0 = time.monotonic()
            ci_log = await _fetch_ci_log(
                request.repo, request.run_id_github, request.failing_job_name
            )
            _log.info(
                "CI fix phase=fetch_ci_log done repo=%s pr=%s elapsed_ms=%d chars=%d",
                request.repo,
                request.pr_number,
                int((time.monotonic() - phase_t0) * 1000),
                len(ci_log),
            )

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
            provider, endpoint = _resolve_llm_provider(primary_model)
            routing_config = _load_contract_routing_config()
            allowlist_patterns = _load_patch_allowlist_patterns()
            _log.info(
                "CI fix phase=llm_generate start repo=%s pr=%s primary=%s "
                "endpoint=%s model_id=%s max_tokens=%s timeout_seconds=%s "
                "patch_allowlist=%s",
                request.repo,
                request.pr_number,
                primary_model,
                endpoint.base_url,
                endpoint.model_id,
                policy.max_tokens,
                endpoint.timeout_seconds,
                routing_config.patch_allowlist_patterns,
            )
            user_prompt = (
                f"Failing job: {request.failing_job_name}\n"
                f"Repository: {request.repo}\n\n"
                f"CI LOG:\n{ci_log[:_CI_LOG_MAX_CHARS]}"
            )
            llm_request = _build_llm_request(
                prompt=(
                    f"{_build_llm_system_prompt(routing_config.patch_allowlist_patterns)}"
                    f"\n\n{user_prompt}"
                ),
                model_name=endpoint.model_id,
                max_tokens=policy.max_tokens,
                temperature=policy.temperature,
                timeout_seconds=endpoint.timeout_seconds,
            )
            phase_t0 = time.monotonic()
            response = await provider.generate_async(llm_request)
            llm_text = response.generated_text
            _log.info(
                "CI fix phase=llm_generate done repo=%s pr=%s elapsed_ms=%d chars=%d",
                request.repo,
                request.pr_number,
                int((time.monotonic() - phase_t0) * 1000),
                len(llm_text),
            )

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
            patch_files = _extract_patch_files(patch)
            _log.info(
                "CI fix phase=patch_validate repo=%s pr=%s files=%s net_lines=%d",
                request.repo,
                request.pr_number,
                patch_files,
                net_lines,
            )
            if net_lines > _PATCH_MAX_NET_LINES:
                raise ValueError(
                    f"Patch too large: {net_lines} net changed lines > {_PATCH_MAX_NET_LINES} limit."
                )

            if not _patch_within_allowlist(patch, allowlist_patterns):
                disallowed = _patch_disallowed_files(patch, allowlist_patterns)
                error_msg = (
                    "Patch references files outside contract allowlist "
                    f"(files={disallowed or patch_files}). Skipping apply."
                )
                _log.warning(
                    "CI fix allowlist violation for %s#%s files=%s",
                    request.repo,
                    request.pr_number,
                    disallowed or patch_files,
                )
                is_noop = True
            else:
                _log.info(
                    "CI fix phase=resolve_pr_worktree start repo=%s pr=%s",
                    request.repo,
                    request.pr_number,
                )
                phase_t0 = time.monotonic()
                worktree_path = await _resolve_pr_worktree(
                    request.repo, request.pr_number
                )
                _log.info(
                    "CI fix phase=resolve_pr_worktree done repo=%s pr=%s "
                    "elapsed_ms=%d path=%s",
                    request.repo,
                    request.pr_number,
                    int((time.monotonic() - phase_t0) * 1000),
                    worktree_path,
                )
                if worktree_path is None:
                    _log.warning(
                        "No worktree found for %s#%s — cannot apply patch",
                        request.repo,
                        request.pr_number,
                    )
                    is_noop = True
                else:
                    _log.info(
                        "CI fix phase=apply_patch start repo=%s pr=%s worktree=%s",
                        request.repo,
                        request.pr_number,
                        worktree_path,
                    )
                    phase_t0 = time.monotonic()
                    await _apply_patch(patch, worktree_path)
                    _log.info(
                        "CI fix phase=apply_patch done repo=%s pr=%s elapsed_ms=%d",
                        request.repo,
                        request.pr_number,
                        int((time.monotonic() - phase_t0) * 1000),
                    )

                    changed_files = await _git_diff_changed_files(worktree_path)
                    unexpected = [
                        f
                        for f in changed_files
                        if not any(p.match(f) for p in allowlist_patterns)
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
                        _log.info(
                            "CI fix phase=run_tests start repo=%s pr=%s worktree=%s",
                            request.repo,
                            request.pr_number,
                            worktree_path,
                        )
                        phase_t0 = time.monotonic()
                        tests_ok = await _run_tests(worktree_path)
                        _log.info(
                            "CI fix phase=run_tests done repo=%s pr=%s "
                            "elapsed_ms=%d ok=%s",
                            request.repo,
                            request.pr_number,
                            int((time.monotonic() - phase_t0) * 1000),
                            tests_ok,
                        )
                        if not tests_ok:
                            await _git_checkout_restore(worktree_path)
                            raise ValueError(
                                "pytest gate failed after patch application. Reverted changes."
                            )

                        commit_msg = (
                            f"fix(ci): auto-fix {request.failing_job_name} "
                            f"[{request.repo}#{request.pr_number}] [OMN-8994]"
                        )
                        _log.info(
                            "CI fix phase=git_commit start repo=%s pr=%s",
                            request.repo,
                            request.pr_number,
                        )
                        phase_t0 = time.monotonic()
                        committed = await _git_commit(worktree_path, commit_msg)
                        _log.info(
                            "CI fix phase=git_commit done repo=%s pr=%s "
                            "elapsed_ms=%d committed=%s",
                            request.repo,
                            request.pr_number,
                            int((time.monotonic() - phase_t0) * 1000),
                            committed,
                        )
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
