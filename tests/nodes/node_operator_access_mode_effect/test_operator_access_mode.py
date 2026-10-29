"""The operator access mode node and the supervision rules it switches (OMN-20892 to OMN-20896).

RULING 2026-10-10T16:55:54Z lane=access-mode-9f8a, corrected by CORRECTION 2026-10-10T16:56:54Z:
the access mode is detected per session with a manual override; the foreground-wait guard is on in
both modes; the reply check refuses bare ticket and PR ids in both modes and local paths in remote
mode only; pushes go out only for attention items; remote sessions get a setup check at start.

The session fixtures under ``fixtures/operator_access_mode`` are the environment a session hands
its hooks plus its SessionStart payload: a Remote Control session driven from the phone (the
markers this session's own hooks see), a cloud session from the mobile app, an interactive CLI at
the Mac, and a scripted ``claude -p`` run.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_operator_access_mode_effect.handlers import (
    HandlerAccessModeDetect,
    HandlerAttentionPushGate,
    HandlerForegroundBashGuard,
    HandlerOperatorReplyCheck,
    HandlerRemoteSetupCheck,
)
from omnimarket.nodes.node_operator_access_mode_effect.handlers import (
    handler_access_mode_detect as detect_module,
)
from omnimarket.nodes.node_operator_access_mode_effect.handlers import (
    handler_access_mode_state as state_module,
)
from omnimarket.nodes.node_operator_access_mode_effect.handlers import (
    handler_operator_hook_cli as cli,
)
from omnimarket.nodes.node_operator_access_mode_effect.handlers import (
    handler_operator_hooks_installer as installer_module,
)
from omnimarket.nodes.node_operator_access_mode_effect.handlers import (
    handler_operator_reply_check as reply_module,
)
from omnimarket.nodes.node_operator_access_mode_effect.handlers.handler_operator_hooks_installer import (
    install_operator_hooks,
    uninstall_operator_hooks,
)
from omnimarket.nodes.node_operator_access_mode_effect.models import (
    EnumAccessMode,
    ModelAccessModeRequest,
    ModelForegroundBashRequest,
    ModelOperatorReplyRequest,
    ModelPushGateRequest,
    ModelRemoteSetupRequest,
)

pytestmark = pytest.mark.unit


def install_operator(settings_path: Path, hooks_dir: Path, python: str) -> Path:
    settings = (
        json.loads(settings_path.read_text(encoding="utf-8"))
        if settings_path.exists()
        else {}
    )
    root = install_operator_hooks(settings, hooks_dir, python)
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return root


def uninstall_operator(settings_path: Path, hooks_dir: Path) -> None:
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    uninstall_operator_hooks(settings, hooks_dir)
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")


detect = detect_module.detect
read_session = state_module.read_session
update_session = state_module.update_session
write_global_override = state_module.write_global_override
ENTRY_MODULE = installer_module.ENTRY_MODULE
MARKER = installer_module.MARKER
bare_ids = reply_module.bare_ids

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NODE_DIR = Path(cli.__file__).resolve().parents[1]
LOCAL, REMOTE, HEADLESS = (
    EnumAccessMode.LOCAL,
    EnumAccessMode.REMOTE,
    EnumAccessMode.HEADLESS,
)


def _sample(name: str) -> tuple[dict[str, str], dict[str, Any]]:
    loaded = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return dict(loaded["environ"]), dict(loaded["payload"])


def _environ(name: str, state: Path, **extra: str) -> dict[str, str]:
    environ, _ = _sample(name)
    environ["OMNI_ACCESS_MODE_STATE_DIR"] = str(state)
    environ.update(extra)
    return environ


def _resolve(name: str, state: Path, **extra: str) -> EnumAccessMode:
    environ = _environ(name, state, **extra)
    _, payload = _sample(name)
    return (
        HandlerAccessModeDetect()
        .handle(ModelAccessModeRequest(payload["session_id"], state, environ))
        .mode
    )


def _run(
    event: str, payload: dict[str, Any], environ: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run the hook entry as the registered hook runs it, with only the given environment."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": environ.get("HOME", "/nonexistent"),
    }
    env.update(environ)
    return subprocess.run(
        [sys.executable, "-B", "-m", ENTRY_MODULE, event],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )


# --- OMN-20892: detection on sample sessions --------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected", "signal"),
    [
        ("remote_bridge", REMOTE, "CLAUDE_CODE_ENVIRONMENT_KIND=bridge"),
        ("remote_cloud", REMOTE, "CLAUDE_CODE_REMOTE=true"),
        ("local_cli", LOCAL, "entrypoint=cli"),
        ("headless_sdk", HEADLESS, "entrypoint=sdk-cli"),
    ],
)
def test_sample_sessions_are_detected(
    name: str, expected: EnumAccessMode, signal: str
) -> None:
    environ, _ = _sample(name)
    mode, signals = detect(environ)
    assert mode is expected
    assert signal in signals


def test_a_landing_worker_is_headless_and_no_entrypoint_is_unknown(
    tmp_path: Path,
) -> None:
    assert (
        detect({"LANDING_WORKER_TAG": "w1", "CLAUDE_CODE_ENTRYPOINT": "cli"})[0]
        is HEADLESS
    )
    inherited = {"LANDING_WORKER_TAG": "w1", "CLAUDE_CODE_ENVIRONMENT_KIND": "bridge"}
    assert detect(inherited)[0] is HEADLESS
    assert detect({})[0] is None
    result = HandlerAccessModeDetect().handle(ModelAccessModeRequest("s", tmp_path, {}))
    assert (result.mode, result.source) == (REMOTE, "unknown")


def test_transcript_entrypoint_is_read_when_the_environment_has_none(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        '{"type":"queue-operation"}\n{"type":"user","entrypoint":"cli"}\n',
        encoding="utf-8",
    )
    payload = {"session_id": "s", "transcript_path": str(transcript)}
    result = cli.resolve(payload, {"OMNI_ACCESS_MODE_STATE_DIR": str(tmp_path)})
    assert (result.mode, result.source) == (LOCAL, "detected")


def test_overrides_win_in_order_env_then_session_then_global(tmp_path: Path) -> None:
    _, payload = _sample("remote_bridge")
    session = payload["session_id"]
    assert _resolve("remote_bridge", tmp_path) is REMOTE
    write_global_override(tmp_path, "local")
    assert _resolve("remote_bridge", tmp_path) is LOCAL
    update_session(tmp_path, session, override="remote")
    assert _resolve("remote_bridge", tmp_path) is REMOTE
    assert _resolve("remote_bridge", tmp_path, OMNI_ACCESS_MODE="local") is LOCAL
    update_session(tmp_path, session, override=None)
    write_global_override(tmp_path, None)
    assert _resolve("remote_bridge", tmp_path) is REMOTE


def test_file_overrides_never_make_a_headless_run_operator_facing(
    tmp_path: Path,
) -> None:
    write_global_override(tmp_path, "local")
    _, payload = _sample("headless_sdk")
    update_session(tmp_path, payload["session_id"], override="remote")
    assert _resolve("headless_sdk", tmp_path) is HEADLESS
    assert _resolve("local_cli", tmp_path) is LOCAL


def test_prompt_command_switches_this_session_and_auto_returns_to_detection(
    tmp_path: Path,
) -> None:
    environ = _environ("remote_bridge", tmp_path)
    _, payload = _sample("remote_bridge")
    payload = {**payload, "hook_event_name": "UserPromptSubmit", "prompt": "mode local"}
    switched = _run("prompt", payload, environ)
    assert switched.returncode == 0
    out = json.loads(switched.stdout)
    assert "local (session-override" in out["systemMessage"]
    assert read_session(tmp_path, payload["session_id"])["override"] == "local"
    assert _resolve("remote_bridge", tmp_path) is LOCAL

    back = _run("prompt", {**payload, "prompt": "Mode auto"}, environ)
    assert "remote (detected" in json.loads(back.stdout)["systemMessage"]
    assert "override" not in read_session(tmp_path, payload["session_id"])


def test_an_ordinary_prompt_records_the_idle_clock_and_prints_nothing(
    tmp_path: Path,
) -> None:
    environ = _environ("local_cli", tmp_path)
    _, payload = _sample("local_cli")
    result = _run("prompt", {**payload, "prompt": "what merged overnight?"}, environ)
    assert (result.returncode, result.stdout) == (0, "")
    assert (
        time.time() - read_session(tmp_path, payload["session_id"])["last_prompt_at"]
        < 60
    )


def test_cli_set_and_show(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNI_ACCESS_MODE_STATE_DIR", str(tmp_path))
    assert cli.mode_main(["set", "local"]) == 0
    assert (tmp_path / "override").read_text(encoding="utf-8").strip() == "local"
    assert cli.mode_main(["set", "remote", "--session", "abc"]) == 0
    assert read_session(tmp_path, "abc")["override"] == "remote"
    assert cli.mode_main(["set", "auto"]) == 0
    assert not (tmp_path / "override").exists()
    capsys.readouterr()
    assert cli.mode_main(["show"]) == 0
    assert "none (detection decides)" in capsys.readouterr().out


# --- shape rules (ported) and OMN-20893 wait rules ---------------------------------------------

SHAPE_REFUSE = [
    "cd ~/.local/state/omni/remote-lanes; ls -dt */ | head",
    "cd /repo && git log --oneline -3",
    "shasum -a 256 f; tail -n 1 g | cut -c1-200",
    "sleep 45",
    "ssh -o ConnectTimeout=5 labhost uptime",
    "for h in a b; do echo $h; done",
    "echo a\necho b",
    "make build &",
    "true || false",
    "FOO=1 cd /tmp",
    "/usr/bin/ssh host",
    "x=$(cd /tmp && pwd)",
]
SHAPE_ALLOW = [
    "git -C /repo log --oneline -3",
    "python3 /tmp/x/merges5h.py",
    "grep -a 'lane=x' /tmp/ledger.md | tail -1",
    'python3 ledger.py --append "2026 | CLAIM | displaces nothing; | a && b"',
    "python3 run.py --text 'one; two && three'",
    "ls -la /tmp 2>&1",
    "cmd &> /tmp/out",
    "python3 a.py \\\n  --flag b",
    "echo 'cd /tmp; sleep 3'",
]
WAIT_REFUSE = [
    "gh pr checks 4802 --repo OmniNode-ai/omnibase_infra --watch",
    "gh run watch 123",
    "kubectl get pods -n demo -w",
    "tail -f /tmp/lane.log",
    "watch -n 5 git status",
    "wait",
    "timeout 600 python3 poll.py",
    "timeout 10m python3 poll.py",
    "timeout -s TERM 600 python3 poll.py",
    "timeout 30 gh run watch 5",
    "printf x | gh run watch 123",
    "tail --follow=name /tmp/lane.log",
    "tail -fn 50 /tmp/lane.log",
]
WAIT_ALLOW = [
    "gh pr checks 4802 --repo OmniNode-ai/omnibase_infra",
    "timeout 30 python3 probe.py",
    "grep -n -- --watch notes.md",
    "tail -n 50 /tmp/lane.log",
    "tail -n 1 -- -f",
    "printf 'gh run watch 1'",
]


def _bash(
    command: str,
    *,
    main: bool = True,
    facing: bool = True,
    timeout_ms: int | None = None,
    mode: str = "",
) -> bool:
    return (
        HandlerForegroundBashGuard()
        .handle(ModelForegroundBashRequest(command, main, facing, timeout_ms, mode))
        .allowed
    )


@pytest.mark.parametrize("command", SHAPE_REFUSE)
def test_shape_refusals_are_unchanged(command: str) -> None:
    assert not _bash(command)
    assert not _bash(command, facing=False)


@pytest.mark.parametrize("command", SHAPE_ALLOW + WAIT_ALLOW)
def test_plain_commands_pass(command: str) -> None:
    assert _bash(command)


@pytest.mark.parametrize("command", WAIT_REFUSE)
def test_foreground_waits_are_refused_in_both_modes_and_name_the_workflow(
    command: str, tmp_path: Path
) -> None:
    for name in ("local_cli", "remote_bridge"):
        environ = _environ(name, tmp_path)
        _, payload = _sample(name)
        payload = {**payload, "tool_name": "Bash", "tool_input": {"command": command}}
        result = _run("pre-tool", payload, environ)
        assert result.returncode == 2, (name, command, result.stderr)
        assert "OMN-20893" in result.stderr
        assert "background Workflow" in result.stderr


def test_waits_pass_in_a_lane_and_in_a_headless_run() -> None:
    for command in WAIT_REFUSE:
        assert _bash(command, main=False)
        assert _bash(command, facing=False)


def test_a_long_bash_timeout_is_a_foreground_wait() -> None:
    assert not _bash("python3 probe.py", timeout_ms=600_000)
    assert _bash("python3 probe.py", timeout_ms=120_000)


# --- OMN-20893, RULING 2026-10-13T04:14:36Z: detached launch and chaining where nothing prompts ---

BYPASS = "bypassPermissions"
NO_BYPASS = ["default", "acceptEdits", "plan", ""]
DETACHED_ALLOW = [
    "nohup python3 job.py > /tmp/job.log 2>&1 &",
    "nohup python3 job.py &> /tmp/job.log &",
    "nohup python3 job.py >/tmp/job.out 2>/tmp/job.err &",
    "nohup python3 job.py >> /tmp/job.log 2>&1 &",
    "nohup python3 job.py 1> /tmp/job.log 2> /tmp/job.log &",
    "FOO=1 BAR=2 nohup python3 job.py > /tmp/job.log 2>&1 &",
    "nohup python3 job.py --text 'one; two && three' > /tmp/job.log 2>&1 &",
    "nohup python3 job.py > /tmp/job.log 2>&1 &\n",
]
DETACHED_REFUSE = [
    "python3 job.py > /tmp/job.log 2>&1 &",
    "nohup python3 job.py &",
    "nohup python3 job.py > /tmp/job.log &",
    "nohup python3 job.py 2> /tmp/job.err &",
    "nohup python3 job.py 2>&1 &",
    "nohup python3 job.py 2>&1 > /tmp/job.log &",
    "nohup python3 job.py > /tmp/job.log 2>&1 & echo started",
    "nohup python3 job.py > /tmp/job.log 2>&1 & nohup python3 b.py > /tmp/b.log 2>&1 &",
    "nohup python3 a.py > /tmp/a.log 2>&1 && nohup python3 b.py > /tmp/b.log 2>&1 &",
    "nohup python3 a.py | tee /tmp/a.log > /tmp/b.log 2>&1 &",
    "echo go; nohup python3 job.py > /tmp/job.log 2>&1 &",
    "env nohup python3 job.py > /tmp/job.log 2>&1 &",
    "nohup sleep 600 > /tmp/job.log 2>&1 &",
    "nohup ssh host long-job > /tmp/job.log 2>&1 &",
    "nohup tail -f /tmp/lane.log > /tmp/job.log 2>&1 &",
    "nohup timeout 600 python3 job.py > /tmp/job.log 2>&1 &",
]
CHAINS = [
    "export PROBE=1; python3 tool.py",
    "git -C /repo status && git -C /repo log --oneline -3",
    "python3 a.py || python3 b.py",
    "echo a\necho b",
    "for x in one two; do echo $x; done",
]


@pytest.mark.parametrize("command", DETACHED_ALLOW)
@pytest.mark.parametrize("mode", [BYPASS, *NO_BYPASS])
def test_a_fully_detached_launch_passes_in_every_mode(command: str, mode: str) -> None:
    assert _bash(command, mode=mode)
    assert _bash(command, facing=False, mode=mode)


@pytest.mark.parametrize("command", DETACHED_REFUSE)
@pytest.mark.parametrize("mode", [BYPASS, *NO_BYPASS])
def test_a_background_launch_that_is_not_fully_detached_is_refused_in_every_mode(
    command: str, mode: str
) -> None:
    assert not _bash(command, mode=mode)


@pytest.mark.parametrize("command", CHAINS)
def test_chaining_passes_under_bypass_permissions_only(command: str) -> None:
    assert _bash(command, mode=BYPASS)
    assert _bash(command, facing=False, mode=BYPASS)
    for mode in NO_BYPASS:
        assert not _bash(command, mode=mode), (command, mode)


def test_a_bypass_mode_must_match_exactly() -> None:
    for mode in ("bypasspermissions", "bypassPermissions ", "dontAsk", "auto"):
        assert not _bash("echo a; echo b", mode=mode), mode


@pytest.mark.parametrize(
    "command",
    [
        "make build &",
        "true && make build &",
        "echo a & echo b",
        "nohup python3 job.py > /tmp/job.log 2>&1 &; echo done",
    ],
)
def test_a_bare_background_ampersand_stays_refused_under_bypass(command: str) -> None:
    assert not _bash(command, mode=BYPASS)


@pytest.mark.parametrize(
    "command",
    [
        "cd /repo && git log --oneline -3",
        "git -C /repo fetch; cd /repo",
        "pushd /repo && ls",
        "ls /tmp; sleep 45",
        "ls /tmp && ssh -o ConnectTimeout=5 labhost uptime",
        "echo a || scp f labhost:/tmp",
        "echo a\nssh host hostname",
        "(cd /repo && git log --oneline -1)",
        "true && x=$(sleep 3)",
    ],
)
def test_cd_sleep_ssh_scp_stay_refused_inside_an_allowed_chain(command: str) -> None:
    assert not _bash(command, mode=BYPASS)
    assert not _bash(command, facing=False, mode=BYPASS)


@pytest.mark.parametrize(
    "command",
    [
        "git -C /repo fetch; gh pr checks 4802 --repo OmniNode-ai/omnibase_infra --watch",
        "echo go && gh run watch 123",
        "echo go || kubectl get pods -n demo -w",
        "ls /tmp; tail -f /tmp/lane.log",
        "ls /tmp; tail -fn 50 /tmp/lane.log",
        "echo go\nwatch -n 5 git status",
        "echo go; wait",
        "echo go && timeout 600 python3 poll.py",
        "echo go; timeout 10m python3 poll.py",
        "wait; echo go",
        "(wait)",
    ],
)
def test_a_wait_stays_refused_inside_an_allowed_chain(command: str) -> None:
    assert not _bash(command, mode=BYPASS)
    assert _bash(command, mode=BYPASS, facing=False)
    assert _bash(command, main=False, mode=BYPASS)


def test_a_chain_with_a_long_bash_timeout_is_still_a_wait() -> None:
    assert not _bash("echo a; echo b", mode=BYPASS, timeout_ms=600_000)
    assert _bash("echo a; echo b", mode=BYPASS, timeout_ms=120_000)


def test_quoted_text_in_a_chain_is_not_parsed_as_shell() -> None:
    assert _bash("echo 'cd /tmp; sleep 3'; echo 'wait'", mode=BYPASS)


def test_the_hook_entry_reads_the_permission_mode_from_the_payload(
    tmp_path: Path,
) -> None:
    chain = "git -C /repo status && git -C /repo log --oneline -3"
    for name in ("local_cli", "remote_bridge"):
        environ = _environ(name, tmp_path)
        _, payload = _sample(name)
        payload = {**payload, "tool_name": "Bash", "tool_input": {"command": chain}}
        bypass = _run("pre-tool", {**payload, "permission_mode": BYPASS}, environ)
        assert bypass.returncode == 0, (name, bypass.stderr)
        for mode in ("default", "acceptEdits"):
            refused = _run("pre-tool", {**payload, "permission_mode": mode}, environ)
            assert refused.returncode == 2, (name, mode, refused.stderr)
            assert "chained commands" in refused.stderr
        silent = {k: v for k, v in payload.items() if k != "permission_mode"}
        assert _run("pre-tool", silent, environ).returncode == 2, name


# --- OMN-20894: reply check -------------------------------------------------------------------

NAMED = [
    "Landed the infra startup-check fix (omnibase_infra#4802).",
    "OMN-20892 (the access-mode detector) is merged.",
    "- OMN-20893: the foreground-wait guard\n- OMN-20894: the operator reply check",
    "| OMN-20895 | remote setup check | merged |",
    "The five supervision tickets (OMN-20892, OMN-20893) are filed.",
    "Command:\n```\ngit log --grep OMN-20892 /private/tmp/r\n```\nThat is all.",
    "Details: https://tracker.example/issue/OMN-20892/access-mode-detector",
    "The access mode detector OMN-20892 is merged.",
]
BARE = [
    ("OMN-20892 is done.", ["OMN-20892"]),
    ("See omnibase_infra#4802 and PR #12.", ["omnibase_infra#4802", "PR #12"]),
    ("Merged (OMN-1).", ["OMN-1"]),
    ("| OMN-20895 | merged |", ["OMN-20895"]),
    ("I merged OMN-20892.", ["OMN-20892"]),
]


@pytest.mark.parametrize("text", NAMED)
def test_named_ids_pass_in_both_modes(text: str) -> None:
    for mode in (LOCAL, REMOTE):
        verdict = HandlerOperatorReplyCheck().handle(
            ModelOperatorReplyRequest(text, mode)
        )
        assert verdict.allowed, (mode, text, verdict.reason)


@pytest.mark.parametrize(("text", "ids"), BARE)
def test_bare_ids_are_refused_in_local_and_remote_mode(
    text: str, ids: list[str]
) -> None:
    assert bare_ids(text) == ids
    for mode in (LOCAL, REMOTE):
        verdict = HandlerOperatorReplyCheck().handle(
            ModelOperatorReplyRequest(text, mode)
        )
        assert not verdict.allowed
        assert (
            "omnibase_infra#4802" in verdict.reason
        )  # the worked example is in the refusal


# An id is named when its sentence, or its whole list item with the item's continuation lines,
# carries two plain words; an id inside inline code or a quotation is not prose and not flagged.
NAMED_BY_SENTENCE_OR_ITEM = [
    (
        "- **Your M4 ticket:** the contract fix for the M4 dod_verify ticket (OMN-19970) is up"
        " as a PR\n  (onex_change_control#13846). CI waits on\n  OMN-19970."
    ),
    'The check now passes "OMN-19970" when it is quoted as an example.',
    'Write "OMN-19970".',
    "Run `gh pr view omnimarket#4530`.",
    "Quoted: “omnimarket#4530”.",
    (
        "- the routing-state cleanup fix (omnimarket#4530) is in review.\n"
        "  Then omnimarket#4530 merges. CI on #4530 is green."
    ),
    (
        "1. the routing-state cleanup fix (omnimarket#4530) is in review.\n\n"
        "   Then PR #4530 lands."
    ),
    "Reviewed and shipped OMN-19970.",
]
STILL_BARE = [
    ("Done: OMN-19970.", ["OMN-19970"]),
    ("- Done: OMN-19970.", ["OMN-19970"]),
    ("Done: OMN-19970. The dashboard sweep finished cleanly.", ["OMN-19970"]),
    ("- the routing-state cleanup fix\n\nThen omnimarket#4530.", ["omnimarket#4530"]),
    ("- Merged:\n- OMN-19970", ["OMN-19970"]),
    ("Quoted `x` then OMN-19970.", ["OMN-19970"]),
]


@pytest.mark.parametrize("text", NAMED_BY_SENTENCE_OR_ITEM)
def test_an_id_named_anywhere_in_its_sentence_or_list_item_passes(text: str) -> None:
    assert bare_ids(text) == []
    for mode in (LOCAL, REMOTE):
        verdict = HandlerOperatorReplyCheck().handle(
            ModelOperatorReplyRequest(text, mode)
        )
        assert verdict.allowed, (mode, text, verdict.reason)


@pytest.mark.parametrize(("text", "ids"), STILL_BARE)
def test_an_id_with_no_name_in_its_sentence_or_item_is_still_refused(
    text: str, ids: list[str]
) -> None:
    assert bare_ids(text) == ids
    verdict = HandlerOperatorReplyCheck().handle(ModelOperatorReplyRequest(text, LOCAL))
    assert not verdict.allowed
    assert verdict.reason_class == "bare-ticket-or-pr-ids"


def test_quoting_does_not_hide_a_path_or_a_phone_step_from_the_remote_checks() -> None:
    for text, reason_class in (
        ("The report is at `/private/tmp/report.md`.", "local-file-paths"),
        ('The report is at "~/notes.txt".', "local-file-paths"),
        ("Then run `ssh-keygen` for the new host.", "phone-infeasible-steps"),
    ):
        assert (
            HandlerOperatorReplyCheck()
            .handle(ModelOperatorReplyRequest(text, LOCAL))
            .allowed
        )
        verdict = HandlerOperatorReplyCheck().handle(
            ModelOperatorReplyRequest(text, REMOTE)
        )
        assert not verdict.allowed, text
        assert verdict.reason_class == reason_class


def test_local_paths_are_refused_remote_only() -> None:
    text = "The report is at /private/tmp/report.md and ~/notes.txt."
    assert (
        HandlerOperatorReplyCheck()
        .handle(ModelOperatorReplyRequest(text, LOCAL))
        .allowed
    )
    verdict = HandlerOperatorReplyCheck().handle(
        ModelOperatorReplyRequest(text, REMOTE)
    )
    assert not verdict.allowed
    assert "phone" in verdict.reason
    link = "Published at https://example.com/private/tmp/report (the weekly report)."
    assert (
        HandlerOperatorReplyCheck()
        .handle(ModelOperatorReplyRequest(link, REMOTE))
        .allowed
    )


def test_a_reply_repeating_one_of_the_last_three_is_refused_in_every_mode() -> None:
    filler = "I'm holding for the 8:23 AM ET tick."
    later = "i'm holding for the 9:41 am et tick"
    for mode in (LOCAL, REMOTE):
        refused = HandlerOperatorReplyCheck().handle(
            ModelOperatorReplyRequest(later, mode, recent_replies=("other", filler))
        )
        assert not refused.allowed
        assert refused.reason_class == "repeated-reply"
        assert "OMN-20893" in refused.reason
        assert "end without a reply" in refused.reason
        assert refused.guard == "operator_access_mode_reply_check"
    window = (filler, "one", "two", "three")  # only the last three count
    assert (
        HandlerOperatorReplyCheck()
        .handle(ModelOperatorReplyRequest(later, LOCAL, recent_replies=window))
        .allowed
    )
    different = ModelOperatorReplyRequest(
        "A lane finished.", LOCAL, recent_replies=(filler,)
    )
    assert HandlerOperatorReplyCheck().handle(different).allowed


def test_an_empty_reply_is_never_a_repeat() -> None:
    for text in ("", "   ", "..."):
        request = ModelOperatorReplyRequest(text, LOCAL, recent_replies=(text, text))
        assert HandlerOperatorReplyCheck().handle(request).allowed


def test_stop_hook_asks_for_one_rewrite_and_never_loops(tmp_path: Path) -> None:
    environ = _environ("local_cli", tmp_path)
    _, payload = _sample("local_cli")
    stop = {
        **payload,
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "last_assistant_message": "OMN-20892 is merged.",
    }
    blocked = _run("stop", stop, environ)
    assert blocked.returncode == 0
    body = json.loads(blocked.stdout)
    assert body["decision"] == "block"
    assert "OMN-20892" in body["reason"]
    assert _run("stop", {**stop, "stop_hook_active": True}, environ).stdout == ""
    named = {
        **stop,
        "last_assistant_message": "The access-mode detector (OMN-20892) is merged.",
    }
    assert _run("stop", named, environ).stdout == ""
    headless = _environ("headless_sdk", tmp_path)
    assert _run("stop", stop, headless).stdout == ""


# --- OMN-20896: attention-only push -----------------------------------------------------------


def _push(message: str, mode: EnumAccessMode, idle: float | None = None) -> bool:
    return (
        HandlerAttentionPushGate()
        .handle(ModelPushGateRequest(message, mode, idle))
        .allowed
    )


def test_routine_pushes_are_refused_in_both_modes() -> None:
    for mode in (LOCAL, REMOTE):
        assert not _push("hourly tick: 4 merges, queue healthy", mode, idle=3600)
        assert not _push("Done: the access-mode detector merged", mode, idle=3600)


def test_attention_items_push_remote_and_wait_for_idle_at_the_terminal() -> None:
    message = "DECISION: hold or ship the prod promotion of the infra release?"
    assert _push(message, REMOTE, idle=10)
    assert not _push(message, LOCAL, idle=5 * 60)
    assert _push(message, LOCAL, idle=31 * 60)
    assert _push(message, LOCAL, idle=None)
    assert _push("LAB DOWN: the lab bus host stopped answering", REMOTE)


def test_a_push_is_held_to_the_reply_check_with_paths_refused_in_every_mode() -> None:
    assert not _push("APPROVAL: OMN-20892", REMOTE)
    assert _push("APPROVAL: OMN-20892 waits on you", REMOTE)  # its sentence names it
    assert not _push(
        "CREDENTIAL: new key needed, see ~/notes/key.txt", LOCAL, idle=3600
    )


@pytest.mark.parametrize("agent_id", [None, "", "   ", 0, "lane-1"])
@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Bash", {"command": "sleep 300"}),
        ("WebFetch", {"url": "https://example.com"}),
        ("SendUserMessage", {"message": "OMN-20892 is done."}),
        ("PushNotification", {"message": "status: all green"}),
    ],
)
def test_pre_tool_session_scope(
    agent_id: str | int | None,
    tool: str,
    tool_input: dict[str, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    environ = _environ("remote_bridge", tmp_path)
    _, payload = _sample("remote_bridge")
    payload.update(agent_id=agent_id, tool_name=tool, tool_input=tool_input)

    code = cli.on_pre_tool(payload, environ)

    expected = 0 if agent_id == "lane-1" and tool != "PushNotification" else 2
    assert code == expected
    assert bool(capsys.readouterr().err) == (expected == 2)


def test_push_gate_applies_inside_a_lane_too(tmp_path: Path) -> None:
    environ = _environ("remote_bridge", tmp_path)
    _, payload = _sample("remote_bridge")
    payload = {
        **payload,
        "agent_id": "lane-1",
        "tool_name": "PushNotification",
        "tool_input": {"message": "status: all green", "status": "proactive"},
    }
    result = _run("pre-tool", payload, environ)
    assert result.returncode == 2
    assert "OMN-20896" in result.stderr


# --- OMN-20895: remote setup check ------------------------------------------------------------

READY_SETTINGS: dict[str, Any] = {
    "permissions": {"defaultMode": "bypassPermissions"},
    "agentPushNotifEnabled": True,
    "enabledPlugins": {"omni@omninode-internal": True, "onex@omninode-tools-dev": True},
}
READY_CONFIG: dict[str, Any] = {"mcpServers": {"linear-server": {}, "github": {}}}
READY_ENV = {
    "OMNI_HOME": "/workspace/registry",
    "ONEX_LEDGER_PATH": "/workspace/ledger.md",
}


def test_a_ready_remote_setup_reports_ok() -> None:
    result = HandlerRemoteSetupCheck().handle(
        ModelRemoteSetupRequest("", READY_SETTINGS, READY_CONFIG, "", READY_ENV)
    )
    assert result.ok
    assert result.line.startswith("Remote setup: OK")


def test_each_gap_is_named_in_one_line() -> None:
    result = HandlerRemoteSetupCheck().handle(
        ModelRemoteSetupRequest(
            "default",
            {
                "enabledPlugins": {
                    "omni@omninode-internal": True,
                    "onex@omninode-tools": False,
                }
            },
            {"projects": {"/workspace/registry": {"mcpServers": {"github": {}}}}},
            "/workspace/registry/omnimarket",
            {"OMNI_HOME": "/workspace/registry"},
        )
    )
    assert "\n" not in result.line
    assert result.missing == (
        "permission mode default prompts, which stalls a phone session",
        "push notifications are off (agentPushNotifEnabled)",
        "no onex plugin enabled",
        "connector linear-server is not configured",
        "ONEX_LEDGER_PATH is not set",
    )


def test_session_start_reports_setup_remote_only(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({}), encoding="utf-8")
    state = tmp_path / "state"
    remote = _run(
        "session-start",
        _sample("remote_bridge")[1],
        {
            **_environ("remote_bridge", state),
            "HOME": str(home),
            "ONEX_SESSION_OPTIN": "1",
        },
    )
    body = json.loads(remote.stdout)
    assert "Remote setup: MISSING" in body["systemMessage"]
    assert "Access mode: remote" in body["hookSpecificOutput"]["additionalContext"]
    local = _run(
        "session-start",
        _sample("local_cli")[1],
        {**_environ("local_cli", state), "HOME": str(home), "ONEX_SESSION_OPTIN": "1"},
    )
    body = json.loads(local.stdout)
    assert "systemMessage" not in body
    assert "Remote setup" not in body["hookSpecificOutput"]["additionalContext"]
    assert "Access mode: local" in body["hookSpecificOutput"]["additionalContext"]
    headless = _run(
        "session-start",
        _sample("headless_sdk")[1],
        {
            **_environ("headless_sdk", state),
            "HOME": str(home),
            "ONEX_SESSION_OPTIN": "1",
        },
    )
    assert headless.stdout == ""


# --- OMN-21149: session start is opt-in -------------------------------------------------------


@pytest.mark.parametrize("name", ["remote_bridge", "local_cli"])
def test_session_start_records_the_mode_but_prints_nothing_without_an_opt_in(
    tmp_path: Path, name: str
) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({}), encoding="utf-8")
    state = tmp_path / "state"
    payload = _sample(name)[1]
    done = _run("session-start", payload, {**_environ(name, state), "HOME": str(home)})
    assert done.returncode == 0
    assert done.stdout == ""
    assert done.stderr == ""
    recorded = read_session(state, str(payload["session_id"]))
    assert recorded.get("mode") in {"local", "remote"}


def test_the_session_marker_opts_in_and_the_env_can_force_out(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({}), encoding="utf-8")
    state = tmp_path / "state"
    payload = _sample("local_cli")[1]
    markers = tmp_path / "markers"
    markers.mkdir()
    (markers / str(payload["session_id"])).write_text("x", encoding="utf-8")
    environ = {
        **_environ("local_cli", state),
        "HOME": str(home),
        "ONEX_SESSION_OPTIN_DIR": str(markers),
    }
    body = json.loads(_run("session-start", payload, environ).stdout)
    assert "Access mode: local" in body["hookSpecificOutput"]["additionalContext"]
    assert (
        _run("session-start", payload, {**environ, "ONEX_SESSION_OPTIN": "0"}).stdout
        == ""
    )
    other = {**payload, "session_id": "another-session"}
    assert _run("session-start", other, environ).stdout == ""


# --- installer and the installed copy ---------------------------------------------------------


def test_node_imports_only_the_standard_library() -> None:
    for path in NODE_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = {node.module.split(".")[0]}
            else:
                continue
            assert names <= sys.stdlib_module_names | {"__future__"}, (path, names)


def test_install_retires_the_untracked_shape_guard_and_is_idempotent(
    tmp_path: Path,
) -> None:
    settings_path = tmp_path / "settings.json"
    hooks_dir = tmp_path / "hooks"
    peer = {"hooks": [{"type": "command", "command": "/peer/status"}]}
    shape = {
        "matcher": "^Bash$",
        "hooks": [
            {"type": "command", "command": "python3 /h/foreground-bash-shape-guard.py"}
        ],
    }
    before = {"model": "opus", "hooks": {"PreToolUse": [peer, shape], "Stop": [peer]}}
    settings_path.write_text(json.dumps(before), encoding="utf-8")

    root = install_operator(settings_path, hooks_dir, sys.executable)
    install_operator(settings_path, hooks_dir, sys.executable)
    after = json.loads(settings_path.read_text(encoding="utf-8"))
    for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"):
        ours = [e for e in after["hooks"][event] if MARKER in json.dumps(e)]
        assert len(ours) == 1, event
    assert shape not in after["hooks"]["PreToolUse"]
    assert after["hooks"]["PreToolUse"][0] == peer
    assert after["hooks"]["Stop"][0] == peer
    assert (root / "omnimarket" / "nodes" / NODE_DIR.name / "contract.yaml").is_file()

    command = next(
        h["command"]
        for e in after["hooks"]["PreToolUse"]
        if MARKER in json.dumps(e)
        for h in e["hooks"]
    )
    _, payload = _sample("local_cli")
    payload = {
        **payload,
        "tool_name": "Bash",
        "tool_input": {"command": "gh run watch 1"},
    }
    env = {
        "PATH": os.environ.get("PATH", ""),
        **_environ("local_cli", tmp_path / "state"),
    }
    ran = subprocess.run(
        ["/bin/sh", "-c", command],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
        cwd=tmp_path,
    )
    assert ran.returncode == 2, ran.stderr
    assert "OMN-20893" in ran.stderr, ran.stderr

    uninstall_operator(settings_path, hooks_dir)
    final = json.loads(settings_path.read_text(encoding="utf-8"))
    assert final == {"model": "opus", "hooks": {"PreToolUse": [peer], "Stop": [peer]}}
    assert not root.exists()


def test_pathological_input_and_a_pipe_transcript_never_wedge_the_hook(
    tmp_path: Path,
) -> None:
    environ = _environ("local_cli", tmp_path)
    deep = '{"tool_name":"Bash","tool_input":' + "[" * 20000 + "0" + "]" * 20000 + "}"
    env = {"PATH": os.environ.get("PATH", ""), **environ}
    result = subprocess.run(
        [sys.executable, "-B", "-m", ENTRY_MODULE, "pre-tool"],
        input=deep,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )
    assert result.returncode == 0
    fifo = tmp_path / "transcript.fifo"
    os.mkfifo(fifo)
    payload = {"session_id": "s", "transcript_path": str(fifo)}
    assert (
        cli.resolve(payload, {"OMNI_ACCESS_MODE_STATE_DIR": str(tmp_path)}).mode
        is REMOTE
    )
