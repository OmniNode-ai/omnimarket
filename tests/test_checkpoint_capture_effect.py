# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The checkpoint capture effect journals a written checkpoint as a `checkpoint` record.

Plan task TA.20a: the session checkpoint writer hands each written file to this
node, which pipes its bytes to the onex plugin's ``hook_content_capture.py
--kind checkpoint``. The capture script is faked here, standing in for the
omniclaude one (whose own tests cover the opt-out and drainer refusals); these
tests prove the node finds it, hands it the file's exact bytes and says what it
did. The hook loads the handler module by file path, so its module-level import
set is pinned here too.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from omnimarket.nodes.node_checkpoint_capture_effect.models import (
    ModelCheckpointCaptureResult,
)

pytestmark = pytest.mark.unit

COMPLETED_TOPIC = "onex.evt.omnimarket.checkpoint-capture-completed.v1"

HANDLER_FILE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_checkpoint_capture_effect"
    / "handlers"
    / "handler_checkpoint_capture.py"
)
SESSION = "abcdefgh-1234"

#: A stand-in for hook_content_capture.py: records argv, stdin and the opt-out
#: env, speaks on stderr the way the real one does, exits with FAKE_CAPTURE_RC.
FAKE_CAPTURE = """\
import json, os, sys
from pathlib import Path
out = Path(os.environ["FAKE_CAPTURE_OUT"])
out.write_bytes(sys.stdin.buffer.read())
out.with_suffix(".json").write_text(json.dumps({
    "argv": sys.argv[1:],
    "opt_out": os.environ.get("OMNICLAUDE_CONTENT_CAPTURE"),
    "pythonpath": os.environ.get("PYTHONPATH"),
}))
print("hook_content_capture: journalled 1 content.captured record(s) (checkpoint)", file=sys.stderr)
sys.exit(int(os.environ.get("FAKE_CAPTURE_RC", "0")))
"""


def _load_by_path() -> ModuleType:
    """Load the handler module the way the hook does: by file path, no package."""
    spec = importlib.util.spec_from_file_location(
        "checkpoint_capture_by_path", HANDLER_FILE
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["checkpoint_capture_by_path"] = module
    spec.loader.exec_module(module)
    return module


capture = _load_by_path()


def fake_capture(root: Path) -> Path:
    script = root / "hooks" / "lib" / "hook_content_capture.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(FAKE_CAPTURE, encoding="utf-8")
    return script


def checkpoint_file(tmp_path: Path) -> Path:
    path = tmp_path / "handoffs" / "checkpoint.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Key order and spacing a parse-and-reserialise would change.
    path.write_bytes(
        b'{"schema": "omni.session-checkpoint/1",  "session_id": "abcdefgh-1234"}\n'
    )
    return path


def base_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "FAKE_CAPTURE_OUT": str(tmp_path / "captured.bin"),
        "PYTHONPATH": "/must/not/reach/the/capture",
        **extra,
    }


def install_onex(
    tmp_path: Path, *, script: bool = True, interpreter: bool = True
) -> Path:
    """An installed onex plugin, older and newer installs, plus an omni plugin."""
    plugins = tmp_path / ".claude" / "plugins"
    old = plugins / "cache" / "mkt" / "onex" / "1.0.0"
    new = plugins / "cache" / "mkt" / "onex" / "2.0.0"
    if script:
        fake_capture(new)
    plugins.mkdir(parents=True, exist_ok=True)
    (plugins / "installed_plugins.json").write_text(
        json.dumps(
            {
                "plugins": {
                    "onex@mkt": [
                        {
                            "installPath": str(old),
                            "lastUpdated": "2026-10-01T00:00:00Z",
                        },
                        {
                            "installPath": str(new),
                            "lastUpdated": "2026-10-17T00:00:00Z",
                        },
                    ],
                    "omni@mkt": [{"installPath": str(old)}],
                }
            }
        ),
        encoding="utf-8",
    )
    if interpreter:
        venv = plugins / "data" / "onex-mkt" / ".venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "python3").symlink_to(sys.executable)
    return new


def test_module_level_imports_only_the_standard_library() -> None:
    """A hook cannot pay a package import; the models load inside ``handle``."""
    tree = ast.parse(HANDLER_FILE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.If):
            assert ast.unparse(node.test) == "TYPE_CHECKING"
    assert imported <= set(sys.stdlib_module_names)


