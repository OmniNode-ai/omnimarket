"""HandlerJudgeVerifier — calls the judge LLM to verify resolved review threads.

Re-evaluates each resolved thread by sending the original finding, the thread
conversation, and the current diff to the judge model.  Returns a structured
PASS/FAIL verdict per thread and updates ThreadState accordingly.

Design constraints (per 2026-04-09-pr-review-bot-design.md):
- Judge model must NEVER be a build-loop model (LLM_CODER_URL / LLM_CODER_FAST_URL).
  Only LLM_DEEPSEEK_R1_URL (port 8101) or port 8102 fallback are permitted.
- LLM endpoint is read from the env var supplied at construction time — never
  hardcoded.
- Judge timeout is 90 s minimum (R4 — cold-start latency on M2 Ultra).
- Malformed JSON from the judge is treated as FAIL with a clear error message (R7).
- Max 3 re-verification attempts per finding (R6 anti-spam guard).
- Each finding can be verified independently; threads still in POSTED/PENDING
  status are skipped.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from uuid import UUID

import httpx

from omnimarket.nodes.node_pr_review_bot.handlers.handler_fsm import (
    ProtocolJudgeVerifier,
)
from omnimarket.nodes.node_pr_review_bot.models.models import (
    EnumThreadStatus,
    ReviewFinding,
    ThreadState,
)

logger = logging.getLogger(__name__)

# Maximum re-verification attempts per thread (R6)
MAX_VERIFY_ATTEMPTS = 3

# Minimum HTTP timeout in seconds (R4 — DeepSeek-R1 cold-start on M2 Ultra)
JUDGE_TIMEOUT_SECONDS = 90.0

_SYSTEM_PROMPT = """\
You are a senior code review judge. Your task is to determine whether an author's \
response adequately addresses a code review finding.

You will receive:
1. The original review finding (title, description, severity, suggestion).
2. The thread conversation (all replies from the author).
3. The current diff at the relevant file and line range (may be empty if the file \
was not changed in a subsequent push).

Respond ONLY with valid JSON in this exact format:
{"verdict": "PASS" | "FAIL", "reasoning": "<one or two sentences explaining your decision>"}

Criteria for PASS:
- The author's reply explains a concrete fix that directly addresses the finding, AND
- The current diff confirms the fix is present in the code (or the finding was a \
false positive the author has explained convincingly).

Criteria for FAIL:
- The reply is vague, dismissive, or does not address the specific concern, OR
- The diff shows the code is unchanged or the fix is absent, OR
- The author's explanation is technically incorrect.

