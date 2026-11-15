# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local facts reader reads through its seams and never fills a zero (OMN-19998)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from omnimarket.models.model_host_introspection_announcement import EnumSoftwareKind
from omnimarket.models.model_host_introspection_request import (
    ModelHostCloneTarget,
    ModelHostReadTargets,
)
from omnimarket.nodes.node_host_introspection_announce_effect.adapters.local_host_facts_reader import (
    NOT_FOUND_PREFIX,
    LocalHostFactsReader,
)
from omnimarket.nodes.node_host_introspection_announce_effect.protocols import (
    HostFactsUnreadableError,
)
from tests.host_introspection_support import HOST, install_overlay

pytestmark = pytest.mark.unit

MIB = 1024 * 1024


class Machine:
    """A fake machine: argv -> stdout, or the error that command raises."""

    def __init__(self, answers: Mapping[str, str | Exception]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str], timeout: float) -> str:
        self.calls.append(tuple(argv))
        answer = self.answers.get(
            argv[0], HostFactsUnreadableError(f"{NOT_FOUND_PREFIX} {argv[0]}")
        )
        if isinstance(answer, Exception):
            raise answer
        return answer


def _reader(machine: Machine, http: object | None = None) -> LocalHostFactsReader:
    return LocalHostFactsReader(
        run=machine.run,
        http_get_json=http or (lambda *_args: {"data": [{"id": "model-x"}]}),  # type: ignore[arg-type]
        system=lambda: ("Linux", "x86_64", "6.8.0"),
        hostnames=lambda: (HOST,),
        boot_id=lambda: "boot-1",
    )


FULL = {
    "python3": "Python 3.12.3\n",
    "docker": "Docker version 27.1.1, build abc\n",
    "claude": "2.1.0 (Claude Code)\n",
    "uv": "uv 0.9.0 (abc 2026-01-01)\n",
    "nvidia-smi": "gpu-a, 24576, 1024\ngpu-b, 24576, 0\n",
    "git": "0123456789abcdef0123456789abcdef01234567\n",
}


def test_golden_every_fact_the_overlay_asks_for_is_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay = install_overlay(monkeypatch, tmp_path)
    machine = Machine({**FULL, "python3": "Python 3.12.3\n"})
    targets = ModelHostReadTargets(
        host_name=HOST,
        model_port=8000,
        clones=(ModelHostCloneTarget(name="omnimarket", path="/srv/x/omnimarket"),),
    )
    facts = _reader(machine).read(overlay, targets)
    assert {r.name: r.version for r in facts.runtimes} == {
        "python": "3.12.3",
        "docker": "27.1.1",
        "claude": "2.1.0",
        "uv": "0.9.0",
    }
    assert [(g.name, g.mem_total_bytes, g.mem_used_bytes) for g in facts.gpus] == [
        ("gpu-a", 24576 * MIB, 1024 * MIB),
        ("gpu-b", 24576 * MIB, 0),
    ]
    assert [(m.name, m.port, m.endpoint) for m in facts.models_served] == [
        ("model-x", 8000, f"http://{HOST}:8000")
    ]
    clone = [s for s in facts.software if s.kind is EnumSoftwareKind.CLONE]
    assert [(s.name, s.commit) for s in clone] == [
        ("omnimarket", "0123456789abcdef0123456789abcdef01234567")
    ]
    assert ("git", "-C", "/srv/x/omnimarket", "rev-parse", "HEAD") in machine.calls
    assert facts.identity.hostnames == (HOST,)


def test_a_runtime_that_is_not_installed_is_left_out_not_reported_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay = install_overlay(monkeypatch, tmp_path)
    machine = Machine({k: v for k, v in FULL.items() if k != "docker"})
    facts = _reader(machine).read(overlay, ModelHostReadTargets(host_name=HOST))
    assert "docker" not in {r.name for r in facts.runtimes}
    assert "python" in {r.name for r in facts.runtimes}


def test_a_host_with_no_gpu_tool_has_no_gpus(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay = install_overlay(monkeypatch, tmp_path)
    machine = Machine({k: v for k, v in FULL.items() if k != "nvidia-smi"})
    facts = _reader(machine).read(overlay, ModelHostReadTargets(host_name=HOST))
    assert facts.gpus == ()


def test_error_a_gpu_line_that_does_not_parse_is_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay = install_overlay(monkeypatch, tmp_path)
    machine = Machine({**FULL, "nvidia-smi": "gpu-a, many, 10\n"})
    with pytest.raises(HostFactsUnreadableError, match="gpu line does not parse"):
        _reader(machine).read(overlay, ModelHostReadTargets(host_name=HOST))


def test_error_a_probe_that_fails_for_another_reason_is_unreadable_not_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay = install_overlay(monkeypatch, tmp_path)
    machine = Machine({**FULL, "python3": HostFactsUnreadableError("python3 exited 1")})
    with pytest.raises(HostFactsUnreadableError, match="python3 exited 1"):
        _reader(machine).read(overlay, ModelHostReadTargets(host_name=HOST))


def test_error_a_model_list_without_data_is_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay = install_overlay(monkeypatch, tmp_path)
    reader = _reader(Machine(FULL), http=lambda *_args: {"models": []})
    with pytest.raises(HostFactsUnreadableError, match="no data list"):
        reader.read(overlay, ModelHostReadTargets(host_name=HOST, model_port=8000))


def test_a_clone_whose_commit_cannot_be_read_is_reported_with_no_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay = install_overlay(monkeypatch, tmp_path)
    machine = Machine({**FULL, "git": HostFactsUnreadableError("git exited 128")})
    targets = ModelHostReadTargets(
        host_name=HOST, clones=(ModelHostCloneTarget(name="gone", path="/nowhere"),)
    )
    facts = _reader(machine).read(overlay, targets)
    assert [
        (s.name, s.commit) for s in facts.software if s.kind is EnumSoftwareKind.CLONE
    ] == [("gone", None)]


def test_a_software_probe_that_fails_leaves_its_cell_unread_and_keeps_the_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay = install_overlay(monkeypatch, tmp_path)
    interpreter = overlay.software[0].version.argv[0]  # type: ignore[union-attr]
    machine = Machine({**FULL, interpreter: HostFactsUnreadableError("exited 1")})
    facts = _reader(machine).read(overlay, ModelHostReadTargets(host_name=HOST))
    rows = [s for s in facts.software if s.kind is EnumSoftwareKind.PACKAGE]
    assert [(s.name, s.version) for s in rows] == [("omnimarket", None)]
