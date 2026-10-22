# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19401: correct causal answers that state a purpose or a prevented result.

240 explanation prompts ("explain why ...", "briefly explain why ...") were
sent to Qwen3.8-27B on a lab model server (2026-10-20) without the
step_by_step_explanation directive, the case of a model that does not number
its steps. The check refused nine answers, all of them correct explanations.
They walk the mechanism through with a result clause led by "preventing",
"ensuring" or "meaning", with "which prevents", or with a purpose clause ("to
ensure that", "to prevent"), which no marker carried. The same 60 prompts sent
with the directive came back as numbered steps and all passed.

The negative controls are answers that assert without explaining, plus prose
that holds a new marker's letters outside the causal use ("Preventing outages
is the goal", "the ensuing outage", "meaningful names"): those must still fail.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    _HEURISTIC_CONTAINS_ANY_CHECKS,
    _check_contains_any,
)

pytestmark = pytest.mark.unit

_VETO = "TASK_MISMATCH: failed step_by_step_explanation"

_CORRECT_CAUSAL_ANSWERS: dict[str, str] = {
    # Verbatim Qwen3.8-27B answer to: Explain in three sentences how it comes
    # about that garbage collection pauses a Java program.
    "lab_gc_pause_comma_ensuring": (
        "The Java Virtual Machine (JVM) periodically suspends the "
        "application's execution threads to perform garbage collection, "
        "ensuring that memory allocation can continue without running out of "
        "available heap space. During this pause, the collector identifies "
        "which objects are still reachable and which are no longer "
        "referenced, effectively marking them for reclamation. This temporary "
        'halt, known as "stop-the-world," allows the collector to reclaim '
        "unused memory and compact the heap, after which the application "
        "threads are resumed."
    ),
    # Verbatim Qwen3.8-27B answer to: Explain in two sentences why exponential
    # backoff reduces load during an outage.
    "lab_backoff_which_prevents": (
        "Exponential backoff spreads out retry attempts over increasingly "
        "longer intervals, which prevents a sudden surge of simultaneous "
        "requests from overwhelming the recovering system. This gradual "
        "reduction in request frequency allows the service to stabilize and "
        "process traffic steadily rather than being crushed by a thundering "
        "herd of immediate retries."
    ),
    # Verbatim Qwen3.8-27B answer to: In two or three sentences, explain why
    # garbage collection pauses a Java program.
    "lab_gc_pause_to_ensure_that": (
        'The JVM pauses execution to perform a "stop-the-world" phase where '
        "it suspends all application threads to ensure that object references "
        "remain stable and consistent during the marking of live objects. "
        "This pause is necessary to accurately determine which objects are "
        "reachable and which are eligible for reclamation without the risk of "
        "race conditions or memory corruption."
    ),
    # Verbatim Qwen3.8-27B answer to: In two or three sentences, explain why
    # HTTPS stops a man-in-the-middle from reading traffic.
    "lab_https_comma_meaning": (
        "HTTPS uses encryption to create a secure tunnel between the user's "
        "browser and the server, meaning all data is scrambled so that a "
        "man-in-the-middle can only see unreadable ciphertext. Additionally, "
        "digital certificates allow the browser to verify it is communicating "
        "with the legitimate server rather than an impersonator, preventing "
        "the attacker from successfully intercepting or modifying the "
        "connection."
    ),
    # Verbatim Qwen3.8-27B answer to: Explain why exponential backoff reduces
    # load during an outage, briefly.
    "lab_backoff_comma_preventing": (
        "Exponential backoff reduces load during an outage by introducing "
        "**randomized, increasing delays** between retry attempts.\n\nInstead "
        "of clients hammering the server with constant requests (which "
        "creates a thundering herd problem and can keep the service down), "
        "each client waits a progressively longer time before retrying. This "
        "spreads out the traffic spikes over time, preventing the service "
        "from being overwhelmed by a sudden surge of retries and giving it a "
        "chance to recover."
    ),
    # Verbatim Qwen3.8-27B answer to: Explain in two sentences why a memory leak
    # in a long-running process eventually crashes it.
    "lab_memory_leak_to_prevent": (
        "As a memory leak persists, the process continuously allocates memory "
        "without releasing it, gradually exhausting the available system RAM. "
        "Once the system runs out of memory and cannot satisfy further "
        "allocation requests, the operating system or the application\u2019s "
        "runtime environment forcibly terminates the process to prevent "
        "system-wide instability."
    ),
    # Verbatim Qwen3.8-27B answer to: Explain why an unbounded retry loop
    # amplifies an outage, briefly.
    "lab_retry_loop_and_preventing": (
        "An unbounded retry loop amplifies an outage by creating a **positive "
        "feedback loop** where the initial failure triggers an exponential "
        "increase in traffic. Instead of allowing the system to stabilize or "
        "fail gracefully, clients aggressively re-send requests to an already "
        "struggling or downed service. This flood of redundant traffic "
        "consumes critical resources (bandwidth, CPU, and thread pools), "
        "often overwhelming the system further and preventing any recovery "
        "attempts. This phenomenon is commonly known as a **retry storm**, "
        "which turns a localized issue into a systemic collapse."
    ),
    # Verbatim Qwen3.8-27B answer to: Explain in two sentences why an unbounded
    # retry loop amplifies an outage.
    "lab_retry_loop_comma_preventing": (
        "An unbounded retry loop floods the failing downstream service with "
        "excessive traffic, preventing it from stabilizing and extending the "
        'duration of the failure. This phenomenon, often called a "thundering '
        'herd," consumes shared resources and network capacity, which can '
        "cascade the outage to other healthy parts of the system."
    ),
    # Verbatim Qwen3.8-27B answer to: Explain in two sentences why a write-ahead
    # log makes a database crash-safe.
    "lab_wal_comma_ensuring": (
        "A write-ahead log forces all modifications to be recorded on stable "
        "storage before they are applied to the actual data files, ensuring "
        "that every committed transaction is preserved even if power is lost. "
        "Upon recovery, the database system can replay these logged records "
        "to reconstruct the database state, guaranteeing that no committed "
        "data is lost and uncommitted changes are rolled back."
    ),
    "comma_preventing": (
        "A lease expires on its own after thirty seconds, preventing a crashed "
        "holder from keeping the lock forever."
    ),
    "and_preventing": (
        "The client resends every failed request at once, flooding the service "
        "and preventing its queue from ever draining."
    ),
    "comma_ensuring": (
        "Each message carries an idempotency key that the consumer records, "
        "ensuring a redelivered message is applied only once."
    ),
    "comma_meaning": (
        "The replica applies the primary's log asynchronously, meaning a read "
        "from it can miss a write the primary already acknowledged."
    ),
    "to_ensure_that": (
        "The build pins every dependency by hash to ensure that two builds of "
        "the same commit install the same code."
    ),
    "to_prevent": (
        "The kernel kills the largest process to prevent the whole machine "
        "from running out of memory."
    ),
}

