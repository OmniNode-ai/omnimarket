# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev guard runs before any secret read or HTTP call, and a missing overlay is a typed failed result.

The contract header promises refusal "before any secret read or HTTP call". These tests hold a real ``secret_store``
block in the overlay and patch the transport and the secret store reader so that constructing either fails the test:
a refusal that built them first would fail here. A missing overlay returns the typed ``overlay_unavailable`` result
instead of raising out of ``handle``, so the runtime emits the handler's own failed event.
"""

from __future__ import annotations

import contextlib
import threading
from pathlib import Path
from typing import Any, NoReturn

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_dev_signed_in_read_effect.handlers import (
    handler_dev_signed_in_read,
)
from omnimarket.nodes.node_dev_signed_in_read_effect.handlers.handler_dev_signed_in_read import (
    HandlerDevSignedInRead,
)
from omnimarket.nodes.node_dev_signed_in_read_effect.models.model_dev_signed_in_read import (
    EnumDevSignedInReadOutcome,
    ModelDevSignedInReadCommand,
)
from omnimarket.nodes.node_dev_signed_in_read_effect.overlay import (
    ModelDevSignedInReadOverlay,
    load_overlay,
)

from ._support import CONTRACT_PATH, overlay_document
from .test_runtime_route import OVERLAY_ROOTS_ENV, SECRET_STORE_BLOCK, _write_overlay

pytestmark = pytest.mark.unit


def _fail_on_construct(monkeypatch: pytest.MonkeyPatch) -> None:
    def constructed(*args: object, **kwargs: object) -> NoReturn:
        pytest.fail("the transport or the secret store was built before the dev guard")

    monkeypatch.setattr(
        handler_dev_signed_in_read, "UrllibIdentityTransport", constructed
    )
    monkeypatch.setattr(
        handler_dev_signed_in_read, "SecretStoreSettingsReader", constructed
    )


def _with_real_store(document: dict[str, Any]) -> None:
    document["secret_store"] = dict(SECRET_STORE_BLOCK)


def test_dev_signed_in_read_guard_refuses_a_foreign_realm_before_building_anything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def foreign_realm(document: dict[str, Any]) -> None:
        _with_real_store(document)
        document["keycloak"]["realm"] = "outside-dev"

    roots = _write_overlay(tmp_path / "roots", foreign_realm)
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(roots))
    _fail_on_construct(monkeypatch)

    import asyncio

    result = asyncio.run(
        HandlerDevSignedInRead().handle(
            ModelDevSignedInReadCommand(lane="dev", write=True)
        )
    )

    assert result.outcome is EnumDevSignedInReadOutcome.REFUSED_NOT_DEV
    assert result.realm == "outside-dev"


async def test_dev_signed_in_read_guard_refuses_a_foreign_command_lane_before_building_anything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    roots = _write_overlay(tmp_path / "roots", _with_real_store)
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(roots))
    _fail_on_construct(monkeypatch)

    result = await HandlerDevSignedInRead().handle(
        ModelDevSignedInReadCommand(lane="prod", write=True)
    )

    assert result.outcome is EnumDevSignedInReadOutcome.REFUSED_NOT_DEV
    assert "command lane" in result.detail


async def test_dev_signed_in_read_guard_refuses_a_foreign_overlay_lane_before_building_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = overlay_document()
    _with_real_store(document)
    document["lane"] = "prod"
    overlay = ModelDevSignedInReadOverlay.model_validate(document)
    assert overlay.secret_store is not None
    _fail_on_construct(monkeypatch)

    result = await HandlerDevSignedInRead(
        overlay=overlay, contract_path=CONTRACT_PATH
    ).handle(ModelDevSignedInReadCommand(lane="dev", write=True))

    assert result.outcome is EnumDevSignedInReadOutcome.REFUSED_NOT_DEV
    assert "overlay lane" in result.detail


async def test_dev_signed_in_read_guard_missing_overlay_returns_a_typed_failed_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path / "missing"))
    _fail_on_construct(monkeypatch)

    # Red on a raise: ``NodeOverlayError`` escaping ``handle`` fails this call.
    result = await HandlerDevSignedInRead().handle(
        ModelDevSignedInReadCommand(lane="dev", write=True)
    )

    assert result.outcome is EnumDevSignedInReadOutcome.OVERLAY_UNAVAILABLE
    assert result.ok is False
    assert result.lane == "dev"
    assert result.realm is None
    assert "overlay" in result.detail


async def test_dev_signed_in_read_failed_result_carries_the_cause_for_the_failed_topic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed outcome carries ``terminal_failure_cause`` so the runtime publishes it on the declared failed topic."""
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path / "missing"))
    _fail_on_construct(monkeypatch)
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))

    result = await HandlerDevSignedInRead().handle(
        ModelDevSignedInReadCommand(lane="dev", write=True)
    )

    assert (
        contract["runtime_dispatch"]["terminal_events"]["failure"]
        == "onex.evt.omnimarket.dev-signed-in-read-failed.v1"
    )
    assert result.terminal_failure_cause == "overlay_unavailable"


