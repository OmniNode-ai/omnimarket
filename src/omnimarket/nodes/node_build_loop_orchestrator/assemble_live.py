"""assemble_live.py — Live build loop runner.

Wires real sub-handler implementations into the HandlerBuildLoopOrchestrator
and runs the autonomous build loop against the Linear Active Sprint.

Usage:
    cd /Volumes/PRO-G40/Code/omni_home/omnimarket
    source ~/.omnibase/.env
    uv run python -m omnimarket.nodes.node_build_loop_orchestrator.assemble_live --max-cycles 3

Sub-handler implementations:
    - Closeout: pass-through (no-op for now)
    - Verify: pass-through (always passes)
    - RSD Fill: fetches tickets from Linear API (Backlog/Todo status)
    - Classify: uses Qwen3-14B (local fast) for LLM-based classification
    - Dispatch: creates worktrees, calls LLMs for code gen, opens PRs via gh CLI

Related:
    - OMN-5113: Autonomous Build Loop epic
    - OMN-7823: Set up continuous build loop with verification
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx

from omnimarket.nodes.node_build_loop.models.model_loop_start_command import (
    ModelLoopStartCommand,
)
from omnimarket.nodes.node_build_loop_orchestrator.handlers.handler_build_loop_orchestrator import (
    HandlerBuildLoopOrchestrator,
)
from omnimarket.nodes.node_build_loop_orchestrator.models.model_live_runner_config import (
    ModelLlmClassificationResult,
)
from omnimarket.nodes.node_build_loop_orchestrator.protocols.protocol_sub_handlers import (
    BuildTarget,
    ClassifyResult,
    CloseoutResult,
    DispatchResult,
    RsdFillResult,
    ScoredTicket,
    VerifyResult,
)
from omnimarket.nodes.node_closeout_effect.handlers.handler_closeout import (
    HandlerCloseout,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
LINEAR_API_URL = "https://api.linear.app/graphql"
LINEAR_TEAM_ID = "9bdff6a3-f4ef-4ff7-b29a-6c4cf44371e6"

OMNI_HOME = Path("/Volumes/PRO-G40/Code/omni_home")
WORKTREE_ROOT = Path("/Volumes/PRO-G40/Code/omni_worktrees")

# LLM endpoints (OpenAI-compatible)
LLM_FAST_URL = os.environ.get("LLM_CODER_FAST_URL", "http://192.168.86.201:8001")
LLM_CODER_URL = os.environ.get("LLM_CODER_URL", "http://192.168.86.201:8000")

# Frontier: GLM-4.5 (primary code generation backend)
LLM_GLM_API_KEY = os.environ.get("LLM_GLM_API_KEY", "")
LLM_GLM_URL = os.environ.get("LLM_GLM_URL", "")
LLM_GLM_MODEL_NAME = os.environ.get("LLM_GLM_MODEL_NAME", "glm-4.5")

# Frontier: OpenAI
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = "https://api.openai.com/v1"

# Frontier: Google (Gemini via OpenAI-compat endpoint)
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "") or os.environ.get(
    "GEMINI_API_KEY", ""
)

LINEAR_API_KEY = os.environ.get("LINEAR_API_KEY", "")

# Per-token cost estimates (USD per 1K tokens) for cost tracking
_MODEL_COST_PER_1K: dict[str, float] = {
    "glm-4.5": 0.0005,
    "glm-4.7-flash": 0.0001,
    "gpt-4o-mini": 0.00015,
    "qwen3-coder-30b": 0.0,  # local — no cost
}


def _estimate_cost(model: str, total_tokens: int) -> float:
    """Estimate USD cost for an LLM call based on model and token count."""
    per_1k = _MODEL_COST_PER_1K.get(model, 0.001)  # conservative default
    return round(per_1k * total_tokens / 1000, 6)


# Repo mapping: label/keyword -> repo name
REPO_HINTS: dict[str, str] = {
    "omniclaude": "omniclaude",
    "omnibase_core": "omnibase_core",
    "omnibase_infra": "omnibase_infra",
    "omnidash": "omnidash",
    "omnimarket": "omnimarket",
    "omnimemory": "omnimemory",
    "omniintelligence": "omniintelligence",
    "omniweb": "omniweb",
    "omninode_infra": "omninode_infra",
    "omnibase_spi": "omnibase_spi",
    "onex_change_control": "onex_change_control",
}


# ---------------------------------------------------------------------------
# Live sub-handler: Closeout (pass-through)
# ---------------------------------------------------------------------------
class LiveCloseoutHandler:
    """Pass-through closeout — delegates to existing HandlerCloseout with no injections."""

    def __init__(self) -> None:
        self._inner = HandlerCloseout()

    async def handle(
        self, *, correlation_id: UUID, dry_run: bool = False
    ) -> CloseoutResult:
        result = await self._inner.handle(
            correlation_id=correlation_id, dry_run=dry_run
        )
        return CloseoutResult(success=result.merge_sweep_completed)


# ---------------------------------------------------------------------------
# Live sub-handler: Verify (pass-through)
# ---------------------------------------------------------------------------
class LiveVerifyHandler:
    """Always-pass verify handler for the live loop."""

    async def handle(
        self, *, correlation_id: UUID, dry_run: bool = False
    ) -> VerifyResult:
        logger.info("[VERIFY] Pass-through verify (correlation_id=%s)", correlation_id)
        return VerifyResult(all_critical_passed=True)


# ---------------------------------------------------------------------------
# Live sub-handler: RSD Fill (Linear API)
# ---------------------------------------------------------------------------
class LiveRsdFillHandler:
    """Fetches tickets from Linear (Backlog/Todo) and returns them as ScoredTickets."""

    async def handle(
        self,
        *,
        correlation_id: UUID,
        scored_tickets: tuple[ScoredTicket, ...],
        max_tickets: int = 5,
    ) -> RsdFillResult:
        logger.info(
            "[RSD-FILL] Fetching up to %d tickets from Linear (correlation_id=%s)",
            max_tickets,
            correlation_id,
        )

        if not LINEAR_API_KEY:
            logger.error("[RSD-FILL] LINEAR_API_KEY not set")
            return RsdFillResult(selected_tickets=(), total_selected=0)

        fetch_count = max_tickets * 2
        query = (
            "{ issues("
            f'filter: {{ team: {{ id: {{ eq: "{LINEAR_TEAM_ID}" }} }}, '
            'state: { type: { in: ["backlog", "unstarted"] } } }, '
            f"first: {fetch_count}, orderBy: updatedAt"
            ") { nodes { id identifier title description priority "
            "state { name type } labels { nodes { name } } } } }"
        )

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                LINEAR_API_URL,
                json={"query": query},
                headers={
                    "Content-Type": "application/json",
                    "Authorization": LINEAR_API_KEY,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        nodes = data.get("data", {}).get("issues", {}).get("nodes", [])
        logger.info("[RSD-FILL] Fetched %d candidate tickets from Linear", len(nodes))

        tickets: list[ScoredTicket] = []
        for node in nodes[:max_tickets]:
            labels = tuple(
                lbl["name"] for lbl in node.get("labels", {}).get("nodes", [])
            )
            priority = node.get("priority", 0) or 0
            # RSD score: higher priority = higher score (Linear priority: 0=none, 1=urgent, 4=low)
            rsd_score = max(0.0, 5.0 - priority) if priority > 0 else 1.0
            tickets.append(
                ScoredTicket(
                    ticket_id=node["identifier"],
                    title=node["title"],
                    rsd_score=rsd_score,
                    priority=priority,
                    labels=labels,
                    description=node.get("description", "") or "",
                    state=node.get("state", {}).get("name", ""),
                )
            )

        logger.info(
            "[RSD-FILL] Selected %d tickets: %s",
            len(tickets),
            ", ".join(t.ticket_id for t in tickets),
        )

        return RsdFillResult(
            selected_tickets=tuple(tickets),
            total_selected=len(tickets),
        )


# ---------------------------------------------------------------------------
# Live sub-handler: Classify (LLM-backed via Qwen3-14B)
# ---------------------------------------------------------------------------
class LiveTicketClassifyHandler:
    """Classifies tickets using Qwen3-14B (local fast model) via OpenAI-compatible API."""

    def __init__(self) -> None:
        self.classification_results: list[ModelLlmClassificationResult] = []

    async def handle(
        self,
        *,
        correlation_id: UUID,
        tickets: tuple[ScoredTicket, ...],
    ) -> ClassifyResult:
        logger.info(
            "[CLASSIFY] Classifying %d tickets via LLM (correlation_id=%s)",
            len(tickets),
            correlation_id,
        )

        classifications: list[BuildTarget] = []

        for ticket in tickets:
            buildability, source, raw_resp = await self._classify_one(ticket)
            classifications.append(
                BuildTarget(
                    ticket_id=ticket.ticket_id,
                    title=ticket.title,
                    buildability=buildability,
                )
            )
            self.classification_results.append(
                ModelLlmClassificationResult(
                    ticket_id=ticket.ticket_id,
                    buildability=buildability,
                    source=source,
                    model_used="Qwen/Qwen3-14B-AWQ"
                    if source == "llm_classifier"
                    else "",
                    raw_response=raw_resp[:200],
                )
            )
            logger.info(
                "[CLASSIFY] %s -> %s (%s): %s",
                ticket.ticket_id,
                buildability,
                source,
                ticket.title[:60],
            )

        return ClassifyResult(classifications=tuple(classifications))

    async def _classify_one(self, ticket: ScoredTicket) -> tuple[str, str, str]:
        """Classify a single ticket using local LLM.

        Returns (buildability, source, raw_response).
        """
        prompt = textwrap.dedent(f"""\
            Classify this software ticket for autonomous execution by an AI coding agent.

            Ticket: {ticket.ticket_id}
            Title: {ticket.title}
            Priority: {ticket.priority}
            Labels: {", ".join(ticket.labels) if ticket.labels else "none"}
            Description:
            {ticket.description[:1500] if ticket.description else "(no description)"}

            Classify as exactly one of:
            - auto_buildable: The ticket describes a CONCRETE CODE CHANGE — a bug fix, new test,
              feature implementation, import fix, or refactor with a clear target file/module.
              The agent can implement this fully without human input.
            - needs_arch_decision: Requires architectural decisions, design review, or cross-team
              coordination before code can be written.
            - blocked: Has explicit blockers, external dependencies, or waiting on another ticket.
            - skip: Should be skipped. This includes: planning epics, coordination tickets,
              "needs subplan" tickets, documentation-only tasks, process/workflow tickets,
              tickets about organizing other tickets, session summaries, retrospectives,
              or anything that does NOT result in a code commit.

            IMPORTANT: If the ticket is about planning, organizing, coordinating, tracking,
            or documenting — classify as "skip", NOT "auto_buildable". Only classify as
            "auto_buildable" if the deliverable is actual source code (Python, TypeScript,
            YAML config, test files, etc.).

            Respond with ONLY the classification word, nothing else.
        """)

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{LLM_FAST_URL}/v1/chat/completions",
                    json={
                        "model": "Qwen/Qwen3-14B-AWQ",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 256,
                        "temperature": 0.0,
                        "chat_template_kwargs": {"enable_thinking": False},
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            raw = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
                .lower()
            )

            for candidate in [
                "auto_buildable",
                "needs_arch_decision",
                "blocked",
                "skip",
            ]:
                if candidate in raw:
                    return candidate, "llm_classifier", raw

            logger.warning(
                "[CLASSIFY] Unexpected LLM response for %s: %s — defaulting to needs_arch_decision",
                ticket.ticket_id,
                raw[:100],
            )
            return "needs_arch_decision", "llm_classifier", raw

        except Exception as exc:
            logger.warning(
                "[CLASSIFY] LLM classification failed for %s: %s — falling back to keyword heuristic",
                ticket.ticket_id,
                exc,
            )
            result = self._keyword_fallback(ticket)
            return result, "keyword_fallback", str(exc)

    def _keyword_fallback(self, ticket: ScoredTicket) -> str:
        """Simple keyword fallback if LLM is unavailable."""
        text = f"{ticket.title} {ticket.description}".lower()
        if any(kw in text for kw in ("blocked", "waiting", "depends on")):
            return "blocked"
        if any(kw in text for kw in ("design", "architecture", "rfc", "spike")):
            return "needs_arch_decision"
        if any(
            kw in text
            for kw in (
                "in progress",
                "wip",
                "stale",
                "duplicate",
                "epic",
                "plan",
                "coordinate",
                "organize",
                "needs subplan",
                "session",
                "retrospective",
                "tracking",
                "documentation",
                "docs only",
            )
        ):
            return "skip"
        return "auto_buildable"


# ---------------------------------------------------------------------------
# Live sub-handler: Build Dispatch (worktree + LLM code gen + PR)
# ---------------------------------------------------------------------------
class LiveBuildDispatchHandler:
    """Dispatches builds by creating worktrees, calling LLMs, and opening PRs."""

    def __init__(self, *, dry_run_global: bool = False) -> None:
        self._dry_run_global = dry_run_global

    async def handle(
        self,
        *,
        correlation_id: UUID,
        targets: tuple[BuildTarget, ...],
        dry_run: bool = False,
    ) -> DispatchResult:
        effective_dry_run = dry_run or self._dry_run_global

        logger.info(
            "[DISPATCH] Dispatching %d targets (correlation_id=%s, dry_run=%s)",
            len(targets),
            correlation_id,
            effective_dry_run,
        )

        dispatched = 0
        for target in targets:
            if target.buildability != "auto_buildable":
                logger.info(
                    "[DISPATCH] Skipping %s (buildability=%s)",
                    target.ticket_id,
                    target.buildability,
                )
                continue

            if effective_dry_run:
                logger.info(
                    "[DISPATCH] [DRY-RUN] Would dispatch %s: %s",
                    target.ticket_id,
                    target.title,
                )
                dispatched += 1
                continue

            success = await self._dispatch_one(target, correlation_id)
            if success:
                dispatched += 1

        logger.info("[DISPATCH] Dispatched %d/%d targets", dispatched, len(targets))
        return DispatchResult(total_dispatched=dispatched, delegation_payloads=())

    async def _dispatch_one(self, target: BuildTarget, correlation_id: UUID) -> bool:
        """Dispatch a single ticket: detect repo, create worktree, generate code, open PR."""
        ticket_id = target.ticket_id
        title = target.title

        # Step 1: Detect target repo from ticket description
        repo = self._detect_repo(target)
        if not repo:
            logger.warning(
                "[DISPATCH] Could not detect repo for %s, skipping", ticket_id
            )
            return False

        repo_path = OMNI_HOME / repo
        if not repo_path.exists():
            logger.warning("[DISPATCH] Repo path %s does not exist", repo_path)
            return False

        # Step 2: Create worktree
        branch_name = f"jonah/{ticket_id.lower()}-auto"
        worktree_path = WORKTREE_ROOT / ticket_id / repo
        if worktree_path.exists():
            logger.info("[DISPATCH] Worktree already exists at %s", worktree_path)
        else:
            try:
                worktree_path.parent.mkdir(parents=True, exist_ok=True)
                # Pull latest main first
                subprocess.run(
                    ["git", "-C", str(repo_path), "pull", "--ff-only"],
                    capture_output=True,
                    timeout=30,
                )
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repo_path),
                        "worktree",
                        "add",
                        str(worktree_path),
                        "-b",
                        branch_name,
                    ],
                    capture_output=True,
                    check=True,
                    timeout=30,
                )
                logger.info("[DISPATCH] Created worktree at %s", worktree_path)
            except subprocess.CalledProcessError as exc:
                logger.warning(
                    "[DISPATCH] Failed to create worktree for %s: %s",
                    ticket_id,
                    exc.stderr.decode() if exc.stderr else str(exc),
                )
                return False

        # Step 3: Generate implementation via LLM
        result = await self._generate_implementation(target, repo, worktree_path)
        if not result:
            logger.warning("[DISPATCH] No implementation generated for %s", ticket_id)
            return False

        impl, model_name = result
        logger.info("[DISPATCH] %s generated by model=%s", ticket_id, model_name)

        # Step 4: Apply changes, commit, push, open PR
        success = await self._apply_and_pr(
            ticket_id=ticket_id,
            title=title,
            repo=repo,
            branch_name=branch_name,
            worktree_path=worktree_path,
            implementation=impl,
        )

        # Step 5: Record delegation event with actual model name (OMN-7810)
        if success:
            self._record_delegation(
                ticket_id=ticket_id,
                title=title,
                model_name=model_name,
                correlation_id=correlation_id,
            )

        return success

    def _detect_repo(self, target: BuildTarget) -> str | None:
        """Detect which repo a ticket targets from its title/description."""
        text = f"{target.title}".lower()
        for hint, repo in REPO_HINTS.items():
            if hint.lower() in text:
                return repo
        # Default to omnimarket for build loop tickets
        return "omnimarket"

    @staticmethod
    def _record_delegation(
        *,
        ticket_id: str,
        title: str,
        model_name: str,
        correlation_id: UUID,
    ) -> None:
        """Write delegation record to disk for later Kafka emission.

        The emit daemon picks up JSON files from .onex_state/delegation-events/
        and publishes them as task-delegated events.
        """
        event = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "correlation_id": str(correlation_id),
            "task_type": "code-generation",
            "delegated_to": model_name,
            "delegated_by": "build-loop-orchestrator",
            "quality_gate_passed": True,
            "ticket_id": ticket_id,
            "title": title,
        }
        events_dir = OMNI_HOME / ".onex_state" / "delegation-events"
        events_dir.mkdir(parents=True, exist_ok=True)
        event_path = events_dir / f"{correlation_id}-{ticket_id}.json"
        event_path.write_text(json.dumps(event, indent=2, default=str))
        logger.info(
            "[DISPATCH] Recorded delegation event: %s -> %s", ticket_id, model_name
        )

    async def _generate_implementation(
        self,
        target: BuildTarget,
        repo: str,
        worktree_path: Path,
    ) -> tuple[dict[str, str], str] | None:
        """Call LLM to generate file changes for a ticket.

        Returns (dict of {file_path: content}, model_name) or None on failure.
        """
        # Gather context: read repo structure
        try:
            result = subprocess.run(
                ["find", str(worktree_path / "src"), "-name", "*.py", "-type", "f"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            file_list = result.stdout.strip()[:3000]
        except Exception:
            file_list = "(unable to list files)"

        prompt = textwrap.dedent(f"""\
            You are an autonomous coding agent. Implement the following ticket.

            Ticket: {target.ticket_id}
            Title: {target.title}
            Repository: {repo}

            Repository file structure (partial):
            {file_list}

            Instructions:
            1. Analyze what needs to be done based on the ticket title
            2. Generate the minimal code changes needed
            3. Respond with a JSON object where keys are file paths (relative to repo root)
               and values are the complete file contents

            Respond with ONLY a JSON object, no markdown fencing, no explanation.
            Example: {{"src/foo/bar.py": "# file contents..."}}

            If you cannot determine what to implement, respond with: {{"_skip": "reason"}}
        """)

        # Tier 1: GLM-4.5 (primary frontier code gen)
        if LLM_GLM_API_KEY and LLM_GLM_URL:
            logger.info(
                "[DISPATCH] Trying GLM-4.5 (%s) for %s",
                LLM_GLM_MODEL_NAME,
                target.ticket_id,
            )
            impl = await self._call_llm(
                url=f"{LLM_GLM_URL}/chat/completions",
                model=LLM_GLM_MODEL_NAME,
                prompt=prompt,
                max_tokens=4096,
                api_key=LLM_GLM_API_KEY,
            )
            if impl and "_skip" not in impl:
                return impl, LLM_GLM_MODEL_NAME

        # Tier 2: Local coder (Qwen3-Coder-30B, longer context)
        coder_model = "cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit"
        impl = await self._call_llm(
            url=f"{LLM_CODER_URL}/v1/chat/completions",
            model=coder_model,
            prompt=prompt,
            max_tokens=4096,
        )

        if impl and "_skip" not in impl:
            return impl, "qwen3-coder-30b"

        # Tier 3: Frontier fallback (OpenAI)
        if OPENAI_API_KEY:
            logger.info(
                "[DISPATCH] Local LLM skipped/failed, trying OpenAI for %s",
                target.ticket_id,
            )
            impl = await self._call_llm(
                url=f"{OPENAI_BASE_URL}/chat/completions",
                model="gpt-4o-mini",
                prompt=prompt,
                max_tokens=4096,
                api_key=OPENAI_API_KEY,
            )
            if impl and "_skip" not in impl:
                return impl, "gpt-4o-mini"

        return None

    async def _call_llm(
        self,
        url: str,
        model: str,
        prompt: str,
        max_tokens: int = 4096,
        api_key: str | None = None,
    ) -> dict[str, str] | None:
        """Call an OpenAI-compatible LLM endpoint and parse JSON response."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    url,
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": max_tokens,
                        "temperature": 0.2,
                    },
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()

            # OMN-7810: Record LLM cost event for cost trends dashboard
            self._record_llm_cost(model=model, response_data=data)

            raw = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )

            # Strip thinking tags first (Qwen3 models emit <think>...</think> before
            # the actual response). Do this before markdown stripping so we see the
            # actual output content.
            if "<think>" in raw:
                think_end = raw.rfind("</think>")
                if think_end >= 0:
                    raw = raw[think_end + len("</think>") :].strip()

            # Strip markdown fencing if present
            if raw.startswith("```"):
                lines = raw.split("\n")
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                raw = "\n".join(lines).strip()

            # Extract the first JSON object from the response. Models sometimes
            # prefix with explanation text or suffix with commentary after the JSON.
            brace_start = raw.find("{")
            brace_end = raw.rfind("}")
            if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
                raw = raw[brace_start : brace_end + 1]

            result: dict[str, str] = json.loads(raw)
            return result

        except (json.JSONDecodeError, httpx.HTTPError, KeyError) as exc:
            logger.warning("[DISPATCH] LLM call failed (%s): %s", url[:50], exc)
            return None

    @staticmethod
    def _record_llm_cost(*, model: str, response_data: dict[str, object]) -> None:
        """Write llm-call-completed event to disk for Kafka emission.

        Extracts usage data from OpenAI-compatible response and writes to
        .onex_state/llm-cost-events/ for the emit daemon to publish to
        onex.evt.omniintelligence.llm-call-completed.v1.
        """
        usage_raw = response_data.get("usage")
        usage: dict[str, object] = usage_raw if isinstance(usage_raw, dict) else {}
        _pt = usage.get("prompt_tokens")
        _ct = usage.get("completion_tokens")
        _tt = usage.get("total_tokens")
        prompt_tokens: int = _pt if isinstance(_pt, int) else 0
        completion_tokens: int = _ct if isinstance(_ct, int) else 0
        total_tokens: int = (
            _tt if isinstance(_tt, int) else prompt_tokens + completion_tokens
        )

        event = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "model_name": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "usage_source": "API" if usage else "ESTIMATED",
            "estimated_cost_usd": _estimate_cost(model, total_tokens),
            "total_cost_usd": _estimate_cost(model, total_tokens),
            "reported_cost_usd": 0,
            "request_count": 1,
            "granularity": "hour",
            "reporting_source": "build-loop",
        }

        events_dir = OMNI_HOME / ".onex_state" / "llm-cost-events"
        events_dir.mkdir(parents=True, exist_ok=True)
        event_path = events_dir / f"{uuid4()}.json"
        event_path.write_text(json.dumps(event, indent=2, default=str))

    async def _apply_and_pr(
        self,
        *,
        ticket_id: str,
        title: str,
        repo: str,
        branch_name: str,
        worktree_path: Path,
        implementation: dict[str, str],
    ) -> bool:
        """Write files, commit, push, and open a PR."""
        if not implementation:
            return False

        # Write files
        files_written = 0
        written_paths: list[str] = []
        for rel_path, content in implementation.items():
            if rel_path.startswith("_"):
                continue
            full_path = worktree_path / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content)
            files_written += 1
            written_paths.append(str(full_path))
            logger.info("[DISPATCH] Wrote %s", full_path)

        if files_written == 0:
            logger.warning("[DISPATCH] No files written for %s", ticket_id)
            return False

        # Stage, run pre-commit, commit, push
        try:
            subprocess.run(
                ["git", "-C", str(worktree_path), "add", "-A"],
                check=True,
                capture_output=True,
                timeout=30,
            )

            # Run ruff format + check using omni_home venv (worktree venvs lack
            # editable deps, causing pre-commit ruff hooks to fail on dependency
            # resolution). This validates generated code before committing.
            repo_venv = OMNI_HOME / repo / ".venv"
            ruff_bin = str(repo_venv / "bin" / "ruff") if repo_venv.exists() else "ruff"

            # Format only the written files (not the whole worktree — pre-existing
            # violations in other files must not block dispatch).
            subprocess.run(
                [ruff_bin, "format", *written_paths],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(worktree_path),
            )
            # Lint with auto-fix on written files only
            subprocess.run(
                [ruff_bin, "check", "--fix", *written_paths],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(worktree_path),
            )
            # Check for blocking errors only (syntax, undefined names) on written files
            blocking_result = subprocess.run(
                [ruff_bin, "check", "--select", "E,F", *written_paths],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(worktree_path),
            )
            if blocking_result.returncode != 0:
                logger.warning(
                    "[DISPATCH] ruff blocking errors for %s:\n%s",
                    ticket_id,
                    blocking_result.stdout[-500:]
                    if blocking_result.stdout
                    else "(no output)",
                )
                logger.error(
                    "[DISPATCH] Generated code has syntax/import errors for %s, skipping",
                    ticket_id,
                )
                return False

            logger.info("[DISPATCH] ruff checks passed for %s", ticket_id)

            # Stamp SPDX headers on all written Python files. Pre-commit hooks in
            # target repos enforce SPDX headers — LLM-generated code won't have them.
            spdx_line = "# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.\n# SPDX-License-Identifier: MIT\n"
            for wp in written_paths:
                if wp.endswith(".py"):
                    try:
                        existing = Path(wp).read_text()
                        if "SPDX-FileCopyrightText" not in existing:
                            Path(wp).write_text(spdx_line + existing)
                    except Exception:
                        pass

            # Run full ruff check --fix (all rules, not just E,F) to auto-fix
            # BLE001 and other lint violations the LLM may introduce.
            subprocess.run(
                [ruff_bin, "check", "--fix", "--unsafe-fixes", *written_paths],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(worktree_path),
            )

            # Re-stage in case ruff/SPDX modified files
            subprocess.run(
                ["git", "-C", str(worktree_path), "add", "-A"],
                check=True,
                capture_output=True,
                timeout=30,
            )

            # Check if there's anything to commit
            diff_result = subprocess.run(
                ["git", "-C", str(worktree_path), "diff", "--cached", "--quiet"],
                capture_output=True,
                timeout=10,
            )
            if diff_result.returncode == 0:
                logger.warning(
                    "[DISPATCH] Nothing to commit for %s (ruff/SPDX may have reverted changes)",
                    ticket_id,
                )
                return False

            commit_msg = f"feat: {ticket_id} — {title}\n\nAuto-generated by build loop (assemble_live)."
            subprocess.run(
                ["git", "-C", str(worktree_path), "commit", "-m", commit_msg],
                check=True,
                capture_output=True,
                timeout=60,
            )

            subprocess.run(
                [
                    "git",
                    "-C",
                    str(worktree_path),
                    "push",
                    "-u",
                    "origin",
                    branch_name,
                ],
                check=True,
                capture_output=True,
                timeout=60,
            )
            logger.info("[DISPATCH] Pushed %s to origin/%s", ticket_id, branch_name)

        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode() if exc.stderr else str(exc)
            logger.warning(
                "[DISPATCH] Git operation failed for %s: %s", ticket_id, stderr
            )
            return False

        # Open PR
        try:
            pr_body = (
                f"## Summary\n\n"
                f"Auto-generated implementation for {ticket_id}.\n\n"
                f"**Ticket:** {title}\n\n"
                f"## Files Changed\n\n"
                + "\n".join(f"- `{p}`" for p in implementation if not p.startswith("_"))
                + "\n\n## Test plan\n\n- [ ] Review generated code\n- [ ] Run tests\n"
            )

            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "create",
                    "--repo",
                    f"OmniNode-ai/{repo}",
                    "--title",
                    f"{ticket_id}: {title[:60]}",
                    "--body",
                    pr_body,
                    "--head",
                    branch_name,
                    "--base",
                    "main",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(worktree_path),
            )

            if result.returncode == 0:
                pr_url = result.stdout.strip()
                logger.info("[DISPATCH] PR created for %s: %s", ticket_id, pr_url)
                return True
            logger.warning(
                "[DISPATCH] PR creation failed for %s: %s",
                ticket_id,
                result.stderr,
            )
            return False

        except Exception as exc:
            logger.warning("[DISPATCH] PR creation error for %s: %s", ticket_id, exc)
            return False


