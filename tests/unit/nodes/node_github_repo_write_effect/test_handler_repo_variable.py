# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""repo_variable_set and repo_variable_get on a recorded fake transport (OMN-19998).

Each scenario replays a strict, ordered list of exchanges, so the request
shapes are pinned. The planted cases prove the refusals: a name outside the
overlay's entries, a name or a source value carrying a refuse marker, and an
unreadable source send nothing, and a variable already at the declared value is
not written again — a repeat only reads GitHub, since variable operations keep
no in-process record. A json_compact source reads the value out of one key of a
YAML document in the overlays repository and renders it as compact JSON. The
variable names and values here are test-local; the shipped overlay example and
the node's source carry none.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.nodes.node_github_repo_write_effect.handlers.handler_github_repo_write import (
    HandlerGithubRepoWriteEffect,
    planned_requests,
)
from omnimarket.nodes.node_github_repo_write_effect.models import (
    EnumRepoWriteMode,
    EnumRepoWriteOperation,
    EnumRepoWriteRefusal,
    ModelRepoWriteCompleted,
    ModelRepoWriteConfig,
    ModelRepoWriteFailed,
    ModelRepoWriteRequest,
)
from omnimarket.nodes.node_github_repo_write_effect.overlay import (
    ModelPrLabelOverlay,
    ModelRepoVariablesEntry,
    ModelRepoVariablesOverlay,
    ModelRepoWriteOverlay,
)
from omnimarket.nodes.node_github_repo_write_effect.services.repo_variable import (
    create_variable_request,
    get_variable_request,
    read_variable_source,
    read_variable_source_value,
    update_variable_request,
    variable_digest,
)

pytestmark = pytest.mark.unit

_NODE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_github_repo_write_effect"
)
_REPO = "example-org/example-repo"
_OTHER_REPO = "example-org/example-other"
_KEY = "variable-key-0001"
_CORRELATION = UUID("00000000-0000-4000-8000-000000000004")
_NOW = 1_791_000_000.0
_READY = "lab:ready"

_SET = EnumRepoWriteOperation.REPO_VARIABLE_SET
_GET = EnumRepoWriteOperation.REPO_VARIABLE_GET
_VAR_ONE = "LAB_VAR_ONE"
_VAR_TWO = "LAB_VAR_TWO"
_VAR_UNREADABLE = "LAB_VAR_UNREADABLE"
_OFF_LIMITS = "LAB_VAR_OFF_LIMITS"
_MARKED_NAME = "lab_token_var"
_VALUE_ONE = "lab-one-value"
_VALUE_TWO = "lab-two-value"
_STALE = "lab-stale-value"
_DRIFT = "lab-drifted-value"
_MARKED_VALUE = "lab-value-carrying-labsecret-inside"
_NAME_MARKER = "TOKEN"
_VALUE_MARKER = "LABSECRET"

_GET_ONE = get_variable_request(_REPO, _VAR_ONE)
_GET_TWO = get_variable_request(_REPO, _VAR_TWO)
_GET_UNREADABLE = get_variable_request(_REPO, _VAR_UNREADABLE)
_CREATE_ONE = create_variable_request(_REPO, _VAR_ONE, _VALUE_ONE)
_UPDATE_ONE = update_variable_request(_REPO, _VAR_ONE, _VALUE_ONE)
_UPDATE_TWO = update_variable_request(_REPO, _VAR_ONE, _VALUE_TWO)


def _headers() -> dict[str, str]:
    return {
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": "4900",
        "x-ratelimit-used": "100",
        "x-ratelimit-reset": str(int(_NOW) + 3600),
        "x-ratelimit-resource": "core",
    }


def _ok(body: dict[str, Any]) -> ModelGithubHttpResponse:
    return ModelGithubHttpResponse(status=200, headers=_headers(), body=body)


def _created(body: dict[str, Any]) -> ModelGithubHttpResponse:
    return ModelGithubHttpResponse(status=201, headers=_headers(), body=body)


def _not_found() -> ModelGithubHttpResponse:
    return ModelGithubHttpResponse(
        status=404, headers=_headers(), body={"message": "Not Found"}
    )


def _variable_value(name: str, value: str) -> ModelGithubHttpResponse:
    return _ok({"name": name, "value": value})


@dataclass
class _Recorded:
    exchanges: list[tuple[ModelGithubHttpRequest, ModelGithubHttpResponse]]
    sent: list[ModelGithubHttpRequest] = field(default_factory=list)

    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        self.sent.append(request)
        assert self.exchanges, f"no recorded exchange left for {request!r}"
        expected, response = self.exchanges.pop(0)
        assert request == expected, f"sent {request!r}, recorded {expected!r}"
        return response


