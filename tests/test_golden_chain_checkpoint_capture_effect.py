# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for the checkpoint capture effect."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_checkpoint_capture_effect.handlers import (
    HandlerCheckpointCapture,
)
from omnimarket.nodes.node_checkpoint_capture_effect.models import (
    EnumCheckpointCaptureStatus,
    ModelCheckpointCaptureRequest,
    ModelCheckpointCaptureResult,
)

pytestmark = pytest.mark.unit

SESSION = "abcdefgh-1234"

#: A stand-in for hook_content_capture.py: copies stdin, speaks on stderr the
#: way the real one does.
FAKE_CAPTURE = """\
import os, sys
from pathlib import Path
Path(os.environ["FAKE_CAPTURE_OUT"]).write_bytes(sys.stdin.buffer.read())
print("hook_content_capture: journalled 1 content.captured record(s) (checkpoint)", file=sys.stderr)
"""


def fake_capture(root: Path) -> Path:
    script = root / "hooks" / "lib" / "hook_content_capture.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(FAKE_CAPTURE, encoding="utf-8")
    return script


def checkpoint_file(tmp_path: Path) -> Path:
    path = tmp_path / "handoffs" / "checkpoint.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b'{"schema": "omni.session-checkpoint/1",  "session_id": "abcdefgh-1234"}\n'
    )
    return path


def base_env(tmp_path: Path) -> dict[str, str]:
    return {"PATH": "/usr/bin:/bin", "FAKE_CAPTURE_OUT": str(tmp_path / "captured.bin")}


def test_golden_chain_handle_returns_the_typed_result(tmp_path: Path) -> None:
    path = checkpoint_file(tmp_path)
    script = fake_capture(tmp_path / "onex")
    request = ModelCheckpointCaptureRequest(
        checkpoint_path=str(path),
        session_id=SESSION,
        home=str(tmp_path),
        capture_script=str(script),
        capture_python=sys.executable,
    )
    monkey_env = base_env(tmp_path)
    with pytest.MonkeyPatch.context() as patch:
        for key, value in monkey_env.items():
            patch.setenv(key, value)
        result = HandlerCheckpointCapture().handle(request)
    assert isinstance(result, ModelCheckpointCaptureResult)
    assert result.status is EnumCheckpointCaptureStatus.OK
    assert result.returncode == 0
    assert result.capture_python == sys.executable
    assert result.capture_script == str(script)
    assert result.detail == (
        "hook_content_capture: journalled 1 content.captured record(s) (checkpoint)"
    )
    assert (tmp_path / "captured.bin").read_bytes() == path.read_bytes()


def test_error_chain_a_request_without_a_session_never_reaches_the_handler() -> None:
    with pytest.raises(ValidationError) as caught:
        ModelCheckpointCaptureRequest.model_validate({"checkpoint_path": "/x.json"})
    assert {err["loc"][0] for err in caught.value.errors()} == {"session_id"}


def test_error_chain_a_failed_capture_returns_failed(tmp_path: Path) -> None:
    script = tmp_path / "hooks" / "lib" / "hook_content_capture.py"
    script.parent.mkdir(parents=True)
    script.write_text("import sys; print('boom', file=sys.stderr); sys.exit(2)\n")
    result = HandlerCheckpointCapture().handle(
        ModelCheckpointCaptureRequest(
            checkpoint_path=str(checkpoint_file(tmp_path)),
            session_id=SESSION,
            home=str(tmp_path),
            capture_script=str(script),
            capture_python=sys.executable,
        )
    )
    assert result.status is EnumCheckpointCaptureStatus.FAILED
    assert result.returncode == 2
    assert result.detail == "boom"
