# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shipped Codex registration merges without disturbing user hooks (OMN-18759)."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_codex_hook_registration_effect.enums import (
    EnumCodexHookRegistrationStatus,
)
from omnimarket.nodes.node_codex_hook_registration_effect.handlers.handler_codex_hook_registration import (
    HandlerCodexHookRegistration,
    main,
    merge_registration,
)
from omnimarket.nodes.node_codex_hook_registration_effect.models import (
    ModelCodexHookRegistrationRequest,
)

pytestmark = pytest.mark.unit

REGISTRATION = {
    "description": "Codex CLI hook registration for ONEX capture (OMN-18704).",
    "hooks": {
        "SessionStart": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/session_start_bus_mirror.sh --actor codex",
                        "timeout": 10,
                    }
                ]
            }
        ],
        "UserPromptSubmit": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/user_prompt_submit_bus_mirror.sh --actor codex",
                        "timeout": 10,
                    }
                ]
            }
        ],
        "PostToolUse": [
            {
                "matcher": ".*",
                "hooks": [
                    {
                        "type": "command",
                        "command": "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/post_tool_use_bus_mirror.sh --actor codex",
                        "timeout": 10,
                    }
                ],
            }
        ],
        "SessionEnd": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/session_end_bus_mirror.sh --actor codex",
                        "timeout": 3,
                    }
                ]
            }
        ],
    },
}

NOW = datetime(2026, 10, 17, 12, 34, 56, tzinfo=UTC)
EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "SessionEnd")


@pytest.fixture
def plugin_root(tmp_path: Path) -> Path:
    root = tmp_path / "plugins" / "onex"
    destination = root / "hooks" / "codex" / "hooks.json"
    destination.parent.mkdir(parents=True)
    destination.write_text(json.dumps(REGISTRATION), encoding="utf-8")
    return root


def _existing() -> dict[str, Any]:
    return {
        "description": "User hooks stay here",
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Bash|Grep|Glob",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "repowise-augment",
                            "timeout": 15,
                        }
                    ],
                }
            ],
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "user-pre-tool"}],
                }
            ],
        },
        "user_setting": {"enabled": True},
    }


def _target(tmp_path: Path) -> Path:
    target = tmp_path / "codex" / "hooks.json"
    target.parent.mkdir()
    target.write_text(json.dumps(_existing(), indent=4) + "\n", encoding="utf-8")
    return target


def _request(
    plugin_root: Path, codex_home: Path, *, apply: bool = False
) -> ModelCodexHookRegistrationRequest:
    return ModelCodexHookRegistrationRequest(
        plugin_root=plugin_root, codex_home=codex_home, apply=apply, now=NOW
    )


def test_real_registration_has_four_codex_emitters(plugin_root: Path) -> None:
    registration = json.loads(
        (plugin_root / "hooks" / "codex" / "hooks.json").read_text(encoding="utf-8")
    )
    root = plugin_root
    merged, added = merge_registration(registration, {}, root)

    assert tuple(merged["hooks"]) == EVENTS
    assert "description" not in merged
    commands = tuple(
        hook["command"]
        for groups in merged["hooks"].values()
        for group in groups
        for hook in group["hooks"]
    )
    assert len(commands) == len(EVENTS)
    assert commands == added
    for command in commands:
        assert command.startswith(f"CLAUDE_PLUGIN_ROOT={root} {root}/hooks/scripts/")
        assert command.endswith("--actor codex")


def test_existing_groups_are_preserved_first(plugin_root: Path, tmp_path: Path) -> None:
    target = _target(tmp_path)
    existing = json.loads(target.read_text(encoding="utf-8"))
    result = HandlerCodexHookRegistration().handle(
        _request(plugin_root, target.parent, apply=True)
    )
    merged = json.loads(target.read_text(encoding="utf-8"))

    assert result.status is EnumCodexHookRegistrationStatus.ADDED
    assert merged["hooks"]["PostToolUse"][:1] == existing["hooks"]["PostToolUse"]
    assert merged["hooks"]["PreToolUse"] == existing["hooks"]["PreToolUse"]
    assert merged["description"] == existing["description"]
    assert merged["user_setting"] == existing["user_setting"]


