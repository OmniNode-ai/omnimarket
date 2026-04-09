"""HandlerThreadWatcher — polls GitHub for review thread resolution events.

Implements ProtocolThreadWatcher from handler_fsm.py.

Responsibilities:
- Poll GitHub review comments for each POSTED thread to detect resolution.
- A thread is considered resolved when a reply containing the
  ``@omnibot-judge verify`` trigger is present (explicit opt-in resolution),
  OR when the GitHub ``resolved`` flag is set on the comment.
- Updates ThreadState objects in-place with reply content, resolved_at
  timestamp, and status transition (POSTED -> RESOLVED).
- Configurable poll interval (default 30 s) and max wait time (default
  600 s / 10 min) to accommodate R4 judge cold-start latency.
- Exits early (returns) once all POSTED threads have moved out of POSTED status
  or the max wait time elapses.
- Does NOT perform judge verification — that is HandlerJudgeVerifier's job.
  This handler only updates status from POSTED to RESOLVED.

Adversarial design doc concerns addressed:
- R2: Polling-based resolution detection; caller should also wire webhook
  triggers. Each poll fetches full thread comments to minimise miss windows.
- R4: Max wait time defaults to 600 s; judge cold start is ~30 s.
- R6: Re-verify trigger rate-limit is enforced here (max 3 per finding).
- R8: All GitHub API calls go through AdapterGitHubBridge which handles
  rate-limit backoff automatically.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from omnimarket.nodes.node_pr_review_bot.adapter_github_bridge import (
    AdapterGitHubBridge,
    ReviewThread,
)
from omnimarket.nodes.node_pr_review_bot.handlers.handler_fsm import (
    ProtocolThreadWatcher,
)
from omnimarket.nodes.node_pr_review_bot.models.models import (
    EnumThreadStatus,
    ThreadState,
)

logger = logging.getLogger(__name__)

# Trigger phrase that a PR author posts to request judge verification
_VERIFY_TRIGGER = "@omnibot-judge verify"
# Maximum judge re-verify requests per finding (design doc R6)
_MAX_VERIFY_ATTEMPTS = 3


class HandlerThreadWatcher(ProtocolThreadWatcher):
    """Polls GitHub for resolution events on posted review threads.

    Args:
        github_bridge: Adapter for all GitHub API calls.
        poll_interval_seconds: Seconds between each polling cycle.
        max_wait_seconds: Maximum total wait time before returning whatever
            thread states have been gathered.
        bot_login: GitHub login of the review bot account (used to identify
            bot-posted threads vs. human comments).
    """

    def __init__(
        self,
        github_bridge: AdapterGitHubBridge,
        *,
        poll_interval_seconds: float = 30.0,
        max_wait_seconds: float = 600.0,
        bot_login: str = "omnibot-review",
    ) -> None:
        self._bridge = github_bridge
        self._poll_interval = poll_interval_seconds
        self._max_wait = max_wait_seconds
        self._bot_login = bot_login

    # ------------------------------------------------------------------
    # ProtocolThreadWatcher implementation
    # ------------------------------------------------------------------

    def watch(
        self,
        pr_number: int,
        repo: str,
        thread_states: tuple[ThreadState, ...],
    ) -> list[ThreadState]:
        """Synchronous entry point — runs the async poll loop via asyncio.

        Returns updated ThreadState list. Threads that are already resolved,
        verified, or escalated are passed through unchanged. Only POSTED
        threads are polled.
        """
        return asyncio.run(self._watch_async(pr_number, repo, thread_states))

    # ------------------------------------------------------------------
    # Async implementation
    # ------------------------------------------------------------------

    async def _watch_async(
        self,
        pr_number: int,
        repo: str,
        thread_states: tuple[ThreadState, ...],
    ) -> list[ThreadState]:
        """Async poll loop. Exits when all POSTED threads are resolved or
        max_wait_seconds elapses."""
        # Work on a mutable copy — ThreadState is not frozen
        states = list(thread_states)

        # Only poll threads that are currently in POSTED state
        watchable_ids = {
            t.github_thread_id
            for t in states
            if t.status == EnumThreadStatus.POSTED and t.github_thread_id is not None
        }

        if not watchable_ids:
            logger.debug(
                "HandlerThreadWatcher: no POSTED threads to watch for PR #%d", pr_number
            )
            return states

        logger.info(
            "HandlerThreadWatcher: watching %d thread(s) on %s PR #%d "
            "(poll_interval=%.0fs, max_wait=%.0fs)",
            len(watchable_ids),
            repo,
            pr_number,
            self._poll_interval,
            self._max_wait,
        )

        deadline = asyncio.get_event_loop().time() + self._max_wait
        poll_count = 0

        while True:
            remaining_time = deadline - asyncio.get_event_loop().time()
            if remaining_time <= 0:
                logger.warning(
                    "HandlerThreadWatcher: max wait time elapsed for PR #%d "
                    "— %d thread(s) still POSTED",
                    pr_number,
                    sum(1 for s in states if s.status == EnumThreadStatus.POSTED),
                )
                break

            poll_count += 1
            logger.debug(
                "HandlerThreadWatcher: poll #%d for PR #%d", poll_count, pr_number
            )

            try:
                states = await self._poll_once(pr_number, repo, states)
            except Exception:
                logger.exception(
                    "HandlerThreadWatcher: poll error on PR #%d (will retry)", pr_number
                )

            # Exit early if no threads remain in POSTED state
            still_posted = [s for s in states if s.status == EnumThreadStatus.POSTED]
            if not still_posted:
                logger.info(
                    "HandlerThreadWatcher: all threads resolved for PR #%d after "
                    "%d poll(s)",
                    pr_number,
                    poll_count,
                )
                break

            # Sleep for poll interval (or until deadline, whichever is sooner)
            sleep_time = min(self._poll_interval, remaining_time)
            await asyncio.sleep(sleep_time)

        return states

    async def _poll_once(
        self,
        pr_number: int,
        repo: str,
        states: list[ThreadState],
    ) -> list[ThreadState]:
        """Fetch current GitHub state for all POSTED threads and update states."""
        # Fetch all PR review comments in one paginated call to minimise API
        # requests (design doc R8 — rate-limit awareness).
        all_threads = await self._bridge.fetch_review_threads(repo, pr_number)
        thread_map: dict[int, ReviewThread] = {t.id: t for t in all_threads}

        updated: list[ThreadState] = []
        for state in states:
            if state.status != EnumThreadStatus.POSTED:
                updated.append(state)
                continue

            if state.github_thread_id is None:
                updated.append(state)
                continue

            root_thread = thread_map.get(state.github_thread_id)
            if root_thread is None:
                logger.warning(
                    "HandlerThreadWatcher: thread %d not found on PR #%d — "
                    "may have been deleted",
                    state.github_thread_id,
                    pr_number,
                )
                updated.append(state)
                continue

            new_state = await self._evaluate_thread(
                pr_number, repo, state, root_thread, all_threads
            )
            updated.append(new_state)

        return updated

    async def _evaluate_thread(
        self,
        pr_number: int,
        repo: str,
        state: ThreadState,
        root_thread: ReviewThread,
        all_threads: list[ReviewThread],
    ) -> ThreadState:
        """Determine whether a thread has been resolved and update state."""
        assert state.github_thread_id is not None

        # Collect all comments in this thread (root + replies)
        thread_comments = [
            t
            for t in all_threads
            if t.id == state.github_thread_id
            or t.in_reply_to_id == state.github_thread_id
        ]

        # Check for explicit resolution trigger from a non-bot author
        verify_requests = [
            c
            for c in thread_comments
            if _VERIFY_TRIGGER in c.body and c.user_login != self._bot_login
        ]

        # GitHub REST does not reliably expose the resolved flag on individual
        # comments. We treat the thread as resolved when:
        # 1. The GitHub ``resolved`` field is True on the root comment, OR
        # 2. A non-bot reply containing ``@omnibot-judge verify`` is found.
        is_github_resolved = root_thread.resolved
        has_verify_trigger = len(verify_requests) > 0

        if not is_github_resolved and not has_verify_trigger:
            return state  # not yet resolved

        # Enforce R6: max verify attempts
        if state.verify_attempts >= _MAX_VERIFY_ATTEMPTS:
            logger.warning(
                "HandlerThreadWatcher: thread %d (finding %s) has reached max "
                "verify attempts (%d) — escalating",
                state.github_thread_id,
                state.finding_id,
                _MAX_VERIFY_ATTEMPTS,
            )
            state.status = EnumThreadStatus.ESCALATED
            state.resolved_at = datetime.now(tz=UTC)
            return state

        # Collect reply content for the judge verifier (stored in judge_reasoning
        # temporarily as raw conversation text; verifier will overwrite with verdict)
        reply_bodies = [
            c.body for c in thread_comments if c.user_login != self._bot_login
        ]
        conversation_text = "\n---\n".join(reply_bodies) if reply_bodies else ""

        logger.info(
            "HandlerThreadWatcher: thread %d resolved for finding %s "
            "(github_resolved=%s, verify_trigger=%s)",
            state.github_thread_id,
            state.finding_id,
            is_github_resolved,
            has_verify_trigger,
        )

        state.status = EnumThreadStatus.RESOLVED
        state.resolved_at = datetime.now(tz=UTC)
        # Stash the conversation text so HandlerJudgeVerifier can read it
        # without an extra API call. This field is repurposed temporarily —
        # judge_reasoning is overwritten with the actual verdict after verification.
        state.judge_reasoning = conversation_text if conversation_text else None

        return state


__all__: list[str] = ["HandlerThreadWatcher"]