class _NoClaims:
    def active_claim(self, *, claims_dir: str, pr_key: str, now: str) -> None:
        return None


def _entry(name: str, repository: str = _REPO) -> ModelRepoVariablesEntry:
    return ModelRepoVariablesEntry(
        repository=repository, name=name, source=f"lab/vars/{name}.txt"
    )


def _overlay(
    entries: tuple[ModelRepoVariablesEntry, ...] = (
        _entry(_VAR_ONE),
        _entry(_VAR_TWO),
        _entry(_VAR_UNREADABLE),
    ),
) -> ModelRepoWriteOverlay:
    return ModelRepoWriteOverlay(
        pr_label=ModelPrLabelOverlay(allowed_labels=(_READY,)),
        repo_variables=ModelRepoVariablesOverlay(
            entries=entries,
            refuse_name_markers=(_NAME_MARKER,),
            refuse_value_markers=(_VALUE_MARKER,),
        ),
    )


def _handler(
    transport: _Recorded,
    root: Path,
    overlay: ModelRepoWriteOverlay | None = None,
) -> HandlerGithubRepoWriteEffect:
    return HandlerGithubRepoWriteEffect(
        transport,
        claim_lookup=_NoClaims(),
        runtime_claims_dir="",
        config=ModelRepoWriteConfig(
            github_mode=EnumRepoWriteMode.DRY_RUN,
            github_mode_by_repository={_REPO: EnumRepoWriteMode.ENFORCE},
        ),
        clock=lambda: _NOW,
        overlay=overlay if overlay is not None else _overlay(),
        variable_source_root=root,
    )


def _source(root: Path, name: str, text: str) -> Path:
    path = root / "lab" / "vars" / f"{name}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _repo_variable(
    operation: EnumRepoWriteOperation, name: str, **kw: object
) -> ModelRepoWriteRequest:
    return ModelRepoWriteRequest.model_validate(
        {
            "correlation_id": _CORRELATION,
            "operation": operation,
            "repo": _REPO,
            "idempotency_key": _KEY,
            "requested_by_lane": "author-lane",
            "variable_name": name,
            **kw,
        }
    )


# --- set ----------------------------------------------------------------------


async def test_an_absent_variable_is_created_from_the_declared_source(
    tmp_path: Path,
) -> None:
    _source(tmp_path, _VAR_ONE, f"{_VALUE_ONE}\n")
    transport = _Recorded(
        [(_GET_ONE, _not_found()), (_CREATE_ONE, _created({"name": _VAR_ONE}))]
    )
    result = await _handler(transport, tmp_path).handle(_repo_variable(_SET, _VAR_ONE))
    assert isinstance(result, ModelRepoWriteCompleted)
    assert (result.replayed, result.mode) == (False, EnumRepoWriteMode.ENFORCE)
    assert result.variable_name == _VAR_ONE
    assert result.variable_value is None
    assert (
        result.variable_digest == hashlib.sha256(_VALUE_ONE.encode("utf-8")).hexdigest()
    )
    assert transport.sent == [_GET_ONE, _CREATE_ONE]
    assert _GET_ONE.method == "GET"
    assert _GET_ONE.path == f"/repos/{_REPO}/actions/variables/{_VAR_ONE}"
    assert _CREATE_ONE.method == "POST"
    assert _CREATE_ONE.path == f"/repos/{_REPO}/actions/variables"
    assert _CREATE_ONE.body == {"name": _VAR_ONE, "value": _VALUE_ONE}
    assert not transport.exchanges


def test_the_digest_is_the_sha256_of_the_value() -> None:
    assert (
        variable_digest(_VALUE_ONE)
        == hashlib.sha256(_VALUE_ONE.encode("utf-8")).hexdigest()
    )


