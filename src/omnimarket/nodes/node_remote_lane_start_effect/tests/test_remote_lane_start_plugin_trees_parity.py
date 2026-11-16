# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parity of the ssh transport's ``ship_plugin_trees`` step with the frozen runner's plugin store install (OMN-23502).

The step installs each tree the way the omni plugin's ``plugin_ensure_script`` does: a temporary directory renamed
into the store, then touched, then ``PLUGIN-DIR <name> <dir>`` printed. The one difference is deliberate: the runner
fills the temporary directory from ``git archive``, the transport from the archive on stdin. A change to either side's
install lines fails these.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_spi.contracts.services import (
    ModelLaneHostRef,
    ModelLanePluginTree,
    ModelLaneShipPluginTreesRequest,
)

from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_ssh_transport import (
    ModelHostCommandResult,
    SshLaneHostTransport,
)
from omnimarket.nodes.node_remote_lane_start_effect.tests.recorded_runner_steps import (
    RECORDED_PLUGIN_TREE_LINES,
)
from omnimarket.nodes.node_remote_lane_start_effect.tests.test_remote_lane_start_staging_parity import (
    Recorder,
)

pytestmark = pytest.mark.unit

REMOTE = ModelLaneHostRef(name="h-test", target="h-test.example.invalid")
STORE = "$HOME/example-store/plugins"
DIGEST = "0123456789abcdef0123"
ARCHIVE = b"\x1f\x8b not a real archive \xff"


def _transport(runner: Recorder, store: str = STORE) -> SshLaneHostTransport:
    return SshLaneHostTransport(
        ssh_argv=["ssh"],
        host_run_root="/srv/example/runs",
        call_timeout_seconds=30,
        detach_argv=["setsid"],
        plugin_store=store,
        runner=runner,
    )


def _request(tmp_path: Path, *names: str) -> ModelLaneShipPluginTreesRequest:
    trees = []
    for name in names:
        archive = tmp_path / f"{name}.tgz"
        archive.write_bytes(ARCHIVE)
        trees.append(
            ModelLanePluginTree(
                name=name, source_digest=DIGEST, archive_path=str(archive)
            )
        )
    return ModelLaneShipPluginTreesRequest(
        host=REMOTE,
        lane="lane-example",
        run_id="rlane-example-000001",
        trees=tuple(trees),
        timeout_seconds=30,
    )


def _printed(name: str) -> ModelHostCommandResult:
    return ModelHostCommandResult(
        exit_code=0, stdout=f"PLUGIN-DIR {name} {STORE}/{name}-{DIGEST[:12]}\n"
    )


def test_the_install_lines_are_the_recorded_runner_lines(tmp_path: Path) -> None:
    runner = Recorder(_printed("example"))
    done = _transport(runner).ship_plugin_trees(_request(tmp_path, "example"))
    sent = runner.argvs[-1][-1].splitlines()
    for line in RECORDED_PLUGIN_TREE_LINES:
        assert line in sent
    order = [sent.index(line) for line in RECORDED_PLUGIN_TREE_LINES]
    assert order == sorted(order)
    assert done.shipped is True
    assert done.plugin_dirs == (("example", f"{STORE}/example-{DIGEST[:12]}"),)


def test_the_archive_goes_on_stdin_and_is_extracted_into_the_temporary_directory(
    tmp_path: Path,
) -> None:
    runner = Recorder(_printed("example"))
    _transport(runner).ship_plugin_trees(_request(tmp_path, "example"))
    assert runner.stdins == [ARCHIVE]
    assert '  tar -xzf - -C "$T" || exit 70' in runner.argvs[-1][-1].splitlines()


def test_each_tree_is_one_call_in_load_order(tmp_path: Path) -> None:
    runner = Recorder(_printed("first"))
    runner.answer = ModelHostCommandResult(
        exit_code=0, stdout=f"PLUGIN-DIR first {STORE}/first-{DIGEST[:12]}\n"
    )
    done = _transport(runner).ship_plugin_trees(_request(tmp_path, "first", "second"))
    assert len(runner.argvs) == 2
    assert 'D="$HOME/example-store/plugins/first-0123456789ab"' in runner.argvs[0][-1]
    assert 'D="$HOME/example-store/plugins/second-0123456789ab"' in runner.argvs[1][-1]
    assert len(done.plugin_dirs) == 2


def test_a_tree_with_a_lock_file_is_refused_by_name_not_shipped_without_its_venv(
    tmp_path: Path,
) -> None:
    runner = Recorder(
        ModelHostCommandResult(exit_code=21, stdout="PLUGIN-VENV-NOT-RUN example\n")
    )
    done = _transport(runner).ship_plugin_trees(_request(tmp_path, "example"))
    assert done.shipped is False
    assert done.error is not None
    assert "PLUGIN-VENV-NOT-RUN example" in done.error


def test_no_plugin_store_leaves_the_step_to_the_runner_and_sends_nothing(
    tmp_path: Path,
) -> None:
    runner = Recorder(_printed("example"))
    done = _transport(runner, store="").ship_plugin_trees(_request(tmp_path, "example"))
    assert done.shipped is False
    assert done.error is not None
    assert done.error.startswith("ship_plugin_trees: not run by the ssh transport")
    assert runner.argvs == []


@pytest.mark.parametrize("name", ["a b", ".hidden", "x;y", "a/b"])
def test_a_plugin_name_that_is_not_a_plain_name_is_refused_without_a_host_command(
    tmp_path: Path, name: str
) -> None:
    runner = Recorder(_printed("example"))
    request = _request(tmp_path, "example")
    bad = ModelLanePluginTree(
        name=name,
        source_digest=DIGEST,
        archive_path=request.trees[0].archive_path,
    )
    done = _transport(runner).ship_plugin_trees(
        request.model_copy(update={"trees": (bad,)})
    )
    assert done.shipped is False
    assert runner.argvs == []


def test_an_unreadable_archive_is_refused_without_a_host_command(
    tmp_path: Path,
) -> None:
    runner = Recorder(_printed("example"))
    request = _request(tmp_path, "example")
    gone = ModelLanePluginTree(
        name="example",
        source_digest=DIGEST,
        archive_path=str(tmp_path / "missing.tgz"),
    )
    done = _transport(runner).ship_plugin_trees(
        request.model_copy(update={"trees": (gone,)})
    )
    assert done.shipped is False
    assert "archive unreadable" in (done.error or "")
    assert runner.argvs == []


def test_a_plugin_store_with_a_quote_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="plugin_store"):
        _transport(Recorder(ModelHostCommandResult(exit_code=0)), store='x"; rm -rf /')
