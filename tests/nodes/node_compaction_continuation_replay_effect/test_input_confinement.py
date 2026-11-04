# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command-payload paths stay inside the configured roots: an outside path is refused, unread."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_compaction_continuation_replay_effect.container.container_replay import (
    create_replay_container,
)
from omnimarket.nodes.node_compaction_continuation_replay_effect.handlers import (
    handler_continuation_replay as replay_module,
)
from omnimarket.nodes.node_compaction_continuation_replay_effect.handlers.confinement import (
    PathConfinementError,
)
from omnimarket.nodes.node_compaction_continuation_replay_effect.handlers.handler_continuation_replay import (
    HandlerContinuationReplay,
)
from omnimarket.nodes.node_compaction_continuation_replay_effect.handlers.handler_experiment import (
    HandlerReplayExperiment,
)
from omnimarket.nodes.node_compaction_continuation_replay_effect.models import (
    ModelReplayRequest,
)

from .test_experiment import ModelFixture, request

pytestmark = pytest.mark.unit

SECRET = "SECRET_OUTSIDE_ROOT=hunter2"


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    """An allowed root and a sibling directory outside it that holds a secret."""
    inside = tmp_path / "inside"
    outside = tmp_path / "outside"
    inside.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text(SECRET)
    return inside, outside


def _replay_request(
    inside: Path, ledger_source: str | None = None, worktree: str | None = None
) -> ModelReplayRequest:
    return ModelReplayRequest(
        correlation_id="c1",
        boundary_id="b1",
        arm="stock",
        repeat_index=0,
        n_turns=1,
        scratch_dir=str(inside / "scratch"),
        ledger_source=ledger_source,
        scratch_worktree=worktree,
    )