async def test_a_present_variable_with_a_different_value_is_patched(
    tmp_path: Path,
) -> None:
    _source(tmp_path, _VAR_ONE, f"{_VALUE_ONE}\n")
    transport = _Recorded(
        [
            (_GET_ONE, _variable_value(_VAR_ONE, _STALE)),
            (_UPDATE_ONE, _variable_value(_VAR_ONE, _VALUE_ONE)),
        ]
    )
    result = await _handler(transport, tmp_path).handle(_repo_variable(_SET, _VAR_ONE))
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.replayed is False
    assert result.variable_name == _VAR_ONE
    assert (
        result.variable_digest == hashlib.sha256(_VALUE_ONE.encode("utf-8")).hexdigest()
    )
    assert transport.sent == [_GET_ONE, _UPDATE_ONE]
    assert _UPDATE_ONE.method == "PATCH"
    assert _UPDATE_ONE.path == f"/repos/{_REPO}/actions/variables/{_VAR_ONE}"
    assert _UPDATE_ONE.body == {"name": _VAR_ONE, "value": _VALUE_ONE}
    assert not transport.exchanges


async def test_a_variable_already_at_the_declared_value_is_not_written_again(
    tmp_path: Path,
) -> None:
    _source(tmp_path, _VAR_ONE, f"{_VALUE_ONE}\n")
    transport = _Recorded([(_GET_ONE, _variable_value(_VAR_ONE, _VALUE_ONE))])
    result = await _handler(transport, tmp_path).handle(_repo_variable(_SET, _VAR_ONE))
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.replayed is True
    assert result.variable_value is None
    assert transport.sent == [_GET_ONE]


async def test_a_repeated_set_of_the_same_key_sends_only_the_get_again(
    tmp_path: Path,
) -> None:
    _source(tmp_path, _VAR_ONE, f"{_VALUE_ONE}\n")
    transport = _Recorded(
        [
            (_GET_ONE, _not_found()),
            (_CREATE_ONE, _created({"name": _VAR_ONE})),
            (_GET_ONE, _variable_value(_VAR_ONE, _VALUE_ONE)),
        ]
    )
    handler = _handler(transport, tmp_path)
    first = await handler.handle(_repo_variable(_SET, _VAR_ONE))
    second = await handler.handle(_repo_variable(_SET, _VAR_ONE))
    assert isinstance(first, ModelRepoWriteCompleted)
    assert isinstance(second, ModelRepoWriteCompleted)
    assert (first.replayed, second.replayed) == (False, True)
    assert transport.sent == [_GET_ONE, _CREATE_ONE, _GET_ONE]
    assert not transport.exchanges


async def test_the_same_key_with_another_variable_sends_its_own_get(
    tmp_path: Path,
) -> None:
    _source(tmp_path, _VAR_ONE, f"{_VALUE_ONE}\n")
    _source(tmp_path, _VAR_TWO, f"{_VALUE_TWO}\n")
    transport = _Recorded(
        [
            (_GET_ONE, _not_found()),
            (_CREATE_ONE, _created({"name": _VAR_ONE})),
            (_GET_TWO, _variable_value(_VAR_TWO, _VALUE_TWO)),
        ]
    )
    handler = _handler(transport, tmp_path)
    first = await handler.handle(_repo_variable(_SET, _VAR_ONE))
    second = await handler.handle(_repo_variable(_SET, _VAR_TWO))
    assert isinstance(first, ModelRepoWriteCompleted)
    assert isinstance(second, ModelRepoWriteCompleted)
    assert second.variable_name == _VAR_TWO
    assert second.replayed is True
    assert transport.sent == [_GET_ONE, _CREATE_ONE, _GET_TWO]


async def test_a_changed_source_rewrites_the_variable_under_the_same_key(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path, _VAR_ONE, f"{_VALUE_ONE}\n")
    transport = _Recorded(
        [
            (_GET_ONE, _not_found()),
            (_CREATE_ONE, _created({"name": _VAR_ONE})),
            (_GET_ONE, _variable_value(_VAR_ONE, _VALUE_ONE)),
            (_UPDATE_TWO, _variable_value(_VAR_ONE, _VALUE_TWO)),
        ]
    )
    handler = _handler(transport, tmp_path)
    first = await handler.handle(_repo_variable(_SET, _VAR_ONE))
    source.write_text(f"{_VALUE_TWO}\n", encoding="utf-8")
    second = await handler.handle(_repo_variable(_SET, _VAR_ONE))
    assert isinstance(first, ModelRepoWriteCompleted)
    assert isinstance(second, ModelRepoWriteCompleted)
    assert second.replayed is False
    assert (
        second.variable_digest == hashlib.sha256(_VALUE_TWO.encode("utf-8")).hexdigest()
    )
    assert transport.sent == [_GET_ONE, _CREATE_ONE, _GET_ONE, _UPDATE_TWO]
    assert _UPDATE_TWO.body == {"name": _VAR_ONE, "value": _VALUE_TWO}
    assert not transport.exchanges


