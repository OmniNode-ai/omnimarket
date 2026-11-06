# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay import crashes that exhausted a lane before it could read a CI log."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from omnimarket.nodes.node_lane_github_budget_effect import NodeLaneGithubBudgetEffect
from omnimarket.nodes.node_lane_github_budget_effect.models.model_lane_github_budget import (
    ModelLaneGithubCall,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def lane(tmp_path):
    tool = tmp_path / "onex"
    tool.write_text(
        f"#!/bin/sh\nexec {shlex.quote(sys.executable)} "
        f'{shlex.quote(str(tmp_path / "entry.py"))} "$@"\n'
    )
    tool.chmod(0o755)
    policy = {
        "lane": "recorded-import-crash",
        "budget": 8,
        "onex": str(tool),
        "log": str(tmp_path / "calls.log"),
        "spent": str(tmp_path / "spent"),
        "cache": str(tmp_path / "cache"),
    }
    return policy, tmp_path / "entry.py", tmp_path / "network"


def invoke(lane, *extra):
    policy, _, network = lane
    return NodeLaneGithubBudgetEffect().handle(
        ModelLaneGithubCall(
            policy,
            "onex",
            ("scm", "job-log", "o/r", "--job-id", "77", *extra),
            {"PATH": os.environ["PATH"], "TEST_NETWORK": str(network)},
        )
    )


def spent(lane):
    path = Path(lane[0]["spent"])
    return int(path.read_text()) if path.exists() else 0


def dispatched_program(*, code=0, receipt='{"command_may_have_run":true}'):
    return (
        "import os, sys\nfrom pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "if '--dispatch-receipt' in args:\n"
        "    marker = Path(args[args.index('--dispatch-receipt') + 1])\n"
        f"    marker.write_text({receipt!r})\n"
        "with Path(os.environ['TEST_NETWORK']).open('a') as stream:\n"
        "    stream.write('GET job log\\n')\n"
        "print('CI failure line')\n"
        f"raise SystemExit({code})\n"
    )


def test_eight_recorded_import_crashes_leave_the_log_read_available(lane):
    policy, entry, network = lane
    # Recorded incident: the onex entrypoint could not import its core dependency.
    entry.write_text("raise ModuleNotFoundError(\"No module named 'omnibase_core'\")\n")
    for _ in range(8):
        result = invoke(lane)
        assert result.code == 1
        assert "ModuleNotFoundError: No module named 'omnibase_core'" in result.stderr
    assert spent(lane) == 0
    assert not network.exists()

    entry.write_text(dispatched_program())
    result = invoke(lane)
    assert result.code == 0
    assert result.stdout == "CI failure line\n"
    assert spent(lane) == 1
    assert invoke(lane).stdout == result.stdout
    assert spent(lane) == 1
    assert network.read_text().splitlines() == ["GET job log"]
    actions = [
        row.split("\t")[-1] for row in Path(policy["log"]).read_text().splitlines()
    ]
    assert actions == [*(["pre-dispatch"] * 8), "allowed", "cache"]


@pytest.mark.parametrize("code", [2, 69, 75])
def test_local_failure_before_dispatch_preserves_the_budget(lane, code):
    lane[1].write_text(f"raise SystemExit({code})\n")
    assert invoke(lane).code == code
    assert spent(lane) == 0
    assert not lane[2].exists()


@pytest.mark.parametrize("code", [1, 69, 75])
def test_failure_after_dispatch_keeps_the_charge_and_is_never_cached(lane, code):
    lane[1].write_text(dispatched_program(code=code))
    for _ in range(8):
        assert invoke(lane).code == code
    assert spent(lane) == 8
    assert invoke(lane).code == 78
    assert len(lane[2].read_text().splitlines()) == 8


@pytest.mark.parametrize(
    "receipt", ["", "broken", "{}", '{"command_may_have_run":"false"}']
)
def test_an_unreadable_dispatch_receipt_never_refunds(lane, receipt):
    lane[1].write_text(dispatched_program(code=75, receipt=receipt))
    assert invoke(lane).code == 75
    assert spent(lane) == 1


def test_an_executable_that_cannot_start_spends_no_read(lane):
    Path(lane[0]["onex"]).unlink()
    assert invoke(lane).code == 78
    assert spent(lane) == 0
    assert not lane[2].exists()


def test_concurrent_refunds_preserve_other_calls_charges(lane):
    lane[1].write_text(
        "import sys\n"
        "if sys.argv[sys.argv.index('--step') + 1].startswith('local'):\n"
        "    raise SystemExit(69)\n" + dispatched_program()
    )
    steps = ["sent-one", "sent-two", *(f"local-{i}" for i in range(6))]
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(lambda step: invoke(lane, "--step", step), steps))
    assert [result.code for result in results] == [0, 0, *([69] * 6)]
    assert spent(lane) == 2
    assert len(lane[2].read_text().splitlines()) == 2


