# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Remote lanes share CI reads and relay attributable accounting without GitHub."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_lane_github_budget_effect.handlers.handler_lane_github_budget import (
    HandlerLaneGithubBudget,
    quota_consumers,
)
from omnimarket.nodes.node_lane_github_budget_effect.models.model_lane_github_budget import (
    ModelLaneGithubCall,
)

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.unit


def test_accounting_relay_uses_the_installed_market_package(tmp_path):
    from omnimarket.nodes.node_lane_github_budget_effect.handlers.handler_lane_github_budget import (
        collect_calls,
    )

    def ssh(script, timeout):
        assert "omnimarket.nodes.node_lane_github_budget_effect import main" in script
        assert "PYTHONPATH" not in script
        return subprocess.CompletedProcess([], 0, '{"lane": "one\\n"}', "")

    assert collect_calls(ssh, "run-a", tmp_path / "calls.log") == {"lane": 1}


@pytest.fixture(autouse=True)
def deployment_overlay(tmp_path, monkeypatch):
    import yaml

    root = tmp_path / "overlays/repos"
    root.mkdir(parents=True)
    (root / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "private_data": "repo-private-data",
                "plugin_source": "repo-plugin-source",
                "knowledge_base": "repo-knowledge",
                "deploy": "repo-deploy",
                "private_data_home_envs": ["PRIVATE_DATA_ROOT"],
                "knowledge_base_home_env": "KNOWLEDGE_ROOT",
                "beside_root": ["private_data", "knowledge_base"],
            }
        )
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root.parent))


@pytest.fixture
def policy(tmp_path):
    count = tmp_path / "network"
    binary = tmp_path / "real-tool"
    binary.write_text(
        '#!/bin/sh\nprintf "call\\n" >> "$TEST_NETWORK"\nprintf "answer\\n"\n'
    )
    binary.chmod(0o755)
    config = {
        "lane": "lane-a",
        "host": "lab-a",
        "budget": 2,
        "gh": str(binary),
        "onex": str(binary),
        "log": str(tmp_path / "calls.log"),
        "spent": str(tmp_path / "spent"),
        "cache": str(tmp_path / "shared-cache"),
    }
    return config, {"TEST_NETWORK": str(count), "PATH": os.environ["PATH"]}, count


def call(config, env, tool, *args):
    return HandlerLaneGithubBudget().handle(
        ModelLaneGithubCall(config, tool, tuple(args), env)
    )


@pytest.mark.parametrize("explicit_bin", [False, True])
def test_onex_execute_strips_the_lane_wrapper_from_path(policy, explicit_bin):
    config, env, _ = policy
    wrappers = Path(config["log"]).parent / "github-bin"
    wrappers.mkdir()
    alias = wrappers.with_name("wrapper-alias")
    alias.symlink_to(wrappers, target_is_directory=True)
    if explicit_bin:
        config["bin"] = str(alias)
    env["PATH"] = os.pathsep.join([str(wrappers), env["PATH"], str(alias)])
    Path(config["onex"]).write_text(
        '#!/bin/sh\nprintf "%s\\n" "$PATH" "${ONEX_GH_CALLS_LOG-unset}" "${ONEX_GH_USAGE_LOG-unset}"\n'
    )
    result = HandlerLaneGithubBudget.execute(
        ModelLaneGithubCall(config, "onex", ("scm", "pr-view", "o/r#1"), env)
    )
    assert result.code == 0
    path, calls_log, usage_log = result.stdout.splitlines()
    assert path == os.environ["PATH"]
    assert calls_log == usage_log == "unset"


def test_raw_reads_refuse_with_lane_and_watcher_guidance_but_writes_run(policy):
    config, env, network = policy
    for args in [
        ("pr", "view"),
        ("api", "--paginate", "repos/o/r/commits/abc/check-runs"),
        ("run", "view", "12", "--log-failed"),
        ("api", "graphql", "-f", "query=query { viewer { login } }"),
    ]:
        result = call(config, env, "gh", *args)
        assert result.code == 78
        assert "lane-a" in result.stderr
        assert "pr_state_local.py" in result.stderr
    assert not network.exists()
    assert call(config, env, "gh", "pr", "create", "--title", "test").code == 0
    assert call(config, env, "gh", "api", "-X", "POST", "repos/o/r/issues").code == 0
    assert len(network.read_text().splitlines()) == 2
    rows = Path(config["log"]).read_text().splitlines()
    assert all(
        len(row.split("\t")) == 8 and row.split("\t")[1] == "lane-a" for row in rows
    )
    assert "viewer" not in "\n".join(rows)