# --- refusals before any call -------------------------------------------------


@pytest.mark.parametrize("mode", [False, True], ids=["enforce", "dry_run"])
async def test_a_name_outside_the_overlay_is_refused_with_no_call(
    tmp_path: Path,
    mode: bool,
) -> None:
    _source(tmp_path, _VAR_ONE, f"{_VALUE_ONE}\n")
    transport = _Recorded([])
    result = await _handler(transport, tmp_path).handle(
        _repo_variable(_SET, _OFF_LIMITS, dry_run=mode)
    )
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.VARIABLE_NOT_ALLOWED
    assert _OFF_LIMITS in result.detail
    assert transport.sent == []


async def test_the_right_name_on_another_repository_is_refused_with_no_call(
    tmp_path: Path,
) -> None:
    _source(tmp_path, _VAR_ONE, f"{_VALUE_ONE}\n")
    transport = _Recorded([])
    result = await _handler(transport, tmp_path).handle(
        _repo_variable(_GET, _VAR_ONE, repo=_OTHER_REPO)
    )
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.VARIABLE_NOT_ALLOWED
    assert transport.sent == []


async def test_a_name_carrying_a_refuse_marker_is_secret_refused(
    tmp_path: Path,
) -> None:
    _source(tmp_path, _VAR_ONE, f"{_VALUE_ONE}\n")
    transport = _Recorded([])
    result = await _handler(transport, tmp_path).handle(
        _repo_variable(_SET, _MARKED_NAME)
    )
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.SECRET_REFUSED
    assert "carries a refuse marker" in result.detail
    assert transport.sent == []


async def test_a_source_value_carrying_a_refuse_marker_is_secret_refused(
    tmp_path: Path,
) -> None:
    _source(tmp_path, _VAR_ONE, f"{_MARKED_VALUE}\n")
    transport = _Recorded([])
    result = await _handler(transport, tmp_path).handle(_repo_variable(_SET, _VAR_ONE))
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.SECRET_REFUSED
    assert _MARKED_VALUE not in result.detail
    assert _VALUE_ONE not in result.detail
    assert transport.sent == []


@pytest.mark.parametrize("mode", [False, True], ids=["enforce", "dry_run"])
async def test_an_unreadable_source_refuses_a_set_with_no_call(
    tmp_path: Path,
    mode: bool,
) -> None:
    transport = _Recorded([])
    result = await _handler(transport, tmp_path).handle(
        _repo_variable(_SET, _VAR_UNREADABLE, dry_run=mode)
    )
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.VARIABLE_SOURCE_UNREADABLE
    assert transport.sent == []


async def test_no_overlay_configured_refuses_a_variable_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    transport = _Recorded([])
    handler = HandlerGithubRepoWriteEffect(
        transport,
        claim_lookup=_NoClaims(),
        runtime_claims_dir="",
        config=ModelRepoWriteConfig(
            github_mode_by_repository={_REPO: EnumRepoWriteMode.ENFORCE}
        ),
        clock=lambda: _NOW,
        variable_source_root=tmp_path,
    )
    result = await handler.handle(_repo_variable(_SET, _VAR_ONE))
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.VARIABLE_NOT_ALLOWED
    assert "no repo_variables overlay is configured" in result.detail
    assert transport.sent == []


async def test_an_overlay_without_repo_variables_refuses_a_variable_write(
    tmp_path: Path,
) -> None:
    overlay = ModelRepoWriteOverlay(
        pr_label=ModelPrLabelOverlay(allowed_labels=(_READY,))
    )
    transport = _Recorded([])
    result = await _handler(transport, tmp_path, overlay).handle(
        _repo_variable(_SET, _VAR_ONE)
    )
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.VARIABLE_NOT_ALLOWED
    assert transport.sent == []


# --- dry_run ------------------------------------------------------------------


@pytest.mark.parametrize("operation", [_SET, _GET], ids=["set", "get"])
async def test_a_dry_run_plans_one_read_and_sends_nothing(
    tmp_path: Path,
    operation: EnumRepoWriteOperation,
) -> None:
    _source(tmp_path, _VAR_ONE, f"{_VALUE_ONE}\n")
    transport = _Recorded([])
    result = await _handler(transport, tmp_path).handle(
        _repo_variable(operation, _VAR_ONE, dry_run=True)
    )
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.mode is EnumRepoWriteMode.DRY_RUN
    assert result.requests == (get_variable_request(_REPO, _VAR_ONE),)
    assert transport.sent == []


