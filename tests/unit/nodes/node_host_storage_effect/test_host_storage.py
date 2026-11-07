# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host storage effect (OMN-22906): one declared command, free bytes before and after, both backends."""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_host_storage_effect.handlers.backend_in_memory import (
    BackendHostStorageInMemory,
)
from omnimarket.nodes.node_host_storage_effect.handlers.handler_host_storage import (
    HandlerHostStorage,
    select_backend,
)
from omnimarket.nodes.node_host_storage_effect.models.model_host_storage import (
    EnumHostStorageBackend,
    EnumHostStorageOutcome,
    EnumHostStorageRefusal,
    ModelHostStorageRequest,
    ModelHostStorageResult,
)
from omnimarket.nodes.node_host_storage_effect.models.model_host_storage_overlay import (
    NODE_NAME,
    ModelHostStorageOverlay,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_host_storage_effect"
)
CAPACITY = 1_000_000
BIG = 400_000
HOST = "store-host"

# A deterministic "df": free bytes are the capacity less the size of the files in the path.
FREE_SCRIPT = (
    "import os,sys;"
    f"print({CAPACITY}-sum(os.path.getsize(os.path.join(sys.argv[1],f)) for f in os.listdir(sys.argv[1])))"
)
DELETE_BIG = "import os,sys;os.remove(os.path.join(sys.argv[1],'big.bin'))"


def overlay_doc(backend: str, work: str) -> dict[str, object]:
    sim = backend == "in_memory"
    return {
        "backend": backend,
        "free_bytes_argv": [sys.executable, "-c", FREE_SCRIPT, "{path}"],
        "free_bytes_timeout_seconds": 10,
        "commands": [
            {
                "name": "prune_big",
                "argv": [sys.executable, "-c", DELETE_BIG, work],
                "measured_path": work,
                "timeout_seconds": 10,
                "simulated_effect_bytes": BIG if sim else 0,
            },
            {
                "name": "always_fails",
                "argv": [sys.executable, "-c", "import sys;sys.exit(3)"],
                "measured_path": work,
                "timeout_seconds": 10,
            },
            {
                "name": "ghost_path",
                "argv": [sys.executable, "-c", DELETE_BIG, work],
                "measured_path": work + "/does-not-exist",
                "timeout_seconds": 10,
            },
            {
                "name": "unlisted",
                "argv": [sys.executable, "-c", "pass"],
                "measured_path": work,
                "timeout_seconds": 10,
            },
        ],
        "hosts": [
            {
                "name": HOST,
                "transport_prefix": [],
                "commands": ["prune_big", "always_fails", "ghost_path"],
                "simulated_free_bytes": CAPACITY - BIG,
            }
        ],
    }


@dataclass
class Rig:
    handler: HandlerHostStorage
    kind: str
    work: Path
    # True while the "big.bin" the prune command removes is still present.
    big_present: Callable[[], bool]
    # Make the named command exit non-zero (the subprocess one already does).
    break_command: Callable[[str], None]
    # Make the host's free bytes unreadable.
    make_unreadable: Callable[[], None]


def make_rig(kind: str, tmp_path: Path) -> Rig:
    work = tmp_path / "work"
    work.mkdir()
    (work / "big.bin").write_bytes(b"x" * BIG)
    overlay = ModelHostStorageOverlay.model_validate(overlay_doc(kind, str(work)))
    backend = select_backend(overlay)
    if isinstance(backend, BackendHostStorageInMemory):
        mem = backend
        return Rig(
            HandlerHostStorage(overlay, backend),
            kind,
            work,
            lambda: (HOST, "prune_big") not in mem.ran,
            lambda name: mem.failing.add(name),
            lambda: mem.unreadable.add(HOST),
        )

    def unreadable() -> None:
        # A real unreadable reading: the measured path of every command stops existing.
        (work / "big.bin").unlink()
        work.rmdir()

    return Rig(
        HandlerHostStorage(overlay, backend),
        kind,
        work,
        lambda: (work / "big.bin").exists(),
        lambda _name: None,
        unreadable,
    )


@pytest.fixture(params=["in_memory", "subprocess"])
def rig(request: pytest.FixtureRequest, tmp_path: Path) -> Rig:
    return make_rig(request.param, tmp_path)


def ask(command: str, host: str = HOST) -> ModelHostStorageRequest:
    return ModelHostStorageRequest(host=host, command=command, requested_by="lane-a")


def assert_free_bytes_reading(result: ModelHostStorageResult) -> None:
    """The AC2 reading: the typed result carries the free bytes before and after, and what was freed."""
    assert result.outcome is EnumHostStorageOutcome.OK
    assert result.free_bytes_before == CAPACITY - BIG
    assert result.free_bytes_after == CAPACITY
    assert result.freed_bytes == BIG
    assert result.exit_code == 0


# -- AC2 ------------------------------------------------------------------------------------


def test_host_storage_runs_a_declared_command_and_reads_free_bytes_before_and_after(
    rig: Rig,
) -> None:
    assert_free_bytes_reading(rig.handler.handle(ask("prune_big")))
    if rig.kind == "subprocess":
        assert not rig.big_present()


def test_host_storage_reading_has_teeth_when_the_after_reading_is_not_taken(
    rig: Rig,
) -> None:
    """Injected break: a handler that never reads the free bytes after. The reading must fail."""

    class SkipsAfterReading(HandlerHostStorage):
        def handle(self, request: ModelHostStorageRequest) -> ModelHostStorageResult:
            done = super().handle(request)
            return done.model_copy(update={"free_bytes_after": done.free_bytes_before})

    broken = SkipsAfterReading(rig.handler._overlay, rig.handler._backend)
    with pytest.raises(AssertionError):
        assert_free_bytes_reading(broken.handle(ask("prune_big")))