def test_cache_hits_spend_no_lane_budget_and_misses_refuse_by_lane(policy):
    config, env, network = policy
    for job in ("1", "2"):
        assert (
            call(config, env, "onex", "scm", "job-log", "o/r", "--job-id", job).code
            == 0
        )
    assert (
        call(config, env, "onex", "scm", "job-log", "o/r", "--job-id", "1").stdout
        == "answer\n"
    )
    refused = call(config, env, "onex", "scm", "job-log", "o/r", "--job-id", "3")
    assert refused.code == 78
    assert "lane-a" in refused.stderr
    assert "budget=2" in refused.stderr
    assert len(network.read_text().splitlines()) == 2


def test_shared_cache_serializes_lanes_and_failure_is_not_cached(policy):
    config, env, network = policy
    configs = [
        dict(config, lane=f"lane-{i}", spent=str(network.parent / f"spent-{i}"))
        for i in range(5)
    ]
    with ThreadPoolExecutor(max_workers=5) as workers:
        results = list(
            workers.map(
                lambda c: call(
                    c, env, "onex", "scm", "run-view", "o/r", "--run-id", "7"
                ),
                configs,
            )
        )
    assert all(result.code == 0 for result in results)
    assert len(network.read_text().splitlines()) == 1
    assert (
        sum(
            int(Path(c["spent"]).read_text())
            for c in configs
            if Path(c["spent"]).exists()
        )
        == 1
    )
    Path(config["onex"]).write_text("#!/bin/sh\nexit 3\n")
    for _ in range(2):
        assert (
            call(config, env, "onex", "scm", "job-log", "o/r", "--job-id", "8").code
            == 3
        )
    assert int(Path(config["spent"]).read_text()) == 2


def test_budget_reservation_is_atomic_and_corrupt_state_fails_closed(policy):
    config, env, network = policy
    with ThreadPoolExecutor(max_workers=6) as workers:
        results = list(
            workers.map(
                lambda i: call(
                    config, env, "onex", "scm", "job-log", "o/r", "--job-id", str(i)
                ),
                range(6),
            )
        )
    assert sum(result.code == 0 for result in results) == 2
    assert len(network.read_text().splitlines()) == 2
    Path(config["spent"]).write_text("broken")
    assert (
        call(config, env, "onex", "scm", "job-log", "o/r", "--job-id", "99").code == 78
    )


def test_remote_snapshot_counted_once_and_local_auth_cache_and_refusal_excluded(
    tmp_path,
):
    now = datetime.now(UTC)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    log = tmp_path / "gh-calls.log"
    log.write_text(f"{stamp}\tcontroller\tenv\tapi\tGET\tgraphql\t0\tallowed\n")
    remote = tmp_path / "gh-calls.d/lab-run.log"
    remote.parent.mkdir()
    data = f"{stamp}\tlane-a\tenv\tscm job-log\tGET\to/r/job/1\t0\tallowed\n"
    remote.write_text(
        data
        + f"{stamp}\tlane-a\tenv\tauth\tLOCAL\t-\t0\tallowed\n"
        + f"{stamp}\tlane-a\tenv\tread\tGET\t-\t0\tcache\n"
        + f"{stamp}\tlane-a\tenv\tread\tGET\t-\t78\trefused\n"
    )
    assert quota_consumers(log, 4998, now)["logged"] == 2
    remote.write_text(remote.read_text())
    result = quota_consumers(log, 4998, now)
    assert result["logged"] == 2
    assert result["unlogged"] == 0
    assert ["lane-a", 1] in result["top"]
    log.unlink()
    assert quota_consumers(log, 4999, now)["logged"] == 1


def test_local_pr_reads_cannot_fall_back_to_bus_and_commit_checks_use_watcher(policy):
    config, env, network = policy
    tool = Path(config["onex"])
    tool.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    result = call(config, env, "onex", "scm", "pr-checks", "o/r#1", "--source", "bus")
    assert result.stdout.splitlines()[-2:] == ["--source", "local"]
    assert not Path(config["spent"]).exists()
    assert (
        call(
            config,
            env,
            "onex",
            "scm",
            "commit-check-runs",
            "o/r",
            "--head-sha",
            "a" * 40,
        ).code
        == 78
    )
    assert not network.exists()


