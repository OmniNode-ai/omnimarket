# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_ci_fix_effect [OMN-8994].

EFFECT node. Receives ModelCiFixCommand, fetches failing CI log, routes to LLM
(deepseek-r1-14b primary), parses unified diff, applies patch, runs test gate.
Model routing: primary=deepseek-r1-14b, fallback=qwen3-coder-30b per contract.yaml.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
import os
import re
import time
import urllib.error
import urllib.parse
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
_SOURCE_CONTEXT_MAX_FILES = 6
_SOURCE_CONTEXT_MAX_CHARS_PER_FILE = 6_000
_PATCH_MAX_NET_LINES = 100
_DEP_CHANGE_PATTERNS = ("pyproject.toml", "uv.lock", "requirements", "package.json")
_PATH_CANDIDATE_RE = re.compile(
    r"(?:^|[\s\"'`(])((?:src|tests|\.github/workflows)/[A-Za-z0-9_./:-]+)",
    re.MULTILINE,
)
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


def _strip_path_location_suffix(path: str) -> str:
    return re.sub(r":\d+(?::\d+)?$", "", path)


def _extract_candidate_context_paths(
    ci_log: str,
    patterns: tuple[re.Pattern[str], ...] | None = None,
    *,
    max_paths: int = _SOURCE_CONTEXT_MAX_FILES,
) -> list[str]:
    """Extract bounded, allowlisted repo-relative paths mentioned by a CI log."""
    allowlist = patterns or _load_patch_allowlist_patterns()
    candidates: list[str] = []
    for match in _PATH_CANDIDATE_RE.finditer(ci_log):
        path = match.group(1).strip().strip("),.;]'\"`")
        path = _normalize_patch_path(_strip_path_location_suffix(path))
        if not path or path.endswith("/"):
            continue
        if not any(pattern.match(path) for pattern in allowlist):
            continue
        if path not in candidates:
            candidates.append(path)
        if len(candidates) >= max_paths:
            break
    return candidates


def _validate_git_style_unified_diff(patch: str) -> list[str]:
    """Return validation errors for the strict patch shape accepted by the effect."""
    errors: list[str] = []
    lines = patch.splitlines()
    non_empty_indices = [idx for idx, line in enumerate(lines) if line.strip()]
    if not non_empty_indices:
        return ["patch is empty"]

    diff_indices = [
        idx for idx, line in enumerate(lines) if line.startswith("diff --git ")
    ]
    if not diff_indices:
        errors.append(
            "patch must contain at least one 'diff --git a/<path> b/<path>' header"
        )
        return errors
    if diff_indices[0] != non_empty_indices[0]:
        errors.append("patch must start with a diff --git header")

    block_starts = [*diff_indices, len(lines)]
    for block_number, start in enumerate(diff_indices, start=1):
        end = block_starts[block_number]
        block = lines[start:end]
        header_parts = block[0].split()
        if len(header_parts) < 4:
            errors.append(f"diff block {block_number} has malformed diff --git header")
            continue
        old_path = _normalize_patch_path(header_parts[2])
        new_path = _normalize_patch_path(header_parts[3])
        minus_indices = [
            idx for idx, line in enumerate(block) if line.startswith("--- ")
        ]
        plus_indices = [
            idx for idx, line in enumerate(block) if line.startswith("+++ ")
        ]
        hunk_indices = [
            idx for idx, line in enumerate(block) if _HUNK_HEADER_RE.match(line)
        ]
        if not minus_indices:
            errors.append(f"diff block {block_number} missing '--- a/<path>' line")
        if not plus_indices:
            errors.append(f"diff block {block_number} missing '+++ b/<path>' line")
        if not hunk_indices:
            errors.append(f"diff block {block_number} missing '@@' hunk header")
        if minus_indices and plus_indices and hunk_indices:
            if not (minus_indices[0] < plus_indices[0] < hunk_indices[0]):
                errors.append(
                    f"diff block {block_number} must order ---, +++, then @@ hunk headers"
                )
            declared_old = _normalize_patch_path(block[minus_indices[0]][4:].strip())
            declared_new = _normalize_patch_path(block[plus_indices[0]][4:].strip())
            if declared_old not in {old_path, "/dev/null"}:
                errors.append(
                    f"diff block {block_number} --- path {declared_old!r} "
                    f"does not match header path {old_path!r}"
                )
            if declared_new not in {new_path, "/dev/null"}:
                errors.append(
                    f"diff block {block_number} +++ path {declared_new!r} "
                    f"does not match header path {new_path!r}"
                )
    return errors


def _extract_patch_from_llm_response(llm_text: str) -> str:
    m = _DIFF_BLOCK_RE.search(llm_text)
    if not m:
        raise ValueError(
            "LLM response did not contain a valid ```diff block. "
            f"Response preview: {llm_text[:300]}"
        )
    patch = m.group(1).strip()
    errors = _validate_git_style_unified_diff(patch)
    if errors:
        raise ValueError(
            "LLM response patch failed strict unified diff validation: "
            + "; ".join(errors)
        )
    return patch