def test_second_apply_is_unchanged(plugin_root: Path, tmp_path: Path) -> None:
    target = _target(tmp_path)
    handler = HandlerCodexHookRegistration()
    first = handler.handle(_request(plugin_root, target.parent, apply=True))
    original_bytes = target.read_bytes()
    listing = sorted(target.parent.iterdir())
    backup_bytes = Path(first.backup_path).read_bytes()
    second_request = _request(plugin_root, target.parent, apply=True).model_copy(
        update={"now": datetime(2026, 10, 18, tzinfo=UTC)}
    )

    second = handler.handle(second_request)

    assert second.status is EnumCodexHookRegistrationStatus.UNCHANGED
    assert second.exit_code == 0
    assert second.added_commands == ()
    assert second.backup_path == ""
    assert target.read_bytes() == original_bytes
    assert sorted(target.parent.iterdir()) == listing
    assert Path(first.backup_path).read_bytes() == backup_bytes


def test_dry_run_preserves_bytes_and_listing(plugin_root: Path, tmp_path: Path) -> None:
    target = _target(tmp_path)
    original_bytes = target.read_bytes()
    listing = sorted(target.parent.iterdir())

    result = HandlerCodexHookRegistration().handle(_request(plugin_root, target.parent))

    assert result.status is EnumCodexHookRegistrationStatus.WOULD_ADD
    assert result.exit_code == 0
    assert len(result.added_commands) == len(EVENTS)
    assert result.hooks_file == str(target)
    assert result.backup_path == ""
    assert target.read_bytes() == original_bytes
    assert sorted(target.parent.iterdir()) == listing


def test_apply_backs_up_exact_bytes_with_injected_timestamp(
    plugin_root: Path, tmp_path: Path
) -> None:
    target = _target(tmp_path)
    original_bytes = target.read_bytes()
    config = target.parent / "config.toml"
    config.write_bytes(b"# user configuration\n")

    result = HandlerCodexHookRegistration().handle(
        _request(plugin_root, target.parent, apply=True)
    )

    assert result.status is EnumCodexHookRegistrationStatus.ADDED
    assert result.exit_code == 0
    assert result.backup_path == str(target.parent / "hooks.json.bak.20261017T123456Z")
    assert Path(result.backup_path).read_bytes() == original_bytes
    merged = json.loads(target.read_text(encoding="utf-8"))
    assert target.read_bytes() == (json.dumps(merged, indent=2) + "\n").encode()
    assert config.read_bytes() == b"# user configuration\n"
    assert sorted(path.name for path in target.parent.iterdir()) == [
        "config.toml",
        "hooks.json",
        "hooks.json.bak.20261017T123456Z",
    ]


def test_invalid_target_fails_without_writes(plugin_root: Path, tmp_path: Path) -> None:
    target = _target(tmp_path)
    target.write_bytes(b"{ invalid JSON\n")
    original_bytes = target.read_bytes()
    listing = sorted(target.parent.iterdir())

    result = HandlerCodexHookRegistration().handle(
        _request(plugin_root, target.parent, apply=True)
    )

    assert result.status is EnumCodexHookRegistrationStatus.FAILED
    assert result.exit_code == 1
    assert result.messages
    assert target.read_bytes() == original_bytes
    assert sorted(target.parent.iterdir()) == listing


def test_absent_target_created_without_backup(
    plugin_root: Path, tmp_path: Path
) -> None:
    home = tmp_path / "new-codex"

    result = HandlerCodexHookRegistration().handle(
        _request(plugin_root, home, apply=True)
    )

    assert result.status is EnumCodexHookRegistrationStatus.ADDED
    assert result.exit_code == 0
    assert result.backup_path == ""
    assert sorted(path.name for path in home.iterdir()) == ["hooks.json"]
    merged = json.loads((home / "hooks.json").read_text(encoding="utf-8"))
    assert tuple(merged["hooks"]) == EVENTS