def test_the_dry_run_plan_is_one_read_of_the_variable() -> None:
    assert planned_requests(_repo_variable(_SET, _VAR_ONE)) == (_GET_ONE,)
    assert planned_requests(_repo_variable(_GET, _VAR_ONE)) == (_GET_ONE,)


# --- get ----------------------------------------------------------------------


async def test_a_get_reports_the_remote_value_when_it_matches_the_source(
    tmp_path: Path,
) -> None:
    _source(tmp_path, _VAR_ONE, f"{_VALUE_ONE}\n")
    transport = _Recorded([(_GET_ONE, _variable_value(_VAR_ONE, _VALUE_ONE))])
    result = await _handler(transport, tmp_path).handle(_repo_variable(_GET, _VAR_ONE))
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.variable_name == _VAR_ONE
    assert result.variable_value == _VALUE_ONE
    assert result.variable_in_sync is True
    assert (
        result.variable_digest == hashlib.sha256(_VALUE_ONE.encode("utf-8")).hexdigest()
    )
    assert transport.sent == [_GET_ONE]


async def test_a_get_reports_drift_from_the_declared_source(
    tmp_path: Path,
) -> None:
    _source(tmp_path, _VAR_ONE, f"{_VALUE_ONE}\n")
    transport = _Recorded([(_GET_ONE, _variable_value(_VAR_ONE, _DRIFT))])
    result = await _handler(transport, tmp_path).handle(_repo_variable(_GET, _VAR_ONE))
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.variable_value == _DRIFT
    assert result.variable_in_sync is False


async def test_a_get_of_an_absent_variable_is_not_found(tmp_path: Path) -> None:
    _source(tmp_path, _VAR_ONE, f"{_VALUE_ONE}\n")
    transport = _Recorded([(_GET_ONE, _not_found())])
    result = await _handler(transport, tmp_path).handle(_repo_variable(_GET, _VAR_ONE))
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.NOT_FOUND
    assert result.requests == (_GET_ONE,)
    assert transport.sent == [_GET_ONE]


async def test_a_get_of_a_name_outside_the_overlay_is_refused_with_no_call(
    tmp_path: Path,
) -> None:
    transport = _Recorded([])
    result = await _handler(transport, tmp_path).handle(
        _repo_variable(_GET, _OFF_LIMITS)
    )
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.VARIABLE_NOT_ALLOWED
    assert transport.sent == []


async def test_a_get_with_an_unreadable_source_reports_sync_as_none(
    tmp_path: Path,
) -> None:
    transport = _Recorded(
        [(_GET_UNREADABLE, _variable_value(_VAR_UNREADABLE, _VALUE_ONE))]
    )
    result = await _handler(transport, tmp_path).handle(
        _repo_variable(_GET, _VAR_UNREADABLE)
    )
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.variable_value == _VALUE_ONE
    assert result.variable_in_sync is None


# --- path and envelope safety -------------------------------------------------


async def test_no_request_reaches_the_secrets_endpoints(tmp_path: Path) -> None:
    _source(tmp_path, _VAR_ONE, f"{_VALUE_ONE}\n")
    transport = _Recorded(
        [
            (_GET_ONE, _not_found()),
            (_CREATE_ONE, _created({"name": _VAR_ONE})),
            (_GET_ONE, _variable_value(_VAR_ONE, _VALUE_ONE)),
        ]
    )
    handler = _handler(transport, tmp_path)
    set_result = await handler.handle(_repo_variable(_SET, _VAR_ONE))
    get_result = await handler.handle(_repo_variable(_GET, _VAR_ONE))
    assert isinstance(set_result, ModelRepoWriteCompleted)
    assert isinstance(get_result, ModelRepoWriteCompleted)
    sent = [*transport.sent, *set_result.requests, *get_result.requests]
    assert sent
    assert all("/actions/secrets" not in request.path for request in sent)
    assert all("/actions/variables" in request.path for request in sent)


def test_a_repo_variable_request_never_carries_a_value_field() -> None:
    dumped = json.loads(_repo_variable(_SET, _VAR_ONE).model_dump_json())
    assert "value" not in dumped
    with pytest.raises(ValidationError):
        ModelRepoWriteRequest.model_validate({**dumped, "value": "x"})