_NON_EXPLANATIONS: dict[str, str] = {
    "bare_assertion": "Exponential backoff is a retry strategy used by clients.",
    "list_of_facts": "A write-ahead log is a file. PostgreSQL has one. SQLite has one too.",
    "preventing_as_subject": "Preventing outages is the goal of every on-call team.",
    "ensuing_inside_a_word": "The ensuing outage lasted an hour.",
    "meaningful_inside_a_word": "Meaningful names matter in large codebases.",
    "ensure_as_instruction": "Please ensure the tests pass before merging.",
    "refusal": "I cannot answer that question.",
}


def _run_step_by_step_check(content: str) -> str | None:
    category, markers = _HEURISTIC_CONTAINS_ANY_CHECKS["step_by_step_explanation"]
    return _check_contains_any(
        content,
        check_name="step_by_step_explanation",
        category=category,
        markers=markers,
    )


@pytest.mark.parametrize(
    "content",
    list(_CORRECT_CAUSAL_ANSWERS.values()),
    ids=list(_CORRECT_CAUSAL_ANSWERS),
)
def test_correct_causal_answer_passes_step_by_step_explanation(content: str) -> None:
    assert _run_step_by_step_check(content) is None


@pytest.mark.parametrize(
    "content",
    list(_NON_EXPLANATIONS.values()),
    ids=list(_NON_EXPLANATIONS),
)
def test_non_explanation_is_still_vetoed(content: str) -> None:
    assert _run_step_by_step_check(content) == _VETO
