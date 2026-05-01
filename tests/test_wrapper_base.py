"""Tests for shared OmniMarket adapter wrapper helpers."""

from __future__ import annotations

import argparse
import json
from uuid import UUID

import pytest

from omnimarket.adapters.wrapper_base import (
    ModelWrapperProgressEvent,
    check_environment,
    collect_args,
    format_output,
    generate_correlation_id,
    handle_error,
    handle_timeout,
    map_args_to_payload,
    stream_progress,
    validate_args,
)


def test_collect_args_accepts_namespace_and_omits_none() -> None:
    args = argparse.Namespace(repo="OmniNode-ai/omnimarket", dry_run=True, limit=None)

    assert collect_args(args) == {
        "repo": "OmniNode-ai/omnimarket",
        "dry_run": True,
    }
    assert collect_args(args, include_none=True)["limit"] is None


def test_collect_args_accepts_mapping() -> None:
    assert collect_args({"repo": "omnimarket", "limit": None}) == {"repo": "omnimarket"}


def test_collect_args_accepts_argv_with_parser() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)

    assert collect_args(["--repo", "OmniNode-ai/omnimarket"], parser=parser) == {
        "repo": "OmniNode-ai/omnimarket"
    }


def test_collect_args_requires_parser_for_argv() -> None:
    with pytest.raises(ValueError, match="parser is required"):
        collect_args(["--repo", "omnimarket"])


def test_validate_args_enforces_required_and_allowed_keys() -> None:
    assert validate_args(
        {"repo": "omnimarket", "dry_run": True},
        required=("repo",),
        allowed=("repo", "dry_run"),
    ) == {"repo": "omnimarket", "dry_run": True}

    with pytest.raises(ValueError, match="Missing required"):
        validate_args({"repo": ""}, required=("repo",))

    with pytest.raises(ValueError, match="Unknown argument"):
        validate_args({"repo": "omnimarket", "extra": True}, allowed=("repo",))


def test_map_args_to_payload_renames_fields_and_omits_none() -> None:
    assert map_args_to_payload(
        {"repo": "omnimarket", "dry_run": True, "limit": None},
        field_map={"repo": "target_repo"},
    ) == {"target_repo": "omnimarket", "dry_run": True}


def test_generate_correlation_id_returns_uuid4() -> None:
    correlation_id = generate_correlation_id()

    assert isinstance(correlation_id, UUID)
    assert correlation_id.version == 4


def test_format_output_handles_models_and_plain_payloads() -> None:
    progress = ModelWrapperProgressEvent(message="running")

    assert json.loads(format_output(progress)) == {
        "event": "progress",
        "message": "running",
        "payload": {},
    }
    assert format_output({"b": 2, "a": 1}) == '{\n  "a": 1,\n  "b": 2\n}'
    assert format_output("already formatted") == "already formatted"


def test_handle_timeout_returns_retryable_structured_error() -> None:
    error = handle_timeout(
        operation="terminal event",
        timeout_ms=30000,
        correlation_id="11111111-1111-4111-8111-111111111111",
    )

    assert error.code == "runtime_timeout"
    assert error.retryable is True
    assert error.details == {
        "operation": "terminal event",
        "timeout_ms": 30000,
        "correlation_id": "11111111-1111-4111-8111-111111111111",
    }


def test_handle_error_returns_structured_error() -> None:
    error = handle_error(
        RuntimeError("runtime unavailable"),
        code="runtime_adapter_error",
        retryable=False,
        details={"command": "merge_sweep"},
    )

    assert error.code == "runtime_adapter_error"
    assert error.message == "runtime unavailable"
    assert error.retryable is False
    assert error.details == {"command": "merge_sweep"}


def test_stream_progress_writes_json_event_to_sink() -> None:
    lines: list[str] = []

    event = stream_progress(
        "dispatching",
        event="dispatch_started",
        payload={"command": "merge_sweep"},
        sink=lines.append,
    )

    assert event.event == "dispatch_started"
    assert event.payload == {"command": "merge_sweep"}
    assert json.loads(lines[0]) == {
        "event": "dispatch_started",
        "message": "dispatching",
        "payload": {"command": "merge_sweep"},
    }


def test_check_environment_reports_missing_required_dependencies() -> None:
    result = check_environment(
        required_env=("ONEX_REQUIRED",),
        optional_env=("ONEX_OPTIONAL",),
        required_commands=("definitely-not-an-omnimarket-command",),
        optional_commands=("python",),
        environ={"ONEX_OPTIONAL": "set"},
    )

    assert result.ok is False
    assert result.missing_required == [
        "ONEX_REQUIRED",
        "definitely-not-an-omnimarket-command",
    ]
    by_name = {check.name: check for check in result.checks}
    assert by_name["ONEX_OPTIONAL"].present is True
    assert by_name["ONEX_OPTIONAL"].value is None
    assert by_name["definitely-not-an-omnimarket-command"].present is False