async def test_dev_signed_in_read_guard_invalid_overlay_returns_a_typed_failed_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    node_dir = tmp_path / "roots" / "node_dev_signed_in_read_effect"
    node_dir.mkdir(parents=True)
    (node_dir / "overlay.yaml").write_text(
        yaml.safe_dump({"lane": "dev"}), encoding="utf-8"
    )
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path / "roots"))
    _fail_on_construct(monkeypatch)

    result = await HandlerDevSignedInRead().handle(
        ModelDevSignedInReadCommand(lane="dev", write=True)
    )

    assert result.outcome is EnumDevSignedInReadOutcome.OVERLAY_UNAVAILABLE


SECRET_SHAPED = "sk-live-4f9c2a7e91b3d0c85a6e12f7"


def _overlay_file(tmp_path: Path, text: str) -> Path:
    node_dir = tmp_path / "roots" / "node_dev_signed_in_read_effect"
    node_dir.mkdir(parents=True)
    (node_dir / "overlay.yaml").write_text(text, encoding="utf-8")
    return tmp_path / "roots"


async def _handle_with_overlay_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str
) -> Any:
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(_overlay_file(tmp_path, text)))
    _fail_on_construct(monkeypatch)
    return await HandlerDevSignedInRead().handle(
        ModelDevSignedInReadCommand(lane="dev", write=True)
    )


async def test_dev_signed_in_read_guard_detail_holds_no_value_of_a_failed_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A secret-shaped string in a field that fails validation never reaches the failed event's ``detail``."""
    document = overlay_document()
    document["keycloak"]["timeout_seconds"] = (
        SECRET_SHAPED  # not a number: pydantic would print it as input_value
    )
    result = await _handle_with_overlay_text(
        monkeypatch, tmp_path, yaml.safe_dump(document)
    )

    assert result.outcome is EnumDevSignedInReadOutcome.OVERLAY_UNAVAILABLE
    assert SECRET_SHAPED not in result.detail
    assert SECRET_SHAPED not in result.model_dump_json()


async def test_dev_signed_in_read_guard_detail_never_forwards_the_reader_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handler does not depend on the reader's wording: an error text that carries a value is not forwarded."""

    def leaking_reader(*args: object, **kwargs: object) -> NoReturn:
        raise NodeOverlayError(
            f"overlay is invalid: 1 validation error, input_value='{SECRET_SHAPED}'"
        )

    monkeypatch.setattr(handler_dev_signed_in_read, "load_overlay", leaking_reader)
    _fail_on_construct(monkeypatch)

    result = await HandlerDevSignedInRead().handle(
        ModelDevSignedInReadCommand(lane="dev", write=True)
    )

    assert result.outcome is EnumDevSignedInReadOutcome.OVERLAY_UNAVAILABLE
    assert SECRET_SHAPED not in result.detail
    assert "NodeOverlayError" in result.detail


async def test_dev_signed_in_read_guard_malformed_yaml_returns_a_typed_failed_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    text = f"lane: dev\nkeycloak: [unclosed, {SECRET_SHAPED}\n  : : bad\n"
    result = await _handle_with_overlay_text(monkeypatch, tmp_path, text)

    assert result.outcome is EnumDevSignedInReadOutcome.OVERLAY_UNAVAILABLE
    assert result.ok is False
    assert SECRET_SHAPED not in result.detail


