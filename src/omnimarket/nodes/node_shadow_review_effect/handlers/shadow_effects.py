# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Effects of the shadow-review tick: diff from the canonical clone, the
pre-send secret scan, and the two reviewer arms (OMN-20422).

Nothing here reads the GitHub API, posts a comment or creates a check. The
diff comes from the registry's canonical clone, refreshed for the PR's head
branch with the sanctioned refresh tool. Arm A1 is the merged omniintelligence
Codex adapter; arm A2 is GLM 5.3 through the claude-glm harness, given the
same prompt the adapter builds and parsed by the same reply parser.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Protocol

from omnimarket.nodes.node_shadow_review_effect.models.model_shadow_review import (
    EnumShadowArm,
    EnumShadowArmStatus,
    EnumShadowStratum,
    ModelShadowArmResult,
    ModelShadowReviewCandidate,
)

GITHUB_OWNER = "OmniNode-ai"
GLM_MODEL = "glm-5.3"
HARNESS_LANE = "shadow-review-runner"
HARNESS_TICKET = "OMN-20422"

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "github-token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_\w{40,})"),
    ),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("openai-style-key", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{32,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    (
        "assigned-secret",
        re.compile(
            r"(?i)^\+.*\b(?:password|passwd|secret|api_?key|token)\b\s*[:=]\s*"
            r"['\"][^'\"\s$<{]{16,}['\"]",
            re.MULTILINE,
        ),
    ),
)


def scan_for_secrets(diff: str) -> tuple[str, ...]:
    """Names of the secret patterns found on added lines; values never leave."""
    added = "\n".join(
        line
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    return tuple(name for name, pattern in _SECRET_PATTERNS if pattern.search(added))


class ProtocolShadowDiffSource(Protocol):
    def diff(self, candidate: ModelShadowReviewCandidate) -> tuple[str | None, str]:
        """The PR diff at its observed head, or None and the drop reason."""
        ...


class ProtocolShadowReviewer(Protocol):
    def review(
        self,
        candidate: ModelShadowReviewCandidate,
        diff: str,
        stratum: EnumShadowStratum,
        work_dir: Path,
    ) -> tuple[ModelShadowArmResult, ...]: ...


def _git(
    clone: Path, *args: str, timeout: float = 120.0
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(clone), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


class CanonicalCloneDiffSource:
    """Reads a PR's diff from the canonical clone, refreshing its branches once."""

    def __init__(
        self, omni_home: Path, refresh_script: Path, refresh_timeout_s: int = 300
    ):
        self._omni_home = omni_home
        self._refresh_script = refresh_script
        self._refresh_timeout_s = refresh_timeout_s

    def clone_for(self, repo: str) -> Path | None:
        for root in (self._omni_home, self._omni_home.parent):
            if (root / repo / ".git").exists():
                return root / repo
        return None

    def _has_commit(self, clone: Path, sha: str) -> bool:
        return _git(clone, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0

    def _refresh(self, candidate: ModelShadowReviewCandidate) -> None:
        subprocess.run(
            [
                "python3",
                str(self._refresh_script),
                "refresh",
                f"{GITHUB_OWNER}/{candidate.repo}",
                "--branch",
                candidate.head_ref,
                "--branch",
                candidate.base,
                "--wait",
                "--timeout",
                str(self._refresh_timeout_s),
            ],
            capture_output=True,
            text=True,
            timeout=self._refresh_timeout_s + 60,
            check=False,
        )

    def diff(self, candidate: ModelShadowReviewCandidate) -> tuple[str | None, str]:
        clone = self.clone_for(candidate.repo)
        if clone is None:
            return None, "no-canonical-clone"
        if not self._has_commit(clone, candidate.head_sha):
            self._refresh(candidate)
            if not self._has_commit(clone, candidate.head_sha):
                return None, "head-unavailable"
        base_ref = f"origin/{candidate.base}"
        merge_base = _git(clone, "merge-base", base_ref, candidate.head_sha)
        if merge_base.returncode != 0:
            return None, "base-unavailable"
        out = _git(
            clone,
            "diff",
            "--no-color",
            "--no-ext-diff",
            merge_base.stdout.strip(),
            candidate.head_sha,
        )
        if out.returncode != 0:
            return None, "diff-failed"
        if not out.stdout.strip():
            return None, "empty-diff"
        return out.stdout, "ok"


# Runs inside the omniintelligence project. Modes: codex (A1), prompt (the
# adapter's own prompt text, for A2), parse (the adapter's reply parser, for A2).
OMNIINTELLIGENCE_PROGRAM = r"""
import asyncio, json, sys
from omniintelligence.review_pairing.adapters.adapter_ai_reviewer import (
    to_review_findings, try_parse_review_response)
from omniintelligence.review_pairing.adapters.adapter_codex_reviewer import async_parse_raw
from omniintelligence.review_pairing.prompts.adversarial_reviewer import (
    PROMPT_VERSION, SYSTEM_PROMPT, USER_PROMPT_TEMPLATE_PR)
mode, repo, pr, sha = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
text = sys.stdin.read()
if mode == "prompt":
    prompt = SYSTEM_PROMPT + "\n\n" + USER_PROMPT_TEMPLATE_PR.format(plan_content=text)
    out = {"prompt": prompt, "prompt_version": PROMPT_VERSION}
elif mode == "codex":
    r = asyncio.run(async_parse_raw(text, review_type="pr", repo=repo, pr_id=pr,
        commit_sha=sha, timeout_seconds=float(sys.argv[5])))
    out = {"success": r.success, "error": r.error, "prompt_version": PROMPT_VERSION,
        "findings": [f.model_dump(mode="json") for f in r.findings]}
else:
    parsed = try_parse_review_response(text)
    out = {"success": parsed is not None, "prompt_version": PROMPT_VERSION,
        "error": None if parsed is not None else "unparseable reply",
        "findings": [] if parsed is None else [f.model_dump(mode="json") for f in
            to_review_findings(parsed, sys.argv[5], repo=repo, pr_id=pr, commit_sha=sha)]}
sys.stdout.write(json.dumps(out, default=str))
"""


class LabShadowReviewer:
    """Runs A1 on every in-scope PR and A2 on public-stratum PRs only."""

    def __init__(
        self,
        *,
        omniintelligence_dir: Path,
        venv_dir: Path,
        harness_script: Path,
        codex_timeout_s: float,
        glm_call_budget_s: float,
    ):
        self._oi_dir = omniintelligence_dir
        self._venv_dir = venv_dir
        self._harness = harness_script
        self._codex_timeout_s = codex_timeout_s
        self._glm_budget_s = glm_call_budget_s

    def _oi(self, args: list[str], stdin: str, timeout: float) -> dict[str, object]:
        env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(self._venv_dir)}
        env.pop("PYTHONPATH", None)
        proc = subprocess.run(
            [
                "uv",
                "run",
                "--frozen",
                "--quiet",
                "--project",
                str(self._oi_dir),
                "python",
                "-c",
                OMNIINTELLIGENCE_PROGRAM,
                *args,
            ],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
        if proc.returncode != 0:
            tail = proc.stderr.strip().splitlines()[-1:] or ["no stderr"]
            raise RuntimeError(
                f"omniintelligence exited {proc.returncode}: {tail[0][:300]}"
            )
        loaded = json.loads(proc.stdout)
        if not isinstance(loaded, dict):
            raise RuntimeError("omniintelligence returned a non-object")
        return loaded

    def _codex(self, c: ModelShadowReviewCandidate, diff: str) -> ModelShadowArmResult:
        start = time.monotonic()
        try:
            out = self._oi(
                [
                    "codex",
                    c.repo,
                    str(c.number),
                    c.head_sha,
                    str(self._codex_timeout_s),
                ],
                diff,
                self._codex_timeout_s + 180,
            )
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            return ModelShadowArmResult(
                arm=EnumShadowArm.CODEX,
                status=EnumShadowArmStatus.FAILED,
                wall_s=round(time.monotonic() - start, 1),
                error=str(exc)[:500],
            )
        wall = round(time.monotonic() - start, 1)
        findings = out.get("findings") or []
        error = out.get("error")
        timed_out = isinstance(error, str) and "timed out" in error
        status = (
            EnumShadowArmStatus.OK
            if out.get("success")
            else EnumShadowArmStatus.TIMEOUT
            if timed_out
            else EnumShadowArmStatus.FAILED
        )
        return ModelShadowArmResult(
            arm=EnumShadowArm.CODEX,
            status=status,
            wall_s=wall,
            finding_count=len(findings) if isinstance(findings, list) else 0,
            findings=findings if isinstance(findings, list) else [],
            error=str(error)[:500] if error else None,
            detail={
                "timeout_s": str(self._codex_timeout_s),
                "prompt_version": str(out.get("prompt_version", "")),
            },
        )

    def _glm(
        self, c: ModelShadowReviewCandidate, diff: str, work_dir: Path
    ) -> ModelShadowArmResult:
        start = time.monotonic()
        detail = {"call_budget_s": str(self._glm_budget_s), "model": GLM_MODEL}

        def failed(
            error: str, status: EnumShadowArmStatus = EnumShadowArmStatus.FAILED
        ) -> ModelShadowArmResult:
            return ModelShadowArmResult(
                arm=EnumShadowArm.GLM,
                status=status,
                wall_s=round(time.monotonic() - start, 1),
                error=error[:500],
                detail=detail,
            )

        try:
            built = self._oi(["prompt", c.repo, str(c.number), c.head_sha], diff, 300)
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            return failed(f"prompt build failed: {exc}")
        prompt_file = work_dir / "a2.prompt.md"
        prompt_file.write_text(str(built["prompt"]), encoding="utf-8")
        detail["prompt_version"] = str(built.get("prompt_version", ""))
        out_dir = work_dir / "harness"
        out_dir.mkdir(parents=True, exist_ok=True)
        before = {p.name for p in out_dir.iterdir()}
        try:
            subprocess.run(
                [
                    "python3",
                    str(self._harness),
                    "run",
                    "--harness",
                    "claude-glm",
                    "--model",
                    GLM_MODEL,
                    "--task-class",
                    "code_review",
                    "--prompt-file",
                    str(prompt_file),
                    "--call-budget-s",
                    str(int(self._glm_budget_s)),
                    "--check",
                    'test -s "$HARNESS_ANSWER_FILE"',
                    "--lane",
                    HARNESS_LANE,
                    "--ticket",
                    HARNESS_TICKET,
                    "--out-dir",
                    str(out_dir),
                    "--no-emit",
                ],
                capture_output=True,
                text=True,
                timeout=self._glm_budget_s + 120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return failed(
                "harness exceeded its call budget", EnumShadowArmStatus.TIMEOUT
            )
        runs = [p for p in out_dir.iterdir() if p.name not in before and p.is_dir()]
        if len(runs) != 1 or not (runs[0] / "receipt.json").is_file():
            return failed(f"harness left {len(runs)} new run directories")
        receipt = json.loads((runs[0] / "receipt.json").read_text(encoding="utf-8"))
        detail["harness_run_id"] = str(receipt.get("run_id", runs[0].name))
        detail["harness_outcome"] = str(receipt.get("outcome", ""))
        answer = runs[0] / "answer.md"
        if receipt.get("timed_out"):
            return failed("harness timed out", EnumShadowArmStatus.TIMEOUT)
        if not answer.is_file() or not answer.read_text(encoding="utf-8").strip():
            return failed(f"no answer (harness outcome {receipt.get('outcome')})")
        try:
            out = self._oi(
                ["parse", c.repo, str(c.number), c.head_sha, GLM_MODEL],
                answer.read_text(encoding="utf-8"),
                300,
            )
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            return failed(f"reply parse failed: {exc}")
        findings = out.get("findings") or []
        return ModelShadowArmResult(
            arm=EnumShadowArm.GLM,
            status=EnumShadowArmStatus.OK
            if out.get("success")
            else EnumShadowArmStatus.FAILED,
            wall_s=round(time.monotonic() - start, 1),
            finding_count=len(findings) if isinstance(findings, list) else 0,
            findings=findings if isinstance(findings, list) else [],
            error=str(out["error"])[:500] if out.get("error") else None,
            detail=detail,
        )

    def review(
        self,
        candidate: ModelShadowReviewCandidate,
        diff: str,
        stratum: EnumShadowStratum,
        work_dir: Path,
    ) -> tuple[ModelShadowArmResult, ...]:
        arms = [self._codex(candidate, diff)]
        if stratum is EnumShadowStratum.PUBLIC:
            arms.append(self._glm(candidate, diff, work_dir))
        return tuple(arms)