class _ReadSpy:
    """Fails the test if the replay handler copies, digests or diffs an outside path."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.touched: list[str] = []
        real_copy = replay_module.shutil.copyfile
        real_digest = replay_module.file_digest
        real_diff = replay_module.collect_diff

        def copy(src: Any, dst: Any) -> Any:
            self.touched.append(str(src))
            return real_copy(src, dst)

        def digest(path: str) -> Any:
            self.touched.append(str(path))
            return real_digest(path)

        def diff(worktree: str) -> Any:
            self.touched.append(str(worktree))
            return real_diff(worktree)

        monkeypatch.setattr(replay_module.shutil, "copyfile", copy)
        monkeypatch.setattr(replay_module, "file_digest", digest)
        monkeypatch.setattr(replay_module, "collect_diff", diff)


@pytest.mark.parametrize("hostile", ["/proc/self/environ", "/etc/passwd"])
def test_replay_refuses_ledger_source_outside_roots_with_no_read(
    roots: tuple[Path, Path], hostile: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    inside, _ = roots
    spy = _ReadSpy(monkeypatch)
    with pytest.raises(PathConfinementError, match="ledger_source"):
        HandlerContinuationReplay(allowed_roots=(inside,)).handle(
            _replay_request(inside, ledger_source=hostile)
        )
    assert spy.touched == []
    assert not (inside / "scratch").exists()


def test_replay_refuses_dot_dot_path_with_no_read(
    roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    inside, _ = roots
    spy = _ReadSpy(monkeypatch)
    traversal = str(inside / ".." / "outside" / "secret.txt")
    with pytest.raises(PathConfinementError, match="ledger_source"):
        HandlerContinuationReplay(allowed_roots=(inside,)).handle(
            _replay_request(inside, ledger_source=traversal)
        )
    # A `..` spelling is refused even when it lands back inside the root.
    inside_traversal = str(inside / "sub" / ".." / "ledger.md")
    with pytest.raises(PathConfinementError, match=r"'\.\.'"):
        HandlerContinuationReplay(allowed_roots=(inside,)).handle(
            _replay_request(inside, ledger_source=inside_traversal)
        )
    assert spy.touched == []


def test_replay_refuses_symlink_that_leaves_the_root(
    roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    inside, outside = roots
    link = inside / "ledger.md"
    link.symlink_to(outside / "secret.txt")
    spy = _ReadSpy(monkeypatch)
    with pytest.raises(PathConfinementError, match="ledger_source"):
        HandlerContinuationReplay(allowed_roots=(inside,)).handle(
            _replay_request(inside, ledger_source=str(link))
        )
    assert spy.touched == []


def test_replay_refuses_scratch_worktree_outside_roots_with_no_diff(
    roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    inside, outside = roots
    spy = _ReadSpy(monkeypatch)
    with pytest.raises(PathConfinementError, match="scratch_worktree"):
        HandlerContinuationReplay(allowed_roots=(inside,)).handle(
            _replay_request(inside, worktree=str(outside))
        )
    assert spy.touched == []


def test_replay_with_no_configured_roots_refuses_every_path(
    roots: tuple[Path, Path],
) -> None:
    inside, _ = roots
    real = inside / "ledger.md"
    real.write_text("2026-11-01T00:00:00Z | CLAIM | lane=a | ticket=OMN-1 | seed\n")
    with pytest.raises(PathConfinementError, match="ledger_source"):
        HandlerContinuationReplay().handle(
            _replay_request(inside, ledger_source=str(real))
        )


def test_replay_reads_an_in_root_ledger_source(roots: tuple[Path, Path]) -> None:
    """Positive control: the refusals above are the confinement, not a broken fixture."""
    inside, _ = roots
    real = inside / "ledger.md"
    row = "2026-11-01T00:00:00Z | CLAIM | lane=a | ticket=OMN-1 | seed"
    real.write_text(row + "\n")
    result = HandlerContinuationReplay(allowed_roots=(inside,)).handle(
        _replay_request(inside, ledger_source=str(real))
    )
    assert result.scratch_rows == [row]


def _container(inside: Path):
    return create_replay_container(
        model=ModelFixture(), scratch_root=inside / "scratch", input_roots=(inside,)
    )


class _Opened:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.paths: list[str] = []
        real = Path.read_bytes

        def read_bytes(path: Path) -> bytes:
            self.paths.append(str(path))
            return real(path)

        monkeypatch.setattr(Path, "read_bytes", read_bytes)


@pytest.mark.parametrize("hostile", ["/proc/self/environ", "outside/secret.txt"])
def test_experiment_refuses_ledger_source_outside_roots_before_any_model_call(
    roots: tuple[Path, Path], hostile: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    inside, outside = roots
    source = hostile if hostile.startswith("/") else str(outside / "secret.txt")
    base = request(inside)
    req = base.model_copy(
        update={
            "contexts": [
                c.model_copy(update={"ledger_source": source}) for c in base.contexts
            ]
        }
    )
    model = ModelFixture()
    opened = _Opened(monkeypatch)
    container = create_replay_container(
        model=model, scratch_root=inside / "scratch", input_roots=(inside,)
    )
    with pytest.raises(PathConfinementError, match="ledger_source") as raised:
        HandlerReplayExperiment(container).handle(req)
    assert model.inputs == []
    assert source not in opened.paths
    assert SECRET not in str(raised.value)


def test_experiment_refuses_tolerance_file_outside_roots_unread(
    roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    inside, outside = roots
    tolerance = outside / "tolerance.yaml"
    tolerance.write_text("jaccard_tolerance: 0.8\nmin_repeats: 3\n")
    req = request(inside).model_copy(update={"tolerance_file": str(tolerance)})
    opened = _Opened(monkeypatch)
    with pytest.raises(PathConfinementError, match="tolerance_file"):
        HandlerReplayExperiment(_container(inside)).handle(req)
    assert str(tolerance) not in opened.paths
    assert str(tolerance.resolve()) not in opened.paths


def test_experiment_refuses_scratch_worktree_outside_roots(
    roots: tuple[Path, Path],
) -> None:
    inside, outside = roots
    base = request(inside)
    req = base.model_copy(
        update={
            "contexts": [
                c.model_copy(update={"scratch_worktree": str(outside)})
                for c in base.contexts
            ]
        }
    )
    model = ModelFixture()
    container = create_replay_container(
        model=model, scratch_root=inside / "scratch", input_roots=(inside,)
    )
    with pytest.raises(PathConfinementError, match="scratch_worktree"):
        HandlerReplayExperiment(container).handle(req)
    assert model.inputs == []


def test_experiment_with_in_root_paths_still_completes(
    roots: tuple[Path, Path],
) -> None:
    """Positive control for the experiment refusals above."""
    inside, _ = roots
    result = HandlerReplayExperiment(_container(inside)).handle(request(inside))
    assert len(result.repeats) == 12