async def _read_context_file_from_github(
    repo: str, pr_number: int, path: str
) -> str | None:
    owner, repo_name = split_repo(repo)
    try:
        pr_data = await asyncio.to_thread(
            rest_json,
            "GET",
            f"/repos/{owner}/{repo_name}/pulls/{pr_number}",
        )
        head = pr_data.get("head")
        sha = str(head.get("sha", "")) if isinstance(head, dict) else ""
        if not sha:
            return None
        encoded_path = urllib.parse.quote(path, safe="/")
        data = await asyncio.to_thread(
            rest_json,
            "GET",
            f"/repos/{owner}/{repo_name}/contents/{encoded_path}?ref={sha}",
        )
    except GitHubApiError:
        return None
    if not isinstance(data, dict) or data.get("encoding") != "base64":
        return None
    content = str(data.get("content", ""))
    try:
        raw = base64.b64decode(content, validate=False)
    except (binascii.Error, ValueError):
        return None
    return raw.decode("utf-8", errors="replace")


async def _build_source_context(
    *,
    repo: str,
    pr_number: int,
    ci_log: str,
    worktree_path: str | None,
    allowlist_patterns: tuple[re.Pattern[str], ...],
) -> str:
    paths = _extract_candidate_context_paths(ci_log, allowlist_patterns)
    if not paths:
        return (
            "SOURCE CONTEXT:\n"
            "No allowlisted repo-relative source paths were detected in the CI log. "
            "Do not guess file paths; return a no-op explanation if the fix target is unclear."
        )

    sections: list[str] = ["SOURCE CONTEXT:"]
    for path in paths:
        content: str | None = None
        source = "github"
        if worktree_path:
            candidate = (Path(worktree_path) / path).resolve()
            worktree_root = Path(worktree_path).resolve()
            try:
                candidate.relative_to(worktree_root)
            except ValueError:
                candidate = worktree_root / "__outside_worktree__"
            if candidate.is_file():
                source = "worktree"
                try:
                    content = candidate.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    content = None
        if content is None:
            content = await _read_context_file_from_github(repo, pr_number, path)
        if content is None:
            sections.append(f"\n--- {path} (unavailable) ---\n<file not found>")
            continue
        truncated = content[:_SOURCE_CONTEXT_MAX_CHARS_PER_FILE]
        suffix = "\n<file truncated>" if len(content) > len(truncated) else ""
        sections.append(
            f"\n--- {path} ({source}, current contents) ---\n{truncated}{suffix}"
        )
    return "\n".join(sections)


async def _generate_validated_patch(
    *,
    provider: AdapterLlmProviderOpenai,
    endpoint: ModelCiFixEndpointConfig,
    policy: ModelRoutingPolicy,
    routing_config: ModelCiFixRoutingConfig,
    user_prompt: str,
    source_context: str,
    max_attempts: int = 2,
) -> str:
    validation_failure: str | None = None
    for attempt in range(1, max_attempts + 1):
        retry_context = ""
        if validation_failure:
            retry_context = (
                "\n\nPrevious patch validation failed. Return a corrected git-style "
                f"unified diff only. Validation failure: {validation_failure}"
            )
        prompt = (
            f"{_build_llm_system_prompt(routing_config.patch_allowlist_patterns)}"
            f"\n\n{source_context}"
            f"\n\n{user_prompt}"
            f"{retry_context}"
        )
        llm_request = _build_llm_request(
            prompt=prompt,
            model_name=endpoint.model_id,
            max_tokens=policy.max_tokens,
            temperature=policy.temperature,
            timeout_seconds=endpoint.timeout_seconds,
        )
        response = await provider.generate_async(llm_request)
        llm_text = response.generated_text
        try:
            return _extract_patch_from_llm_response(llm_text)
        except ValueError as exc:
            validation_failure = str(exc)
            _log.warning(
                "CI fix LLM patch validation failed attempt=%d/%d: %s",
                attempt,
                max_attempts,
                validation_failure,
            )
            if attempt == max_attempts:
                raise
    raise ValueError("unreachable patch generation failure")


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
                "CI fix phase=resolve_pr_worktree start repo=%s pr=%s",
                request.repo,
                request.pr_number,
            )
            phase_t0 = time.monotonic()
            worktree_path = await _resolve_pr_worktree(request.repo, request.pr_number)
            _log.info(
                "CI fix phase=resolve_pr_worktree done repo=%s pr=%s "
                "elapsed_ms=%d path=%s",
                request.repo,
                request.pr_number,
                int((time.monotonic() - phase_t0) * 1000),
                worktree_path,
            )

            _log.info(
                "CI fix phase=source_context start repo=%s pr=%s",
                request.repo,
                request.pr_number,
            )
            phase_t0 = time.monotonic()
            source_context = await _build_source_context(
                repo=request.repo,
                pr_number=request.pr_number,
                ci_log=ci_log,
                worktree_path=worktree_path,
                allowlist_patterns=allowlist_patterns,
            )
            _log.info(
                "CI fix phase=source_context done repo=%s pr=%s elapsed_ms=%d chars=%d",
                request.repo,
                request.pr_number,
                int((time.monotonic() - phase_t0) * 1000),
                len(source_context),
            )

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
            phase_t0 = time.monotonic()
            patch = await _generate_validated_patch(
                provider=provider,
                endpoint=endpoint,
                policy=policy,
                routing_config=routing_config,
                user_prompt=user_prompt,
                source_context=source_context,
            )
            _log.info(
                "CI fix phase=llm_generate done repo=%s pr=%s elapsed_ms=%d chars=%d",
                request.repo,
                request.pr_number,
                int((time.monotonic() - phase_t0) * 1000),
                len(patch),
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