def test_other_operations_do_not_carry_the_variable_fields() -> None:
    label = ModelRepoWriteRequest.model_validate(
        {
            "correlation_id": _CORRELATION,
            "operation": EnumRepoWriteOperation.PR_LABEL,
            "repo": _REPO,
            "idempotency_key": _KEY,
            "requested_by_lane": "author-lane",
            "pr_number": 4000,
            "expected_head_sha": "4ceedeafeb4040c987883d96c20e745ad5eed6e1",
            "add_labels": [_READY],
        }
    )
    dumped = label.model_dump(mode="json")
    assert "variable_name" not in dumped
    with pytest.raises(ValidationError):
        ModelRepoWriteRequest.model_validate({**dumped, "variable_name": _VAR_ONE})


def test_a_repo_variable_write_names_the_variable() -> None:
    dumped = json.loads(_repo_variable(_SET, _VAR_ONE).model_dump_json())
    with pytest.raises(ValidationError):
        ModelRepoWriteRequest.model_validate({**dumped, "variable_name": None})


@pytest.mark.parametrize(
    "bad",
    [
        "bad name",
        "x" * 101,
        "1starts_digit",
        "has-dash",
    ],
)
def test_a_variable_name_matches_githubs_shape(bad: str) -> None:
    with pytest.raises(ValidationError):
        _repo_variable(_SET, bad)


def test_a_variable_name_may_be_one_hundred_characters() -> None:
    assert _repo_variable(_SET, "y" * 100).variable_name == "y" * 100


# --- the overlay model --------------------------------------------------------


def _variables_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "entries": [
            {
                "repository": _REPO,
                "name": _VAR_ONE,
                "source": f"lab/vars/{_VAR_ONE}.txt",
            },
            {
                "repository": _REPO,
                "name": _VAR_TWO,
                "source": f"lab/vars/{_VAR_TWO}.txt",
            },
        ],
        "refuse_name_markers": [_NAME_MARKER],
        "refuse_value_markers": [_VALUE_MARKER],
    }
    document.update(overrides)
    return document


def test_an_entry_whose_name_carries_a_refuse_marker_refuses_the_load() -> None:
    document = _variables_document(
        entries=[
            {
                "repository": _REPO,
                "name": "EXAMPLE_TOKEN_LIKE",
                "source": "lab/vars/EXAMPLE_TOKEN_LIKE.txt",
            }
        ]
    )
    with pytest.raises(ValidationError):
        ModelRepoVariablesOverlay.model_validate(document)


def test_a_duplicate_repository_and_name_refuses_the_load() -> None:
    document = _variables_document(
        entries=[
            {
                "repository": _REPO,
                "name": _VAR_ONE,
                "source": f"lab/vars/{_VAR_ONE}.txt",
            },
            {
                "repository": _REPO.upper(),
                "name": _VAR_ONE,
                "source": f"lab/vars/{_VAR_ONE}.txt",
            },
        ]
    )
    with pytest.raises(ValidationError):
        ModelRepoVariablesOverlay.model_validate(document)


@pytest.mark.parametrize(
    "source",
    ["../outside.txt", "/lab/vars/absolute.txt", "lab/../../outside.txt"],
)
def test_a_source_outside_the_overlay_root_refuses_the_load(source: str) -> None:
    document = _variables_document(
        entries=[{"repository": _REPO, "name": _VAR_ONE, "source": source}]
    )
    with pytest.raises(ValidationError):
        ModelRepoVariablesOverlay.model_validate(document)


def test_the_shipped_overlay_example_loads() -> None:
    document = yaml.safe_load((_NODE / "overlay.example.yaml").read_text("utf-8"))
    overlay = ModelRepoWriteOverlay.model_validate(document)
    assert overlay.pr_label.allowed_labels
    variables = overlay.repo_variables
    assert variables is not None
    assert variables.entries
    assert variables.refuse_name_markers
    assert variables.refuse_value_markers


# --- read_variable_source -----------------------------------------------------


def test_read_variable_source_refuses_an_escaping_source(tmp_path: Path) -> None:
    for bad in ("../outside.txt", "/lab/vars/absolute.txt", "lab/../../outside.txt"):
        with pytest.raises(ValueError, match="overlay root"):
            read_variable_source(tmp_path, bad, max_chars=100)


def test_read_variable_source_refuses_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing or unreadable"):
        read_variable_source(tmp_path, "lab/vars/absent.txt", max_chars=100)


@pytest.mark.parametrize("text", ["", "\n"])
def test_read_variable_source_refuses_an_empty_source(
    tmp_path: Path,
    text: str,
) -> None:
    (tmp_path / "empty.txt").write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        read_variable_source(tmp_path, "empty.txt", max_chars=100)