def test_host_storage_a_failing_command_is_a_typed_failure_with_both_readings(
    rig: Rig,
) -> None:
    rig.break_command("always_fails")
    result = rig.handler.handle(ask("always_fails"))
    assert result.outcome is EnumHostStorageOutcome.FAILED
    assert result.exit_code not in (0, None)
    assert result.free_bytes_before == result.free_bytes_after == CAPACITY - BIG
    assert result.freed_bytes == 0


def test_host_storage_runs_nothing_when_free_bytes_before_cannot_be_read(
    rig: Rig,
) -> None:
    if rig.kind == "subprocess":
        result = rig.handler.handle(ask("ghost_path"))
        assert rig.big_present()
    else:
        rig.make_unreadable()
        result = rig.handler.handle(ask("prune_big"))
        assert rig.handler._backend.ran == []  # type: ignore[attr-defined]
    assert result.outcome is EnumHostStorageOutcome.REFUSED
    assert result.refusal is EnumHostStorageRefusal.FREE_BYTES_UNREADABLE
    assert result.free_bytes_before is None


def test_host_storage_refuses_what_the_overlay_does_not_declare(rig: Rig) -> None:
    unknown_host = rig.handler.handle(ask("prune_big", "other-host"))
    assert unknown_host.refusal is EnumHostStorageRefusal.HOST_UNKNOWN
    unknown_cmd = rig.handler.handle(ask("rm_everything"))
    assert unknown_cmd.refusal is EnumHostStorageRefusal.COMMAND_UNKNOWN
    unlisted = rig.handler.handle(ask("unlisted"))
    assert unlisted.refusal is EnumHostStorageRefusal.COMMAND_NOT_DECLARED_FOR_HOST
    for result in (unknown_host, unknown_cmd, unlisted):
        assert result.outcome is EnumHostStorageOutcome.REFUSED
        assert result.free_bytes_before is None
    if rig.kind == "subprocess":
        assert rig.big_present()


def test_host_storage_subprocess_timeout_is_a_typed_failure(tmp_path: Path) -> None:
    doc = overlay_doc("subprocess", str(tmp_path))
    doc["commands"][0].update(  # type: ignore[index, union-attr]
        {
            "argv": [sys.executable, "-c", "import time;time.sleep(30)"],
            "timeout_seconds": 0.3,
        }
    )
    (tmp_path / "big.bin").write_bytes(b"x" * BIG)
    overlay = ModelHostStorageOverlay.model_validate(doc)
    result = HandlerHostStorage(overlay, select_backend(overlay)).handle(
        ask("prune_big")
    )
    assert result.outcome is EnumHostStorageOutcome.FAILED
    assert result.timed_out is True
    assert result.detail == "timed out"


def test_host_storage_subprocess_puts_the_transport_prefix_before_every_argv(
    tmp_path: Path,
) -> None:
    """The prefix wraps the command and the free-bytes reading: a prefix that cannot start refuses."""
    doc = overlay_doc("subprocess", str(tmp_path))
    doc["hosts"][0]["transport_prefix"] = ["/nonexistent-transport-binary"]  # type: ignore[index]
    overlay = ModelHostStorageOverlay.model_validate(doc)
    (tmp_path / "big.bin").write_bytes(b"x" * BIG)
    result = HandlerHostStorage(overlay, select_backend(overlay)).handle(
        ask("prune_big")
    )
    assert result.refusal is EnumHostStorageRefusal.FREE_BYTES_UNREADABLE
    assert (tmp_path / "big.bin").exists()


# -- overlay --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "profile", ["overlays/in_memory.yaml", "overlays/subprocess.example.yaml"]
)
def test_host_storage_both_shipped_profiles_load(profile: str) -> None:
    overlay = load_structured_overlay(
        NODE_NAME,
        ModelHostStorageOverlay,
        explicit=yaml.safe_load((NODE_DIR / profile).read_text(encoding="utf-8")),
    )
    assert overlay.backend in set(EnumHostStorageBackend)


def test_host_storage_in_memory_profile_runs_its_declared_command() -> None:
    overlay = ModelHostStorageOverlay.model_validate(
        yaml.safe_load(
            (NODE_DIR / "overlays/in_memory.yaml").read_text(encoding="utf-8")
        )
    )
    result = HandlerHostStorage(overlay, select_backend(overlay)).handle(
        ask("prune_temp", "sim-host-a")
    )
    assert (result.free_bytes_before, result.free_bytes_after) == (20000, 25000)


def test_host_storage_overlay_is_found_under_the_overlay_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / NODE_NAME
    target.mkdir()
    (target / "overlay.yaml").write_text(
        (NODE_DIR / "overlays/in_memory.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    assert (
        load_structured_overlay(NODE_NAME, ModelHostStorageOverlay).backend
        is EnumHostStorageBackend.IN_MEMORY
    )


def test_host_storage_overlay_refuses_inconsistent_documents(tmp_path: Path) -> None:
    work = str(tmp_path)
    doc = overlay_doc("in_memory", work)
    doc["free_bytes_argv"] = ["df"]
    with pytest.raises(ValidationError, match="placeholder"):
        ModelHostStorageOverlay.model_validate(doc)
    doc = overlay_doc("in_memory", work)
    doc["hosts"][0]["commands"] = ["not_declared"]  # type: ignore[index]
    with pytest.raises(ValidationError, match="undeclared"):
        ModelHostStorageOverlay.model_validate(doc)
    doc = overlay_doc("in_memory", work)
    del doc["free_bytes_timeout_seconds"]
    with pytest.raises(ValidationError):
        ModelHostStorageOverlay.model_validate(doc)