# ---------------------------------------------------------------------------
# Assembly + main
# ---------------------------------------------------------------------------
async def run_build_loop(
    max_cycles: int = 3,
    dry_run: bool = False,
    max_tickets: int = 5,
) -> None:
    """Assemble and run the live build loop."""
    correlation_id = uuid4()
    started_at = datetime.now(tz=UTC)

    logger.info(
        "=== BUILD LOOP LIVE START === (correlation_id=%s, max_cycles=%d, dry_run=%s)",
        correlation_id,
        max_cycles,
        dry_run,
    )

    # Wire sub-handlers
    orchestrator = HandlerBuildLoopOrchestrator(
        closeout=LiveCloseoutHandler(),
        verify=LiveVerifyHandler(),
        rsd_fill=LiveRsdFillHandler(),
        classify=LiveTicketClassifyHandler(),
        dispatch=LiveBuildDispatchHandler(dry_run_global=dry_run),
        event_bus=None,  # No Kafka for standalone execution
    )

    # Create start command
    command = ModelLoopStartCommand(
        correlation_id=correlation_id,
        mode="build",
        dry_run=dry_run,
        skip_closeout=True,  # Skip closeout for build-focused runs
        max_cycles=max_cycles,
        requested_at=started_at,
    )

    # Run
    result = await orchestrator.handle(command)

    # Report
    elapsed = (datetime.now(tz=UTC) - started_at).total_seconds()
    logger.info(
        "=== BUILD LOOP LIVE END === "
        "cycles_completed=%d, cycles_failed=%d, total_dispatched=%d, "
        "elapsed=%.1fs",
        result.cycles_completed,
        result.cycles_failed,
        result.total_tickets_dispatched,
        elapsed,
    )

    # Write result to disk
    result_path = (
        OMNI_HOME / ".onex_state" / "build-loop-results" / f"{correlation_id}.json"
    )
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(result.model_dump(mode="json"), indent=2, default=str)
    )
    logger.info("Result written to %s", result_path)

    for i, summary in enumerate(result.cycle_summaries):
        logger.info(
            "  Cycle %d: phase=%s, filled=%d, classified=%d, dispatched=%d%s",
            i + 1,
            summary.final_phase.value,
            summary.tickets_filled,
            summary.tickets_classified,
            summary.tickets_dispatched,
            f" ERROR: {summary.error_message}" if summary.error_message else "",
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the autonomous build loop live")
    parser.add_argument("--max-cycles", type=int, default=3, help="Max build cycles")
    parser.add_argument("--dry-run", action="store_true", help="Skip actual dispatch")
    parser.add_argument(
        "--max-tickets", type=int, default=5, help="Max tickets per cycle"
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    asyncio.run(
        run_build_loop(
            max_cycles=args.max_cycles,
            dry_run=args.dry_run,
            max_tickets=args.max_tickets,
        )
    )


if __name__ == "__main__":
    main()