def test_installed_wrapper_enforces_policy_and_preserves_host_tool(tmp_path):
    from omnimarket.nodes.node_lane_github_budget_effect.handlers.handler_lane_github_budget import (
        setup_lines,
    )

    ctx, tools = tmp_path / "ctx", tmp_path / "tools"
    ctx.mkdir()
    tools.mkdir()
    network = tmp_path / "network"
    for name in ("gh", "onex"):
        tool = tools / name
        tool.write_text(
            '#!/bin/sh\nprintf "call\\n" >> "$TEST_NETWORK"\nprintf "answer\\n"\n'
        )
        tool.chmod(0o755)
    env = {
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
        "C": str(ctx),
        "ONEX_LANE": "wrapped-a",
        "ONEX_LANE_HOST": "host-a",
        "ONEX_REGISTRY_ROOT": str(tmp_path),
        "ONEX_STATE_DIR": str(tmp_path / "state"),
        "TEST_NETWORK": str(network),
    }
    done = subprocess.run(
        ["bash", "-c", "\n".join(setup_lines())],
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )
    assert done.returncode == 0, done.stderr

    def invoke(tool, *args):
        return subprocess.run(
            [str(ctx / "github-bin" / tool), *args],
            env=env,
            check=False,
            text=True,
            capture_output=True,
        )

    assert invoke("gh", "pr", "view").returncode == 78
    assert not network.exists()
    assert invoke("gh", "pr", "create").returncode == 0
    assert invoke("onex", "scm", "job-log", "o/r", "--job-id", "1").stdout == "answer\n"
    assert invoke("onex", "scm", "job-log", "o/r", "--job-id", "1").stdout == "answer\n"
    assert len(network.read_text().splitlines()) == 2
    assert json.loads((ctx / "github-policy.json").read_text())["lane"] == "wrapped-a"
    before = (ctx / "github-read-spent").read_text()
    repeat = subprocess.run(
        ["bash", "-c", "\n".join(setup_lines())],
        env={**env, "PATH": f"{ctx / 'github-bin'}{os.pathsep}{env['PATH']}"},
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert repeat.returncode == 0, repeat.stderr
    policy = json.loads((ctx / "github-policy.json").read_text())
    assert policy["gh"] == str(tools / "gh")
    assert policy["onex"] == str(tools / "onex")
    assert (ctx / "github-read-spent").read_text() == before


def test_host_json_calls_without_shared_tsv_are_relayed_idempotently(tmp_path):
    from omnimarket.nodes.node_lane_github_budget_effect.handlers.handler_lane_github_budget import (
        collect_calls,
        host_call_snapshot,
    )

    now = datetime.now(UTC)
    usage = tmp_path / f".cache/omni/gh-usage/{now:%Y-%m-%d}.jsonl"
    usage.parent.mkdir(parents=True)
    row = {
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lane": "watcher",
        "cls": "api",
        "method": "GET",
        "endpoint": "repos/o/r/pulls",
        "exit": 0,
        "cache": "none",
    }
    usage.write_text(
        "\n".join(
            json.dumps(r)
            for r in [row, dict(row, cache="hit"), dict(row, method="LOCAL")]
        )
    )
    snapshot = host_call_snapshot("run-a", True, home=tmp_path, now=now)
    assert len(snapshot["host"].splitlines()) == 1
    assert "watcher" in snapshot["host"]
    log = tmp_path / "launch/gh-calls.log"

    def ssh(script, timeout):
        assert "--host-calls run-a 1" in script
        assert timeout == 30
        return subprocess.CompletedProcess([], 0, json.dumps(snapshot), "")

    for _ in range(2):
        assert collect_calls(ssh, "run-a", log, host="lab-a")["host"] == 1
    assert quota_consumers(log, 4999, now)["logged"] == 1
    assert quota_consumers(log, 4999, now)["top"] == [["watcher", 1]]


def test_failed_relay_is_visible_and_does_not_replace_last_snapshot(tmp_path):
    from omnimarket.nodes.node_lane_github_budget_effect.handlers.handler_lane_github_budget import (
        collect_calls,
    )

    log = tmp_path / "gh-calls.log"
    old = tmp_path / "gh-calls.d/host-lab.log"
    old.parent.mkdir()
    old.write_text("last complete observation\n")

    def ssh(script, timeout):
        return subprocess.CompletedProcess([], 1, "", "offline")

    with pytest.raises(OSError, match="relay rc=1"):
        collect_calls(ssh, "run-a", log, host="lab")
    assert old.read_text() == "last complete observation\n"
    from omnimarket.nodes.node_lane_github_budget_effect.handlers.handler_lane_github_budget import (
        refresh_scope,
    )

    def collect():
        raise OSError("transport down")

    messages = []
    with refresh_scope(None, lambda: None, collect, 300, {}, messages.append) as info:
        assert info["github_accounting_error"] == "transport down"
    assert any("GITHUB-ACCOUNTING-FAILED" in line for line in messages)


def test_a_launcher_without_a_call_log_reports_the_relay_and_the_lane_runs_on():
    from omnimarket.nodes.node_lane_github_budget_effect.handlers.handler_lane_github_budget import (
        call_log_path,
    )

    assert call_log_path({"ONEX_REGISTRY_ROOT": "/o"}) == Path(
        "/o/.onex_state/gh-calls.log"
    )
    assert call_log_path({"ONEX_STATE_DIR": "/s", "ONEX_REGISTRY_ROOT": "/o"}) == Path(
        "/s/gh-calls.log"
    )
    with pytest.raises(ValueError, match="no call log"):
        call_log_path({})
    from omnimarket.nodes.node_lane_github_budget_effect.handlers.handler_lane_github_budget import (
        refresh_scope,
    )

    messages = []
    with refresh_scope(
        None, lambda: None, lambda: call_log_path({}), 300, {}, messages.append
    ) as info:
        assert "no call log" in info["github_accounting_error"]
    assert any("GITHUB-ACCOUNTING-FAILED" in line for line in messages)


def test_contract_retains_the_same_read_budget():
    from omnimarket.nodes.node_lane_github_budget_effect.handlers.handler_lane_github_budget import (
        POLICY,
    )

    contract = json.loads(
        (
            ROOT / "src/omnimarket/nodes/node_lane_github_budget_effect/contract.yaml"
        ).read_text()
    )
    assert contract["policy"] == POLICY
    assert POLICY["raw_gh_read_budget"] == 0
    assert POLICY["gateway_cache_misses_per_lane"] == 8


def test_an_unwritable_call_log_refuses_before_any_network_attempt(policy):
    config, env, network = policy
    config["log"] = str(network.parent)  # A directory cannot hold a TSV record.
    with pytest.raises(OSError, match="directory"):
        call(config, env, "gh", "pr", "create")
    assert not network.exists()


def test_the_preflight_login_probe_is_never_refused_as_a_read(tmp_path):
    """The 2026-10-18 host-c case: session_preflight's two login checks logged
    `auth status GET - 78 refused` twice, and the lane reported the refusal as a failed login."""
    from omnimarket.nodes.node_lane_github_budget_effect.handlers.handler_lane_github_budget import (
        setup_lines,
    )

    ctx, tools = tmp_path / "ctx", tmp_path / "tools"
    ctx.mkdir()
    tools.mkdir()
    network = tmp_path / "network"
    host_gh = tools / "gh"
    host_gh.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_NETWORK"\n'
        '[ -e "$TEST_LOGGED_OUT" ] && { echo "You are not logged into any GitHub hosts." >&2; exit 1; }\n'
        'printf "github.com\\n  Logged in to github.com\\n  Token scopes: repo, workflow\\n"\n'
    )
    host_gh.chmod(0o755)
    (tools / "onex").write_text("#!/bin/sh\nexit 0\n")
    (tools / "onex").chmod(0o755)
    env = {
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
        "C": str(ctx),
        "ONEX_LANE": "pf-drive-a",
        "ONEX_LANE_HOST": "host-a",
        "ONEX_REGISTRY_ROOT": str(tmp_path),
        "ONEX_STATE_DIR": str(tmp_path / "state"),
        "TEST_NETWORK": str(network),
        "TEST_LOGGED_OUT": str(tmp_path / "logged-out"),
    }
    done = subprocess.run(
        ["bash", "-c", "\n".join(setup_lines())],
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )
    assert done.returncode == 0, done.stderr

    def probe(*extra):
        return subprocess.run(
            [str(ctx / "github-bin" / "gh"), "auth", "status", *extra],
            env=env,
            check=False,
            text=True,
            capture_output=True,
        )

    # github_login and github_token_scopes, the overlay's two checks, back to back.
    first, second = probe(), probe()
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert "workflow" in second.stdout
    assert len(network.read_text().splitlines()) == 1  # host-shared, one request
    rows = [row.split("\t") for row in (ctx / "gh-calls.log").read_text().splitlines()]
    assert [row[3:] for row in rows] == [
        ["auth status", "AUTH", "-", "-1", "allowed"],
        ["auth status", "AUTH", "-", "0", "cache"],
    ]
    assert not (
        ctx / "github-read-spent"
    ).exists()  # the lane's read budget is untouched

    # A real failed login is reported as one and never cached.
    (tmp_path / "logged-out").touch()
    shutil.rmtree(tmp_path / "state" / "github-read-cache")
    for _ in range(2):
        failed = probe()
        assert failed.returncode == 1
        assert "not logged" in failed.stderr
    # Printing the token stays a refused read, so no token reaches the shared cache.
    shown = probe("--show-token")
    assert shown.returncode == 78
    assert "REFUSED" in shown.stderr
    assert len(network.read_text().splitlines()) == 3