def test_merge_does_not_mutate_inputs(plugin_root: Path) -> None:
    registration = json.loads(
        (plugin_root / "hooks" / "codex" / "hooks.json").read_text(encoding="utf-8")
    )
    existing = _existing()
    registration_before = deepcopy(registration)
    existing_before = deepcopy(existing)

    merged, added = merge_registration(registration, existing, plugin_root)
    merged["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = "changed"

    assert added
    assert registration == registration_before
    assert existing == existing_before


def test_main_prints_would_add_json(
    plugin_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "codex"

    exit_code = main(["--plugin-root", str(plugin_root), "--codex-home", str(home)])

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "would_add"
    assert result["hooks_file"] == str(home / "hooks.json")
    assert not home.exists()


def test_partial_group_adds_only_missing_commands(plugin_root: Path) -> None:
    root = plugin_root
    first = f"CLAUDE_PLUGIN_ROOT={root} {root}/first --actor codex"
    second = f"CLAUDE_PLUGIN_ROOT={root} {root}/second --actor codex"
    existing = {
        "hooks": {
            "PostToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": first}]}
            ]
        }
    }
    registration = {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": ".*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "${CLAUDE_PLUGIN_ROOT}/first --actor codex",
                        },
                        {
                            "type": "command",
                            "command": "${CLAUDE_PLUGIN_ROOT}/second --actor codex",
                            "timeout": 10,
                        },
                    ],
                }
            ]
        }
    }

    merged, added = merge_registration(registration, existing, root)

    assert added == (second,)
    assert merged["hooks"]["PostToolUse"] == [
        *existing["hooks"]["PostToolUse"],
        {
            "matcher": ".*",
            "hooks": [{"type": "command", "command": second, "timeout": 10}],
        },
    ]
    repeated, repeated_added = merge_registration(registration, merged, root)
    assert repeated == merged
    assert repeated_added == ()


@pytest.mark.parametrize("contents", ["{invalid", "[]", "{}", '{"hooks": []}'])
def test_invalid_registration_writes_nothing(
    plugin_root: Path, tmp_path: Path, contents: str
) -> None:
    registration = plugin_root / "hooks" / "codex" / "hooks.json"
    registration.write_text(contents, encoding="utf-8")
    target = _target(tmp_path)
    original_bytes = target.read_bytes()
    listing = sorted(target.parent.iterdir())

    result = HandlerCodexHookRegistration().handle(
        _request(plugin_root, target.parent, apply=True)
    )

    assert result.status is EnumCodexHookRegistrationStatus.FAILED
    assert result.exit_code == 1
    assert result.messages
    assert target.read_bytes() == original_bytes
    assert sorted(target.parent.iterdir()) == listing


@pytest.mark.parametrize("field", ["plugin_root", "codex_home"])
def test_request_rejects_relative_paths(tmp_path: Path, field: str) -> None:
    paths = {"plugin_root": tmp_path, "codex_home": tmp_path}
    paths[field] = Path("relative")

    with pytest.raises(ValidationError, match="path must be absolute"):
        ModelCodexHookRegistrationRequest(**paths, now=NOW)


def test_contract_declares_command_and_terminal_topics() -> None:
    contract_path = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_codex_hook_registration_effect/contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))

    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.codex-hook-registration.v1"
    ]
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.codex-hook-registration-applied.v1"
    ]
    assert contract["runtime_dispatch"]["command_topic"] == (
        "onex.cmd.omnimarket.codex-hook-registration.v1"
    )
    assert contract["terminal_event"] == (
        "onex.evt.omnimarket.codex-hook-registration-applied.v1"
    )