def test_dev_signed_in_read_guard_the_reader_turns_malformed_yaml_into_node_overlay_error(
    tmp_path: Path,
) -> None:
    roots = _overlay_file(tmp_path, "lane: dev\nkeycloak: [unclosed\n")
    with pytest.raises(NodeOverlayError) as excinfo:
        load_overlay({OVERLAY_ROOTS_ENV: str(roots)})
    assert "ParserError" in str(
        excinfo.value
    )  # the YAML error is named by type, its text is not carried
    assert excinfo.value.__cause__ is None


def test_dev_signed_in_read_guard_the_reader_turns_an_os_error_into_node_overlay_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    roots = _overlay_file(tmp_path, yaml.safe_dump(overlay_document()))
    real_read_text = Path.read_text

    def refuse(self: Path, *args: Any, **kwargs: Any) -> str:
        if self.name == "overlay.yaml":
            raise PermissionError(13, f"permission denied reading {SECRET_SHAPED}")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", refuse)
    with pytest.raises(NodeOverlayError) as excinfo:
        load_overlay({OVERLAY_ROOTS_ENV: str(roots)})
    assert "PermissionError" in str(excinfo.value)
    assert SECRET_SHAPED not in str(excinfo.value)


async def test_dev_signed_in_read_guard_an_os_error_on_the_overlay_returns_a_typed_failed_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(
        OVERLAY_ROOTS_ENV,
        str(_overlay_file(tmp_path, yaml.safe_dump(overlay_document()))),
    )
    real_read_text = Path.read_text

    def refuse(self: Path, *args: Any, **kwargs: Any) -> str:
        if self.name == "overlay.yaml":
            raise PermissionError(13, "permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", refuse)
    _fail_on_construct(monkeypatch)

    result = await HandlerDevSignedInRead().handle(
        ModelDevSignedInReadCommand(lane="dev", write=True)
    )

    assert result.outcome is EnumDevSignedInReadOutcome.OVERLAY_UNAVAILABLE
    assert result.ok is False


def _count_builds_under_a_race(
    handler: HandlerDevSignedInRead,
    monkeypatch: pytest.MonkeyPatch,
    threads: int = 8,
) -> int:
    """Start ``threads`` first requests together, hold the first build open inside its factory, count the builds."""
    entered = threading.Event()
    release = threading.Event()
    builds: list[object] = []

    def slow_transport() -> object:
        builds.append(object())
        entered.set()
        # Held inside the factory: a build that is not serialized lets the other threads reach here too.
        release.wait(timeout=5)
        return object()

    def store(**_: object) -> object:
        return object()

    monkeypatch.setattr(
        handler_dev_signed_in_read, "UrllibIdentityTransport", slow_transport
    )
    monkeypatch.setattr(handler_dev_signed_in_read, "SecretStoreSettingsReader", store)
    overlay = ModelDevSignedInReadOverlay.model_validate(overlay_document())
    workers = [
        threading.Thread(target=handler._resolve_io, args=(overlay,))
        for _ in range(threads)
    ]
    for worker in workers:
        worker.start()
    assert entered.wait(timeout=5)
    # Give the other threads time to pile up behind the first build (or, with no lock, to enter their own).
    for worker in workers[1:]:
        worker.join(timeout=0.2)
    release.set()
    for worker in workers:
        worker.join(timeout=5)
    assert not any(worker.is_alive() for worker in workers)
    return len(builds)


def test_dev_signed_in_read_guard_first_resolution_builds_once_under_concurrent_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay = ModelDevSignedInReadOverlay.model_validate(overlay_document())
    handler = HandlerDevSignedInRead(overlay=overlay)
    assert _count_builds_under_a_race(handler, monkeypatch) == 1


def test_dev_signed_in_read_guard_the_lock_test_fails_without_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: with the lock removed the same race builds more than once, so the test above can fail."""
    overlay = ModelDevSignedInReadOverlay.model_validate(overlay_document())
    handler = HandlerDevSignedInRead(overlay=overlay)
    monkeypatch.setattr(handler, "_lock", contextlib.nullcontext())
    assert _count_builds_under_a_race(handler, monkeypatch) > 1