def test_job_log_through_canonical_runtime_bus_reads_the_failure_once(lane):
    root = Path(__file__).resolve().parents[3]
    # Keep the real CLI, discovery, adapter, event bus and gateway handler; replace
    # only the credential lookup and external HTTP transport with a recorded read.
    lane[1].write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        f"sys.path[:0] = {[str(root / 'src'), str(root)]!r}\n"
        "from click.testing import CliRunner\n"
        "from omnimarket.handlers.scm.scm_cli import scm_group\n"
        "from omnimarket.nodes.node_github_repo_gateway_effect.handlers import handler_github_repo_gateway as gateway\n"
        "from tests.nodes.node_github_repo_gateway_effect.recorded_transport import BytesExchange, RecordedGatewayTransport, get\n"
        "async def credential():\n"
        "    return 'fixture-only'\n"
        "class ObservedTransport(RecordedGatewayTransport):\n"
        "    def send_bytes_window_sync(self, *args, **kwargs):\n"
        "        marker = Path(sys.argv[sys.argv.index('--dispatch-receipt') + 1])\n"
        "        assert json.loads(marker.read_text()) == {'command_may_have_run': True}\n"
        "        with Path(os.environ['TEST_NETWORK']).open('a') as stream:\n"
        "            stream.write('recorded GET\\n')\n"
        "        return super().send_bytes_window_sync(*args, **kwargs)\n"
        "log = b'A' * 600000 + b'FATAL: recorded failing step\\n' + b'B' * 48000\n"
        "transport = ObservedTransport([BytesExchange(get('/repos/o/r/actions/jobs/77/logs'), 200, log, 64 * 1024 * 1024, 'window')])\n"
        "gateway.resolve_github_token_async = credential\n"
        "gateway.RealGitHubReadTransport = lambda token: transport\n"
        "result = CliRunner().invoke(scm_group, [*sys.argv[2:], '--bus', 'inmemory'])\n"
        "sys.stdout.write(result.stdout)\n"
        "sys.stderr.write(result.stderr)\n"
        "if result.exit_code == 0:\n"
        "    assert not transport.exchanges\n"
        "raise SystemExit(result.exit_code)\n"
    )
    first = invoke(lane)
    assert first.code == 0, first.stderr + first.stdout[-4000:]
    answer = json.loads(first.stdout.strip().splitlines()[-1])
    assert answer["source"] == "bus"
    assert answer["bus"]["command_topic"]
    assert answer["bus"]["terminal_topic"]
    assert answer["result"]["truncated"] is True
    assert "FATAL: recorded failing step" in answer["result"]["text"]
    assert len(answer["result"]["text"].encode()) == 65536
    assert spent(lane) == 1
    assert invoke(lane).stdout == first.stdout
    assert spent(lane) == 1
    assert lane[2].read_text().splitlines() == ["recorded GET"]


@pytest.mark.parametrize(
    ("extra", "option", "size"),
    [
        ((), "--tail-bytes", "65536"),
        (("--offset-bytes", "0"), "--length-bytes", "65536"),
        (("--step", "Run pytest (full suite)"), "--length-bytes", "65536"),
        (("--tail-bytes", "16384"), "--tail-bytes", "16384"),
        (("--offset-bytes", "0", "--length-bytes", "16384"), "--length-bytes", "16384"),
        (("--tail-bytes=16384",), "--tail-bytes=16384", None),
    ],
)
def test_log_defaults_use_the_existing_cap_and_honor_explicit_windows(
    lane, extra, option, size
):
    lane[1].write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    result = invoke(lane, *extra)
    assert result.code == 0
    args = json.loads(result.stdout)
    assert option in args
    if size is not None:
        assert args[args.index(option) + 1] == size
    assert spent(lane) == 1


def test_budget_boundary_imports_without_the_cli_core_dependency():
    root = Path(__file__).resolve().parents[3]
    program = (
        "import importlib.abc, sys\n"
        f"sys.path.insert(0, {str(root / 'src')!r})\n"
        "class MissingCore(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname == 'omnibase_core' or fullname.startswith('omnibase_core.'):\n"
        "            raise ModuleNotFoundError(\"No module named 'omnibase_core'\")\n"
        "sys.meta_path.insert(0, MissingCore())\n"
        "from omnimarket.nodes.node_lane_github_budget_effect import NodeLaneGithubBudgetEffect\n"
        "assert callable(NodeLaneGithubBudgetEffect().handle)\n"
        "assert 'omnibase_core' not in sys.modules\n"
        "print('budget boundary imported without omnibase_core')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "budget boundary imported without omnibase_core\n"