# --- the preflight login probe cannot stall a lane (OMN-17427, 2026-10-18 host-c timeouts) -------

OVERLAY = ROOT / "plugins/onex-overlays/overlays/session_preflight/overlay.yaml"
SECRET = "ghp_" + "a" * 36


def auth_policy(tmp_path, monkeypatch, **bounds):
    """A host gh that answers `auth token` locally and sleeps on `auth status` while TEST_SLOW exists."""
    from omnimarket.nodes.node_lane_github_budget_effect.handlers import (
        handler_lane_github_budget as module,
    )

    network = tmp_path / "network"
    binary = tmp_path / "real-gh"
    binary.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_NETWORK"\n'
        f'if [ "$1 $2" = "auth token" ]; then [ -e "$TEST_NO_TOKEN" ] && exit 1; echo {SECRET}; exit 0; fi\n'
        '[ -e "$TEST_SLOW" ] && sleep 30\n'
        'printf "github.com\\n  Logged in to github.com\\n  Token scopes: repo, workflow\\n"\n'
    )
    binary.chmod(0o755)
    config = {
        "lane": "lane-a",
        "host": "lab-a",
        "budget": 2,
        "gh": str(binary),
        "onex": str(binary),
        "log": str(tmp_path / "calls.log"),
        "spent": str(tmp_path / "spent"),
        "cache": str(tmp_path / "shared-cache"),
    }
    env = {
        "TEST_NETWORK": str(network),
        "TEST_SLOW": str(tmp_path / "slow"),
        "TEST_NO_TOKEN": str(tmp_path / "no-token"),
        "PATH": os.environ["PATH"],
    }
    policy = {
        **module.POLICY,
        "auth_status_lock_wait_seconds": 0.5,
        "auth_status_timeout_seconds": 1,
        "auth_status_local_seconds": 1,
        **bounds,
    }
    monkeypatch.setattr(module, "POLICY", policy)
    return config, env, tmp_path


