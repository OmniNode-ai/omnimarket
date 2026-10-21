"""Model roles resolve only after their routing-contract promotion (OMN-21145)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_context_pack_failure import EnumContextPackFailure

from omnimarket.models.context_profile_resolve.model_context_profile_resolve_request import (
    ModelContextProfileResolveRequest,
)
from omnimarket.models.context_profile_resolve.model_context_profile_resolve_result import (
    EnumContextProfileResolveStatus,
)
from omnimarket.nodes.node_context_profile_resolver_effect.handlers.handler_context_profile_resolver import (
    HandlerContextProfileResolver,
)

ROLE_FIELDS = (
    "extraction_task_class",
    "selection_task_class",
    "summary_task_class",
)


def _entry() -> dict[str, object]:
    return {
        "gateway_exposure": "internal",
        "selection": {"priority": 0, "phrases": []},
    }


def _contract(path: Path, classes: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump({"task_classes": classes}), encoding="utf-8")


def _profile(path: Path, **roles: str) -> None:
    (path / "roles.yaml").write_text(
        yaml.safe_dump({"model_id": "fixture-model", **roles}), encoding="utf-8"
    )


@pytest.mark.parametrize("field", ROLE_FIELDS)
def test_unpromoted_role_is_invalid_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    contract = tmp_path / "routing.yaml"
    _contract(contract, {"other_task": _entry()})
    monkeypatch.setenv("TASK_CLASS_CONTRACT_PATH", str(contract))
    _profile(tmp_path, **{field: "unpromoted_role"})

    result = HandlerContextProfileResolver(tmp_path).handle(
        ModelContextProfileResolveRequest(profile_name="roles")
    )

    assert result.status is EnumContextProfileResolveStatus.FAILED
    assert result.failure_class is EnumContextPackFailure.INVALID_PROFILE
    assert result.profile is None
    assert result.profile_content_hash is None
    assert any(field in error and "unpromoted_role" in error for error in result.errors)


def test_promoted_roles_resolve_and_removing_entry_refuses_next_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = tmp_path / "routing.yaml"
    classes = {f"promoted_{field}": _entry() for field in ROLE_FIELDS}
    _contract(contract, classes)
    monkeypatch.setenv("TASK_CLASS_CONTRACT_PATH", str(contract))
    _profile(tmp_path, **{field: f"promoted_{field}" for field in ROLE_FIELDS})
    handler = HandlerContextProfileResolver(tmp_path)
    request = ModelContextProfileResolveRequest(profile_name="roles")

    first = handler.handle(request)
    assert first.status is EnumContextProfileResolveStatus.OK
    assert first.profile is not None
    assert first.profile_content_hash == first.profile.content_hash()
    assert handler.handle(request) == first

    del classes["promoted_extraction_task_class"]
    _contract(contract, classes)
    removed = handler.handle(request)
    assert removed.failure_class is EnumContextPackFailure.INVALID_PROFILE
    assert removed.profile is None
    assert removed.profile_content_hash is None


@pytest.mark.parametrize(
    "contents", [None, "task_classes: [broken", "[]", "task_classes: {}"]
)
def test_unreadable_or_invalid_routing_contract_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: str | None
) -> None:
    contract = tmp_path / "routing.yaml"
    if contents is not None:
        contract.write_text(contents, encoding="utf-8")
    monkeypatch.setenv("TASK_CLASS_CONTRACT_PATH", str(contract))
    _profile(tmp_path, extraction_task_class="unpromoted_role")

    result = HandlerContextProfileResolver(tmp_path).handle(
        ModelContextProfileResolveRequest(profile_name="roles")
    )
    assert result.failure_class is EnumContextPackFailure.INVALID_PROFILE
    assert result.profile is None
    assert result.profile_content_hash is None


def test_profile_without_model_roles_does_not_read_routing_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TASK_CLASS_CONTRACT_PATH", str(tmp_path / "missing.yaml"))
    _profile(tmp_path)
    result = HandlerContextProfileResolver(tmp_path).handle(
        ModelContextProfileResolveRequest(profile_name="roles")
    )
    assert result.status is EnumContextProfileResolveStatus.OK
    assert result.profile is not None


def test_packaged_routing_contract_is_used_without_an_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TASK_CLASS_CONTRACT_PATH", raising=False)
    _profile(tmp_path, summary_task_class="unpromoted_fixture_role")
    result = HandlerContextProfileResolver(tmp_path).handle(
        ModelContextProfileResolveRequest(profile_name="roles")
    )
    assert result.failure_class is EnumContextPackFailure.INVALID_PROFILE
    assert result.errors == (
        "summary_task_class: no routing-contract entry for 'unpromoted_fixture_role'",
    )


@pytest.mark.parametrize("status", ["withheld", "pending_capability"])
def test_declared_unavailable_role_is_invalid_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    contract = tmp_path / "routing.yaml"
    entry = _entry()
    entry["routing_availability"] = {
        "status": status,
        "missing_capability": "fixture-capability",
        "reason": "No rung has been promoted.",
        "tracking": "fixture-tracking",
    }
    _contract(contract, {"unpromoted_role": entry})
    monkeypatch.setenv("TASK_CLASS_CONTRACT_PATH", str(contract))
    _profile(tmp_path, extraction_task_class="unpromoted_role")
    result = HandlerContextProfileResolver(tmp_path).handle(
        ModelContextProfileResolveRequest(profile_name="roles")
    )
    assert result.failure_class is EnumContextPackFailure.INVALID_PROFILE
    assert any(status in error for error in result.errors)


@pytest.mark.parametrize(
    ("name", "missing_fields"),
    [
        ("compaction_orchestrator", ("summary_task_class",)),
        ("session_resume", ("summary_task_class",)),
    ],
)
def test_shipped_profiles_refuse_roles_missing_from_routing_contract(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    missing_fields: tuple[str, ...],
) -> None:
    monkeypatch.delenv("TASK_CLASS_CONTRACT_PATH", raising=False)
    result = HandlerContextProfileResolver().handle(
        ModelContextProfileResolveRequest(profile_name=name)
    )
    assert result.status is EnumContextProfileResolveStatus.FAILED
    assert result.failure_class is EnumContextPackFailure.INVALID_PROFILE
    assert result.profile is None
    assert result.profile_content_hash is None
    assert tuple(error.split(":", 1)[0] for error in result.errors) == missing_fields
    assert all("no routing-contract entry" in error for error in result.errors)


@pytest.mark.parametrize(
    "name",
    [
        "agent_spawn",
        "delegation_payload_local",
        "lane_brief",
        "lane_brief_section",
        "session_start",
        "per_prompt",
    ],
)
def test_shipped_profiles_resolve_with_the_packaged_routing_contract(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.delenv("TASK_CLASS_CONTRACT_PATH", raising=False)
    result = HandlerContextProfileResolver().handle(
        ModelContextProfileResolveRequest(profile_name=name)
    )
    assert result.status is EnumContextProfileResolveStatus.OK
    assert result.profile is not None
    assert result.profile_content_hash == result.profile.content_hash()