def test_the_file_is_piped_to_the_capture_byte_for_byte(tmp_path: Path) -> None:
    path = checkpoint_file(tmp_path)
    env = base_env(
        tmp_path,
        OMNI_CONTENT_CAPTURE_PY=str(fake_capture(tmp_path / "onex")),
        OMNI_CONTENT_CAPTURE_PYTHON=sys.executable,
    )
    outcome = capture.capture_checkpoint(path, SESSION, env=env, home=tmp_path)
    assert outcome.status == "ok"
    assert outcome.line == (
        "capture ok: hook_content_capture: journalled 1 content.captured record(s) (checkpoint)"
    )
    assert not outcome.warning
    piped = (tmp_path / "captured.bin").read_bytes()
    assert (
        hashlib.sha256(piped).hexdigest()
        == hashlib.sha256(path.read_bytes()).hexdigest()
    )
    seen = json.loads((tmp_path / "captured.json").read_text())
    argv = seen["argv"]
    assert argv[argv.index("--kind") + 1] == "checkpoint"
    assert argv[argv.index("--session-id") + 1] == SESSION
    assert argv[argv.index("--correlation-id") + 1] == SESSION
    assert seen["pythonpath"] is None


def test_the_opt_out_reaches_the_capture_unchanged(tmp_path: Path) -> None:
    """The capture owns OMNICLAUDE_CONTENT_CAPTURE=0; the node must not drop it."""
    env = base_env(
        tmp_path,
        OMNI_CONTENT_CAPTURE_PY=str(fake_capture(tmp_path / "onex")),
        OMNI_CONTENT_CAPTURE_PYTHON=sys.executable,
        OMNICLAUDE_CONTENT_CAPTURE="0",
    )
    capture.capture_checkpoint(
        checkpoint_file(tmp_path), SESSION, env=env, home=tmp_path
    )
    assert json.loads((tmp_path / "captured.json").read_text())["opt_out"] == "0"


def test_a_failed_capture_is_failed_and_warns(tmp_path: Path) -> None:
    env = base_env(
        tmp_path,
        OMNI_CONTENT_CAPTURE_PY=str(fake_capture(tmp_path / "onex")),
        OMNI_CONTENT_CAPTURE_PYTHON=sys.executable,
        FAKE_CAPTURE_RC="1",
    )
    outcome = capture.capture_checkpoint(
        checkpoint_file(tmp_path), SESSION, env=env, home=tmp_path
    )
    assert outcome.status == "failed"
    assert outcome.returncode == 1
    assert outcome.warning
    assert outcome.line.startswith("capture FAILED: ")


def test_no_capture_entry_is_skipped(tmp_path: Path) -> None:
    outcome = capture.capture_checkpoint(
        checkpoint_file(tmp_path), SESSION, env=base_env(tmp_path), home=tmp_path
    )
    assert outcome.status == "skipped"
    assert not outcome.warning
    assert outcome.line.startswith("capture skipped: no onex hook_content_capture.py")
    assert not (tmp_path / "captured.bin").exists()


def test_the_newest_installed_onex_plugin_and_its_data_venv_are_used(
    tmp_path: Path,
) -> None:
    new = install_onex(tmp_path)
    path = checkpoint_file(tmp_path)
    outcome = capture.capture_checkpoint(
        path, SESSION, env=base_env(tmp_path), home=tmp_path
    )
    assert outcome.status == "ok"
    assert outcome.script == str(new / "hooks" / "lib" / "hook_content_capture.py")
    assert (tmp_path / "captured.bin").read_bytes() == path.read_bytes()


@pytest.mark.parametrize("missing", ["script", "interpreter"])
def test_an_install_missing_a_half_is_not_used(tmp_path: Path, missing: str) -> None:
    install_onex(
        tmp_path, script=missing != "script", interpreter=missing != "interpreter"
    )
    outcome = capture.capture_checkpoint(
        checkpoint_file(tmp_path), SESSION, env=base_env(tmp_path), home=tmp_path
    )
    assert outcome.status == "skipped"


def test_the_omniclaude_clone_is_the_last_candidate(tmp_path: Path) -> None:
    clone = tmp_path / "registry" / "omniclaude"
    pairs = capture.capture_entries({"OMNI_HOME": str(tmp_path / "registry")}, tmp_path)
    assert pairs == [
        (
            clone / ".venv" / "bin" / "python3",
            clone / "plugins" / "onex" / "hooks" / "lib" / "hook_content_capture.py",
        )
    ]


def test_a_missing_checkpoint_file_is_failed(tmp_path: Path) -> None:
    env = base_env(
        tmp_path,
        OMNI_CONTENT_CAPTURE_PY=str(fake_capture(tmp_path / "onex")),
        OMNI_CONTENT_CAPTURE_PYTHON=sys.executable,
    )
    outcome = capture.capture_checkpoint(
        tmp_path / "gone.json", SESSION, env=env, home=tmp_path
    )
    assert outcome.status == "failed"
    assert "FileNotFoundError" in outcome.line


def test_the_contract_declares_its_terminal_topic_and_typed_outputs() -> None:
    contract = yaml.safe_load(
        (HANDLER_FILE.parents[1] / "contract.yaml").read_text(encoding="utf-8")
    )
    assert contract["terminal_event"] == COMPLETED_TOPIC
    assert contract["event_bus"]["publish_topics"] == [COMPLETED_TOPIC]
    assert set(contract["outputs"]) == set(ModelCheckpointCaptureResult.model_fields)