def age_cache(root, seconds):
    for entry in (root / "shared-cache").glob("*.json"):
        old = entry.stat().st_mtime - seconds
        os.utime(entry, (old, old))


def probe_under_deadline(config, env, deadline=10.0):
    started = time.monotonic()
    result = call(config, env, "gh", "auth", "status")
    assert time.monotonic() - started < deadline
    return result


def test_the_login_probe_reaches_a_host_shim_that_walks_path_for_the_real_gh(tmp_path):
    """The root cause measured 2026-10-18 on host-b (and the same chain on host-c): the host gh shim picks
    the first `gh` on PATH that is not itself as the real one, which was the lane wrapper, so the wrapper
    ran the shim, which ran the wrapper, which waited on the lock its own parent held, forever."""
    from omnimarket.nodes.node_lane_github_budget_effect.handlers.handler_lane_github_budget import (
        setup_lines,
    )

    ctx, shim_dir, real_dir = tmp_path / "ctx", tmp_path / "shim", tmp_path / "real"
    for path in (ctx, shim_dir, real_dir):
        path.mkdir()
    (real_dir / "gh").write_text(
        '#!/bin/sh\nprintf "github.com\\n  Logged in to github.com\\n  Token scopes: repo, workflow\\n"\n'
    )
    (shim_dir / "gh").write_text(
        "#!/bin/sh\n"
        '[ "${SHIM_DEPTH:-0}" -lt 3 ] || { echo "shim recursion" >&2; exit 99; }\n'
        "export SHIM_DEPTH=$(( ${SHIM_DEPTH:-0} + 1 ))\n"
        'rest="$PATH:"\n'
        'while [ -n "$rest" ]; do\n'
        '  d="${rest%%:*}"; rest="${rest#*:}"; c="$d/gh"\n'
        '  [ -x "$c" ] && [ ! "$c" -ef "$0" ] && exec "$c" "$@"\n'
        "done\nexit 127\n"
    )
    (tmp_path / "onex-stub").write_text("#!/bin/sh\nexit 0\n")
    for path in (real_dir / "gh", shim_dir / "gh", tmp_path / "onex-stub"):
        path.chmod(0o755)
    (shim_dir / "onex").symlink_to(tmp_path / "onex-stub")
    env = {
        "PATH": os.pathsep.join([str(shim_dir), str(real_dir), os.environ["PATH"]]),
        "C": str(ctx),
        "ONEX_LANE": "pf-drive-a",
        "ONEX_LANE_HOST": "host-a",
        "ONEX_REGISTRY_ROOT": str(tmp_path),
        "ONEX_STATE_DIR": str(tmp_path / "state"),
    }
    done = subprocess.run(
        ["bash", "-c", "\n".join(setup_lines())],
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )
    assert done.returncode == 0, done.stderr
    lane_env = {**env, "PATH": f"{ctx / 'github-bin'}{os.pathsep}{env['PATH']}"}
    child = subprocess.Popen(
        [str(ctx / "github-bin" / "gh"), "auth", "status"],
        env=lane_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = child.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, 9)
        child.communicate()
        pytest.fail(
            "the lane wrapper ran itself through the host shim and never answered"
        )
    assert child.returncode == 0, err
    assert "workflow" in out