Do not add commentary outside the JSON object.
"""


def _build_user_prompt(
    finding: ReviewFinding,
    conversation: list[str],
    diff_context: str,
) -> str:
    """Construct the user prompt for the judge model."""
    finding_block = (
        f"## Finding\n"
        f"Title: {finding.title}\n"
        f"Severity: {finding.severity}\n"
        f"Category: {finding.category}\n"
        f"Description: {finding.description}\n"
    )
    if finding.suggestion:
        finding_block += f"Suggested fix: {finding.suggestion}\n"
    if finding.evidence.file_path:
        line_info = ""
        if finding.evidence.line_start is not None:
            end = finding.evidence.line_end or finding.evidence.line_start
            line_info = f", lines {finding.evidence.line_start}-{end}"
        finding_block += f"File: {finding.evidence.file_path}{line_info}\n"

    conversation_block = "## Author Thread Replies\n"
    if conversation:
        for i, reply in enumerate(conversation, start=1):
            conversation_block += f"{i}. {reply}\n"
    else:
        conversation_block += "(No author replies — thread was silently dismissed.)\n"

    diff_block = "## Current Diff at Finding Location\n"
    if diff_context.strip():
        diff_block += f"```diff\n{diff_context}\n```\n"
    else:
        diff_block += "(No diff available for this location.)\n"

    return f"{finding_block}\n{conversation_block}\n{diff_block}"


def _parse_judge_response(raw: str) -> tuple[bool, str]:
    """Parse the judge model JSON response.

    Returns (passed, reasoning).  On parse failure returns (False, error_msg).
    """
    try:
        # Strip markdown code fences the model may add despite instructions
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            # Remove first and last fence lines
            cleaned = "\n".join(
                lines[1:-1] if lines[-1].startswith("```") else lines[1:]
            )
        data = json.loads(cleaned)
        verdict = str(data.get("verdict", "")).upper()
        reasoning = str(data.get("reasoning", "No reasoning provided."))
        if verdict not in {"PASS", "FAIL"}:
            return (
                False,
                f"Judge returned unknown verdict {verdict!r}. Treating as FAIL.",
            )
        return verdict == "PASS", reasoning
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("Judge response parse failure: %s | raw=%r", exc, raw[:200])
        return (
            False,
            f"Judge model returned malformed JSON (parse error: {exc}). Treating as FAIL.",
        )


class HandlerJudgeVerifier(ProtocolJudgeVerifier):
    """Calls the judge LLM to verify each resolved review thread.

    Args:
        judge_base_url_env: Environment variable name holding the judge LLM
            base URL (e.g. ``"LLM_DEEPSEEK_R1_URL"``).  The value must be an
            OpenAI-compatible endpoint.  Never hardcoded.
        judge_model_id: Model identifier string sent in the ``model`` field of
            the chat completions request (e.g. ``"deepseek-r1"``).
        timeout_seconds: HTTP request timeout.  Defaults to 90 s (R4).
        thread_conversations: Optional mapping of ``finding_id -> list[str]``
            with author reply text.  If not supplied the verifier treats each
            resolved thread as having no replies.
        diff_context_map: Optional mapping of ``finding_id -> diff_str`` with
            the current diff at the finding location.  If not supplied the
            verifier sends an empty diff block.
    """

    def __init__(
        self,
        judge_base_url_env: str = "LLM_DEEPSEEK_R1_URL",
        judge_model_id: str = "deepseek-r1",
        timeout_seconds: float = JUDGE_TIMEOUT_SECONDS,
        thread_conversations: dict[UUID, list[str]] | None = None,
        diff_context_map: dict[UUID, str] | None = None,
    ) -> None:
        self._judge_base_url_env = judge_base_url_env
        self._judge_model_id = judge_model_id
        self._timeout_seconds = max(timeout_seconds, JUDGE_TIMEOUT_SECONDS)
        self._thread_conversations: dict[UUID, list[str]] = thread_conversations or {}
        self._diff_context_map: dict[UUID, str] = diff_context_map or {}

    def _get_judge_url(self) -> str:
        url = os.environ.get(self._judge_base_url_env, "")
        if not url:
            msg = (
                f"Judge LLM endpoint not configured. "
                f"Set {self._judge_base_url_env} env var to an OpenAI-compatible base URL."
            )
            raise RuntimeError(msg)
        return url.rstrip("/")

    def _call_judge(self, system_prompt: str, user_prompt: str) -> str:
        """Synchronous HTTP call to the judge model."""
        base_url = self._get_judge_url()
        payload = {
            "model": self._judge_model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": 512,
            "temperature": 0.1,
        }
        with httpx.Client(timeout=self._timeout_seconds) as client:
            resp = client.post(
                f"{base_url}/v1/chat/completions",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            return str(data["choices"][0]["message"]["content"])

    def verify(
        self,
        correlation_id: UUID,
        findings: tuple[ReviewFinding, ...],
        thread_states: tuple[ThreadState, ...],
        judge_model: str,
    ) -> list[ThreadState]:
        """Verify each resolved thread against the judge model.

        Threads in PENDING or POSTED status are skipped (not yet resolved).
        Threads that have already reached a terminal status (VERIFIED_PASS,
        VERIFIED_FAIL, ESCALATED) are skipped.
        Threads at MAX_VERIFY_ATTEMPTS are escalated rather than retried.

        Returns the updated list of ThreadState objects.
        """
        findings_by_id: dict[UUID, ReviewFinding] = {f.id: f for f in findings}
        updated: list[ThreadState] = []

        for thread in thread_states:
            if thread.status not in (EnumThreadStatus.RESOLVED,):
                # Only act on freshly resolved threads
                updated.append(thread)
                continue

            finding = findings_by_id.get(thread.finding_id)
            if finding is None:
                logger.warning(
                    "No finding for thread finding_id=%s — skipping", thread.finding_id
                )
                updated.append(thread)
                continue

            if thread.verify_attempts >= MAX_VERIFY_ATTEMPTS:
                logger.warning(
                    "Finding %s hit max verify attempts (%d) — escalating",
                    thread.finding_id,
                    MAX_VERIFY_ATTEMPTS,
                )
                thread.status = EnumThreadStatus.ESCALATED
                thread.judge_reasoning = (
                    f"Escalated after {MAX_VERIFY_ATTEMPTS} failed verification attempts. "
                    "Human review required."
                )
                updated.append(thread)
                continue

            conversation = self._thread_conversations.get(thread.finding_id, [])
            diff_context = self._diff_context_map.get(thread.finding_id, "")
            user_prompt = _build_user_prompt(finding, conversation, diff_context)

            try:
                raw_response = self._call_judge(_SYSTEM_PROMPT, user_prompt)
                passed, reasoning = _parse_judge_response(raw_response)
            except Exception as exc:
                logger.exception(
                    "Judge call failed for finding %s: %s", thread.finding_id, exc
                )
                passed = False
                reasoning = f"Judge model call failed: {exc}. Treating as FAIL."

            thread.verify_attempts += 1
            thread.verified_at = datetime.now(tz=UTC)
            thread.judge_reasoning = reasoning
            thread.status = (
                EnumThreadStatus.VERIFIED_PASS
                if passed
                else EnumThreadStatus.VERIFIED_FAIL
            )

            logger.info(
                "Judge verdict for finding %s (attempt %d): %s — %s",
                thread.finding_id,
                thread.verify_attempts,
                "PASS" if passed else "FAIL",
                reasoning[:100],
            )
            updated.append(thread)

        return updated


__all__: list[str] = [
    "JUDGE_TIMEOUT_SECONDS",
    "MAX_VERIFY_ATTEMPTS",
    "HandlerJudgeVerifier",
]
