"""The triage handler's pointer comment names the routing policy model's repository path."""

from __future__ import annotations

from pathlib import Path

HANDLER = (
    Path(__file__).resolve().parents[2]
    / "src/omnimarket/nodes/node_merge_sweep_triage_orchestrator/handlers/handler_triage.py"
)
POLICY_PATH = "src/omnibase_core/models/routing/model_routing_policy.py"


def test_pointer_comment_names_the_routing_policy_repository_path() -> None:
    assert POLICY_PATH in HANDLER.read_text(encoding="utf-8")