def test_a_slow_login_probe_degrades_to_the_last_verified_answer(tmp_path, monkeypatch):
    config, env, tmp = auth_policy(tmp_path, monkeypatch)
    assert call(config, env, "gh", "auth", "status").code == 0  # a real answer, cached
    age_cache(tmp, 3600)  # past the 300 s freshness, inside the stale window
    (tmp / "slow").touch()
    for writes in ({}, {"ONEX_LANE_GITHUB_WRITES": "0"}):
        result = probe_under_deadline(config, {**env, **writes})
        assert result.code == 0
        assert "workflow" in result.stdout
        assert "WARN" in result.stderr
        assert "last verified" in result.stderr
    assert SECRET not in result.stdout + result.stderr


def test_a_slow_login_probe_is_a_warning_only_for_a_lane_that_writes_nothing(
    tmp_path, monkeypatch
):
    config, env, tmp = auth_policy(tmp_path, monkeypatch)
    (tmp / "slow").touch()
    reader = probe_under_deadline(config, {**env, "ONEX_LANE_GITHUB_WRITES": "0"})
    assert reader.code == 0
    assert "WARN" in reader.stderr
    assert "not verified" in reader.stderr
    assert "Logged in" in reader.stdout
    assert "workflow" not in reader.stdout
    # A lane that writes (the default, and an unreadable declaration) is refused, never passed silently.
    for extra in (
        {},
        {"ONEX_LANE_GITHUB_WRITES": "1"},
        {"ONEX_LANE_GITHUB_WRITES": "maybe"},
    ):
        writer = probe_under_deadline(config, {**env, **extra})
        assert writer.code == 75
        assert "not verified" in writer.stderr
        assert "writes" in writer.stderr
    assert SECRET not in "".join(
        [
            reader.stdout,
            reader.stderr,
            writer.stdout,
            writer.stderr,
            (tmp / "calls.log").read_text(),
        ]
    )


def test_a_slow_login_probe_with_no_local_credential_is_a_failed_login(
    tmp_path, monkeypatch
):
    config, env, tmp = auth_policy(tmp_path, monkeypatch)
    (tmp / "slow").touch()
    (tmp / "no-token").touch()
    for extra in ({}, {"ONEX_LANE_GITHUB_WRITES": "0"}):
        result = probe_under_deadline(config, {**env, **extra})
        assert result.code == 1
        assert "no local credential" in result.stderr


def test_a_login_probe_does_not_wait_on_a_wedged_lock_holder(tmp_path, monkeypatch):
    import fcntl
    import hashlib

    config, env, tmp = auth_policy(tmp_path, monkeypatch)
    root = tmp / "shared-cache"
    root.mkdir(mode=0o700)
    key = hashlib.sha256(json.dumps(("auth", "status")).encode()).hexdigest()
    with (root / f"{key}.lock").open("a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)  # the lane whose probe never returns
        result = probe_under_deadline(config, {**env, "ONEX_LANE_GITHUB_WRITES": "0"})
    assert result.code == 0
    assert "WARN" in result.stderr
    assert (
        not (tmp / "network").exists() or "status" not in (tmp / "network").read_text()
    )