def test_read_variable_source_refuses_an_oversize_source(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("x" * 41, encoding="utf-8")
    with pytest.raises(ValueError, match="maximum variable length"):
        read_variable_source(tmp_path, "big.txt", max_chars=40)
    assert read_variable_source(tmp_path, "big.txt", max_chars=41) == "x" * 41


def test_read_variable_source_refuses_content_that_is_not_utf8(
    tmp_path: Path,
) -> None:
    (tmp_path / "binary.txt").write_bytes(b"lab-\xff\xfe-value")
    with pytest.raises(ValueError, match="not valid UTF-8"):
        read_variable_source(tmp_path, "binary.txt", max_chars=100)


def test_read_variable_source_strips_one_trailing_newline(tmp_path: Path) -> None:
    (tmp_path / "one.txt").write_text("lab-value\n", encoding="utf-8")
    assert read_variable_source(tmp_path, "one.txt", max_chars=100) == "lab-value"
    (tmp_path / "two.txt").write_text("lab-value\n\n", encoding="utf-8")
    assert read_variable_source(tmp_path, "two.txt", max_chars=100) == "lab-value\n"


# --- no hard-coded variable name ----------------------------------------------


def test_no_production_variable_name_is_written_in_the_node_source() -> None:
    sources = (
        _NODE / "overlay.py",
        _NODE / "handlers" / "handler_github_repo_write.py",
        _NODE / "services" / "repo_variable.py",
        _NODE / "contract.yaml",
    )
    joined = "\n".join(path.read_text("utf-8") for path in sources)
    assert joined
    for name in ("LAB_PROOF" + "_LANES_JSON", "ONEX_LEDGER" + "_BUS_READ_COMMAND"):
        assert name not in joined


# --- json_compact sources -----------------------------------------------------


_JSON_VAR = "LAB_ITEMS_VAR"
_JSON_SOURCE = f"lab/docs/{_JSON_VAR}.yaml"
_JSON_VALUE = '[{"name":"a-one","port":1},{"name":"b-two","port":2}]'
_JSON_DOCUMENT = (
    "variable: LAB_ITEMS_VAR\n"
    "repository: example-org/example-repo\n"
    "items:\n"
    "  - name: a-one\n"
    "    port: 1\n"
    "  - name: b-two\n"
    "    port: 2\n"
    "    note: null\n"
)
_GET_JSON = get_variable_request(_REPO, _JSON_VAR)
_CREATE_JSON = create_variable_request(_REPO, _JSON_VAR, _JSON_VALUE)


def _json_entry(name: str, repository: str = _REPO) -> ModelRepoVariablesEntry:
    return ModelRepoVariablesEntry(
        repository=repository,
        name=name,
        source=f"lab/docs/{name}.yaml",
        format="json_compact",
        select="items",
    )


def _json_overlay() -> ModelRepoWriteOverlay:
    return _overlay(entries=(_json_entry(_JSON_VAR),))


def _json_document(root: Path, text: str) -> Path:
    path = root / "lab" / "docs" / f"{_JSON_VAR}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _read_json_value(root: Path, *, max_chars: int = 100) -> str:
    return read_variable_source_value(
        root,
        _JSON_SOURCE,
        fmt="json_compact",
        select="items",
        name=_JSON_VAR,
        repository=_REPO,
        max_chars=max_chars,
    )


async def test_a_json_compact_set_creates_the_variable_with_the_compact_json(
    tmp_path: Path,
) -> None:
    _json_document(tmp_path, _JSON_DOCUMENT)
    transport = _Recorded(
        [(_GET_JSON, _not_found()), (_CREATE_JSON, _created({"name": _JSON_VAR}))]
    )
    result = await _handler(transport, tmp_path, _json_overlay()).handle(
        _repo_variable(_SET, _JSON_VAR)
    )
    assert isinstance(result, ModelRepoWriteCompleted)
    assert (result.replayed, result.variable_name) == (False, _JSON_VAR)
    assert result.variable_value is None
    assert (
        result.variable_digest
        == hashlib.sha256(_JSON_VALUE.encode("utf-8")).hexdigest()
    )
    assert _JSON_VALUE == '[{"name":"a-one","port":1},{"name":"b-two","port":2}]'
    assert _CREATE_JSON.body == {"name": _JSON_VAR, "value": _JSON_VALUE}
    assert transport.sent == [_GET_JSON, _CREATE_JSON]
    assert not transport.exchanges


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(
            "variable: LAB_OTHER_VAR\n"
            "repository: example-org/example-repo\n"
            "items:\n"
            "  - name: a-one\n"
            "    port: 1\n",
            id="another-variable",
        ),
        pytest.param(
            "variable: LAB_ITEMS_VAR\n"
            "repository: example-org/example-other\n"
            "items:\n"
            "  - name: a-one\n"
            "    port: 1\n",
            id="another-repository",
        ),
        pytest.param(
            "variable: LAB_ITEMS_VAR\n"
            "repository: example-org/example-repo\n"
            "lanes:\n"
            "  - name: a-one\n",
            id="select-key-missing",
        ),
        pytest.param(
            "- a-one\n- b-two\n",
            id="not-a-mapping",
        ),
    ],
)
async def test_an_unusable_json_compact_document_refuses_a_set_with_no_call(
    tmp_path: Path,
    document: str,
) -> None:
    _json_document(tmp_path, document)
    transport = _Recorded([])
    result = await _handler(transport, tmp_path, _json_overlay()).handle(
        _repo_variable(_SET, _JSON_VAR)
    )
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.VARIABLE_SOURCE_UNREADABLE
    assert "a-one" not in result.detail
    assert transport.sent == []


