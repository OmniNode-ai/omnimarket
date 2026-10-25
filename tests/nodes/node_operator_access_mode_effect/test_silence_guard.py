"""Failing-first contract for silent operator turns (OMN-20894)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_operator_access_mode_effect.handlers.handler_access_mode_state import (
    read_global_silent,
    read_session,
    write_global_silent,
)
from omnimarket.nodes.node_operator_access_mode_effect.handlers.handler_operator_hooks_installer import (
    ENTRY_MODULE,
)
from omnimarket.nodes.node_operator_access_mode_effect.handlers.handler_silence_guard import (
    SILENCE_PREFIXES,
    HandlerSilenceGuard,
    is_silence_tick,
)
from omnimarket.nodes.node_operator_access_mode_effect.models import ModelSilenceRequest
from omnimarket.nodes.node_operator_access_mode_effect.models.model_silence_request import (
    ModelSilenceRequest as SilenceRequest,
)

pytestmark = pytest.mark.unit
FIXTURES = Path(__file__).resolve().parent / "fixtures"
PREFIXES = (
    "DECISION",
    "APPROVAL",
    "CREDENTIAL",
    "RESTORE FAILED",
    "M4 RED",
    "LAB DOWN",
)


def _sample(name: str) -> tuple[dict[str, str], dict[str, Any]]:
    loaded = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return dict(loaded["environ"]), dict(loaded["payload"])


def _environ(name: str, state: Path, **extra: str) -> dict[str, str]:
    environ, _ = _sample(name)
    environ["OMNI_ACCESS_MODE_STATE_DIR"] = str(state)
    environ.update(extra)
    return environ


def _run(
    event: str, payload: dict[str, Any], environ: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-m", ENTRY_MODULE, event],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": "/nonexistent",
            "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src"),
            **environ,
        },
    )


def _switch(environ: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-m", ENTRY_MODULE, "silent", *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": "/nonexistent",
            "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src"),
            **environ,
        },
    )


def _prompt(payload: dict[str, Any], prompt: str, environ: dict[str, str]) -> None:
    result = _run(
        "prompt",
        {**payload, "hook_event_name": "UserPromptSubmit", "prompt": prompt},
        environ,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def _stop(
    payload: dict[str, Any], text: str, environ: dict[str, str], **extra: Any
) -> subprocess.CompletedProcess[str]:
    result = _run(
        "stop",
        {
            **payload,
            "hook_event_name": "Stop",
            "stop_hook_active": False,
            "last_assistant_message": text,
            **extra,
        },
        environ,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    return result


def _assert_silence_reason(reason: str) -> None:
    assert "silent" in reason.lower()
    assert "end the turn with no text" in reason.lower()
    for prefix in PREFIXES:
        assert prefix in reason


def _assert_blocked(result: subprocess.CompletedProcess[str]) -> str:
    body = json.loads(result.stdout)
    assert body["decision"] == "block"
    _assert_silence_reason(body["reason"])
    return str(body["reason"])


def _transcript(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    path = tmp_path / "turn.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _assistant(*blocks: dict[str, Any], sidechain: bool = False) -> dict[str, Any]:
    return {
        "type": "assistant",
        "isSidechain": sidechain,
        "message": {"content": list(blocks)},
    }


def _text(text: str) -> dict[str, str]:
    return {"type": "text", "text": text}


def test_silence_request_is_exported_frozen_and_slotted() -> None:
    assert ModelSilenceRequest is SilenceRequest
    request = ModelSilenceRequest(texts=("first", "second"), silent=True)
    assert request.texts == ("first", "second")
    assert not hasattr(request, "__dict__")
    with pytest.raises(FrozenInstanceError):
        request.silent = False
    assert SILENCE_PREFIXES == PREFIXES


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("Silence tick", True),
        ("  \n\tSilence tick: inspect lanes", True),
        ("Silence ticking", True),
        ("silence tick", False),
        ("Please run a Silence tick", False),
        ("", False),
        ("silent on", False),
    ],
)
def test_is_silence_tick_uses_the_case_sensitive_lstripped_prefix(
    prompt: str, expected: bool
) -> None:
    assert is_silence_tick(prompt) is expected


@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("separator", [": ", " "])
def test_each_attention_prefix_is_allowed_as_a_single_line(
    prefix: str, separator: str
) -> None:
    request = ModelSilenceRequest((f" \n {prefix}{separator}pick A \n",), True)
    assert HandlerSilenceGuard().handle(request).allowed


@pytest.mark.parametrize(
    "texts",
    [(), ("", " \n\t"), ("DECISION: pick A", "", "LAB DOWN: host unreachable")],
)
def test_empty_blank_or_multiple_individually_allowed_blocks_pass(
    texts: tuple[str, ...],
) -> None:
    assert HandlerSilenceGuard().handle(ModelSilenceRequest(texts, True)).allowed


@pytest.mark.parametrize(
    "texts",
    [
        ("Done.",),
        ("decision: x",),
        ("DECISIONS: x",),
        ("APPROVALS: x",),
        ("M4 REDNESS: x",),
        ("DECISION: x\nmore",),
        ("DECISION: x\nAPPROVAL: y",),
        ("DECISION: x\r\nmore",),
        ("Earlier progress.", "DECISION: pick A"),
        ("DECISION: pick A", "Later progress."),
    ],
)
def test_silent_turn_refuses_any_prose_or_multiline_block(
    texts: tuple[str, ...],
) -> None:
    verdict = HandlerSilenceGuard().handle(ModelSilenceRequest(texts, True))
    assert not verdict.allowed
    assert verdict.guard == "operator_access_mode_silence_guard"
    assert verdict.reason_class == "silent-turn-text"
    _assert_silence_reason(verdict.reason)


def test_non_silent_turn_allows_arbitrary_text() -> None:
    verdict = HandlerSilenceGuard().handle(
        ModelSilenceRequest(("Done.\nMore prose.",), False)
    )
    assert verdict.allowed


@pytest.mark.parametrize(
    ("text", "blocked"),
    [
        ("Done.", True),
        ("DECISION: approve the merge", False),
        ("DECISION: pick A\nmore", True),
    ],
)
def test_tick_prompt_then_stop(tmp_path: Path, text: str, blocked: bool) -> None:
    environ = _environ("local_cli", tmp_path)
    _, payload = _sample("local_cli")
    _prompt(payload, "  Silence tick: inspect lanes", environ)
    assert read_session(tmp_path, payload["session_id"])["silence_tick"] is True
    stopped = _stop(payload, text, environ)
    if blocked:
        _assert_blocked(stopped)
    else:
        assert stopped.stdout == ""


@pytest.mark.parametrize("prefix", PREFIXES)
def test_each_attention_prefix_passes_the_stop_hook(
    tmp_path: Path, prefix: str
) -> None:
    environ = _environ("local_cli", tmp_path)
    _, payload = _sample("local_cli")
    _prompt(payload, "Silence tick", environ)
    assert _stop(payload, f"{prefix}: pick A", environ).stdout == ""


def test_prose_before_tool_use_is_caught_across_tool_results(tmp_path: Path) -> None:
    environ = _environ("local_cli", tmp_path)
    _, payload = _sample("local_cli")
    _prompt(payload, "Silence tick", environ)
    path = _transcript(
        tmp_path,
        [
            {"type": "user", "message": {"content": [_text("Silence tick")]}},
            _assistant(
                _text("I will inspect the lanes."),
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
            ),
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "ready"}
                    ]
                },
            },
            _assistant(_text("DECISION: approve the merge")),
        ],
    )
    _assert_blocked(
        _stop(
            payload, "DECISION: approve the merge", environ, transcript_path=str(path)
        )
    )


def test_every_text_block_in_an_assistant_row_is_checked(tmp_path: Path) -> None:
    environ = _environ("local_cli", tmp_path)
    _, payload = _sample("local_cli")
    _prompt(payload, "Silence tick", environ)
    path = _transcript(
        tmp_path,
        [
            {"type": "user", "message": {"content": "Silence tick"}},
            _assistant(_text("DECISION: pick A"), _text("Progress is healthy.")),
        ],
    )
    _assert_blocked(
        _stop(payload, "DECISION: pick A", environ, transcript_path=str(path))
    )


@pytest.mark.parametrize(
    "user_content",
    [
        "Silence tick",
        [_text("Silence tick")],
        [{"type": "tool_result", "content": "old"}, _text("Silence tick")],
    ],
)
def test_previous_turn_and_sidechains_do_not_leak_into_silent_turn(
    tmp_path: Path, user_content: Any
) -> None:
    environ = _environ("local_cli", tmp_path)
    _, payload = _sample("local_cli")
    _prompt(payload, "Silence tick", environ)
    path = _transcript(
        tmp_path,
        [
            {"type": "user", "message": {"content": "Earlier task"}},
            _assistant(_text("Old prose.")),
            {"type": "user", "message": {"content": user_content}},
            _assistant(_text("Lane progress."), sidechain=True),
            {
                "type": "user",
                "isSidechain": True,
                "message": {"content": "Sidechain prompt"},
            },
            _assistant(_text("DECISION: approve the merge")),
        ],
    )
    assert (
        _stop(
            payload, "DECISION: approve the merge", environ, transcript_path=str(path)
        ).stdout
        == ""
    )


def test_sidechain_user_row_does_not_reset_the_main_turn(tmp_path: Path) -> None:
    environ = _environ("local_cli", tmp_path)
    _, payload = _sample("local_cli")
    _prompt(payload, "Silence tick", environ)
    path = _transcript(
        tmp_path,
        [
            {"type": "user", "message": {"content": "Silence tick"}},
            _assistant(_text("Main turn prose.")),
            {
                "type": "user",
                "isSidechain": True,
                "message": {"content": "Lane prompt"},
            },
            _assistant(_text("DECISION: pick A")),
        ],
    )
    _assert_blocked(
        _stop(payload, "DECISION: pick A", environ, transcript_path=str(path))
    )


def test_unreadable_transcript_falls_back_to_last_message(tmp_path: Path) -> None:
    environ = _environ("local_cli", tmp_path)
    _, payload = _sample("local_cli")
    _prompt(payload, "Silence tick", environ)
    missing = str(tmp_path / "missing.jsonl")
    _assert_blocked(_stop(payload, "Done.", environ, transcript_path=missing))
    assert (
        _stop(payload, "DECISION: pick A", environ, transcript_path=missing).stdout
        == ""
    )


def test_new_non_tick_prompt_clears_the_turn_flag(tmp_path: Path) -> None:
    environ = _environ("local_cli", tmp_path)
    _, payload = _sample("local_cli")
    _prompt(payload, "Silence tick", environ)
    _prompt(payload, "Please give a progress update", environ)
    assert read_session(tmp_path, payload["session_id"])["silence_tick"] is False
    assert _stop(payload, "Progress is healthy.", environ).stdout == ""


@pytest.mark.parametrize(
    ("on", "off"), [("silent on", "silent off"), ("/silent on.", "/silent off!")]
)
def test_silent_prompt_commands_set_show_and_clear_session_mode(
    tmp_path: Path, on: str, off: str
) -> None:
    environ = _environ("local_cli", tmp_path)
    _, payload = _sample("local_cli")
    for command, enabled in [
        (on, True),
        ("silent", True),
        (off, False),
        ("silent", False),
    ]:
        result = _run(
            "prompt",
            {**payload, "hook_event_name": "UserPromptSubmit", "prompt": command},
            environ,
        )
        assert result.returncode == 0, result.stderr
        body = json.loads(result.stdout)
        hook = body["hookSpecificOutput"]
        assert hook["hookEventName"] == "UserPromptSubmit"
        context = hook["additionalContext"].lower()
        assert f"silent mode is {'on' if enabled else 'off'}" in context
        assert "end the turn with no text" in context
        assert "\n" not in body["systemMessage"]
        assert (
            read_session(tmp_path, payload["session_id"]).get("silent", False)
            is enabled
        )
        stopped = _stop(payload, "Done.", environ)
        if enabled:
            _assert_blocked(stopped)
        else:
            assert stopped.stdout == ""
    assert not read_global_silent(tmp_path)


def test_session_silent_mode_persists_across_non_tick_prompts(tmp_path: Path) -> None:
    environ = _environ("local_cli", tmp_path)
    _, payload = _sample("local_cli")
    _prompt(payload, "silent on", environ)
    _prompt(payload, "Please inspect lanes", environ)
    _assert_blocked(_stop(payload, "Done.", environ))


def test_global_silent_flag_uses_on_file_and_off_removes_it(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    assert read_global_silent(directory) is False
    write_global_silent(directory, True)
    assert (directory / "silent").read_text(encoding="utf-8").strip() == "on"
    assert read_global_silent(directory) is True
    write_global_silent(directory, False)
    assert not (directory / "silent").exists()
    assert read_global_silent(directory) is False
    write_global_silent(directory, False)


def test_global_silent_cli_applies_to_any_operator_session_and_can_clear(
    tmp_path: Path,
) -> None:
    environ = _environ("local_cli", tmp_path)
    _, payload = _sample("local_cli")
    enabled = _switch(environ, "on")
    assert enabled.returncode == 0, enabled.stderr
    assert len(enabled.stdout.splitlines()) == 1
    assert "on" in enabled.stdout.lower()
    assert read_global_silent(tmp_path)
    for name, session in [
        ("local_cli", payload),
        ("local_cli", {**payload, "session_id": "another-session"}),
        ("remote_bridge", _sample("remote_bridge")[1]),
    ]:
        _assert_blocked(_stop(session, "Done.", _environ(name, tmp_path)))
    disabled = _switch(environ, "off")
    assert disabled.returncode == 0, disabled.stderr
    assert len(disabled.stdout.splitlines()) == 1
    assert "off" in disabled.stdout.lower()
    assert not read_global_silent(tmp_path)
    assert _stop(payload, "Done.", environ).stdout == ""


def test_session_silent_cli_does_not_enable_other_sessions(tmp_path: Path) -> None:
    environ = _environ("local_cli", tmp_path)
    _, payload = _sample("local_cli")
    for word, enabled in [("on", True), ("off", False)]:
        result = _switch(environ, word, "--session", payload["session_id"])
        assert result.returncode == 0, result.stderr
        assert len(result.stdout.splitlines()) == 1
        assert word in result.stdout.lower()
        assert (
            read_session(tmp_path, payload["session_id"]).get("silent", False)
            is enabled
        )
        assert not read_global_silent(tmp_path)
        assert _stop({**payload, "session_id": "other"}, "Done.", environ).stdout == ""
        stopped = _stop(payload, "Done.", environ)
        if enabled:
            _assert_blocked(stopped)
        else:
            assert stopped.stdout == ""


@pytest.mark.parametrize(
    ("text", "active"), [("Done.", True), ("", False), (" \n\t", False)]
)
def test_reentrant_stop_and_empty_last_message_pass(
    tmp_path: Path, text: str, active: bool
) -> None:
    environ = _environ("local_cli", tmp_path)
    _, payload = _sample("local_cli")
    _prompt(payload, "Silence tick", environ)
    path = _transcript(tmp_path, [_assistant(_text("Earlier prose."))])
    assert (
        _stop(
            payload, text, environ, stop_hook_active=active, transcript_path=str(path)
        ).stdout
        == ""
    )


def test_headless_session_is_untouched_even_with_global_silence(tmp_path: Path) -> None:
    environ = _environ("headless_sdk", tmp_path)
    _, payload = _sample("headless_sdk")
    write_global_silent(tmp_path, True)
    for prompt in ("Silence tick", "silent on"):
        result = _run("prompt", {**payload, "prompt": prompt}, environ)
        assert result.returncode == 0
        assert result.stdout == ""
    assert read_session(tmp_path, payload["session_id"]) == {}
    assert _stop(payload, "Done.", environ).stdout == ""


def test_allowed_silence_verdict_still_runs_existing_reply_check(
    tmp_path: Path,
) -> None:
    environ = _environ("remote_bridge", tmp_path)
    _, payload = _sample("remote_bridge")
    _prompt(payload, "Silence tick", environ)
    result = _stop(payload, "DECISION: see /tmp/report.md", environ)
    body = json.loads(result.stdout)
    assert body["decision"] == "block"
    assert "paste" in body["reason"].lower()
    first = _stop(payload, "Done. See /tmp/report.md", environ)
    _assert_blocked(first)


def test_silent_turn_reply_with_local_path_refuses_in_remote(tmp_path: Path) -> None:
    environ = _environ("remote_bridge", tmp_path)
    _, payload = _sample("remote_bridge")
    _prompt(payload, "Silence tick", environ)
    body = json.loads(_stop(payload, "DECISION: see /tmp/report.md", environ).stdout)
    assert body["decision"] == "block"
    assert "part that matters" in body["reason"].lower()


def test_silent_turn_reply_with_local_path_allows_at_terminal(tmp_path: Path) -> None:
    environ = _environ("local_cli", tmp_path)
    _, payload = _sample("local_cli")
    _prompt(payload, "Silence tick", environ)
    assert _stop(payload, "DECISION: see /tmp/report.md", environ).stdout == ""


def test_silent_turn_named_id_passes_in_remote(tmp_path: Path) -> None:
    environ = _environ("remote_bridge", tmp_path)
    _, payload = _sample("remote_bridge")
    _prompt(payload, "Silence tick", environ)
    reply = "DECISION: merge the operator reply check (OMN-20894)"
    assert _stop(payload, reply, environ).stdout == ""