async def test_a_get_of_the_json_compact_variable_reports_sync(
    tmp_path: Path,
) -> None:
    _json_document(tmp_path, _JSON_DOCUMENT)
    in_sync = _Recorded([(_GET_JSON, _variable_value(_JSON_VAR, _JSON_VALUE))])
    result = await _handler(in_sync, tmp_path, _json_overlay()).handle(
        _repo_variable(_GET, _JSON_VAR)
    )
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.variable_value == _JSON_VALUE
    assert result.variable_in_sync is True
    drifted = _Recorded([(_GET_JSON, _variable_value(_JSON_VAR, _DRIFT))])
    result = await _handler(drifted, tmp_path, _json_overlay()).handle(
        _repo_variable(_GET, _JSON_VAR)
    )
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.variable_value == _DRIFT
    assert result.variable_in_sync is False


def test_read_variable_source_value_text_is_read_variable_source(
    tmp_path: Path,
) -> None:
    (tmp_path / "one.txt").write_text("lab-value\n", encoding="utf-8")
    value = read_variable_source_value(
        tmp_path,
        "one.txt",
        fmt="text",
        select=None,
        name=_VAR_ONE,
        repository=_REPO,
        max_chars=100,
    )
    assert value == read_variable_source(tmp_path, "one.txt", max_chars=100)
    assert value == "lab-value"


def test_read_variable_source_value_refuses_an_escaping_source(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="overlay root"):
        read_variable_source_value(
            tmp_path,
            "../outside.yaml",
            fmt="json_compact",
            select="items",
            name=_JSON_VAR,
            repository=_REPO,
            max_chars=100,
        )


@pytest.mark.parametrize(
    "document", ["items: null\n", 'items: ""\n', "items: []\n", "items: {}\n"]
)
def test_read_variable_source_value_refuses_an_empty_selection(
    tmp_path: Path,
    document: str,
) -> None:
    _json_document(tmp_path, document)
    with pytest.raises(ValueError, match="select key holds no value"):
        _read_json_value(tmp_path)


def test_read_variable_source_value_refuses_an_oversize_rendered_value(
    tmp_path: Path,
) -> None:
    _json_document(tmp_path, "items: [ééé]\n")
    with pytest.raises(ValueError, match="rendered value exceeds"):
        _read_json_value(tmp_path, max_chars=20)
    assert _read_json_value(tmp_path, max_chars=22) == '["\\u00e9\\u00e9\\u00e9"]'


def test_a_json_compact_entry_without_select_refuses_the_load() -> None:
    document = _variables_document(
        entries=[
            {
                "repository": _REPO,
                "name": _VAR_ONE,
                "source": f"lab/docs/{_VAR_ONE}.yaml",
                "format": "json_compact",
            }
        ]
    )
    with pytest.raises(ValidationError):
        ModelRepoVariablesOverlay.model_validate(document)


def test_a_text_entry_with_select_refuses_the_load() -> None:
    document = _variables_document(
        entries=[
            {
                "repository": _REPO,
                "name": _VAR_ONE,
                "source": f"lab/vars/{_VAR_ONE}.txt",
                "select": "items",
            }
        ]
    )
    with pytest.raises(ValidationError):
        ModelRepoVariablesOverlay.model_validate(document)
