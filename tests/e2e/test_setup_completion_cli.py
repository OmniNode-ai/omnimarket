# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real installed CLI orchestration with controlled audio transport, not speakers."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers.setup_shell_environment import requires_supported_macos

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize(
    ("kind", "tool", "notice"),
    [("audio", "say", "system voice"), ("notification", "osascript", "notification")],
)
@requires_supported_macos
def test_completion_review_and_once_only_cli(
    tmp_path: Path, kind: str, tool: str, notice: str
) -> None:
    home, binary = tmp_path / "home", tmp_path / "bin"
    home.mkdir()
    binary.mkdir()
    record = tmp_path / "spoken.jsonl"
    say = binary / tool
    say.write_text(
        f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
        f"with Path({str(record)!r}).open('a') as f: f.write(json.dumps(sys.stdin.read())+'\\n')\n"
        "print('audio tool stdout must not appear in CLI JSON')\n"
    )
    say.chmod(0o755)
    env = {
        "HOME": str(home),
        "PATH": str(binary)
        + ":"
        + str(Path(sys.executable).parent)
        + ":"
        + os.environ.get("PATH", "/usr/bin:/bin"),
        "ONEX_STATE_DIR": str(tmp_path / "state"),
    }
    argv = [
        str(Path(sys.executable).parent / "onex"),
        "setup",
        "--workspace",
        str(tmp_path / "workspace"),
        "--component",
        f"completion-{kind}",
        "--completion-name",
        "Ada",
    ]

    def run(*flags: str, answer: str = ""):
        return subprocess.run(
            [*argv, *flags],
            cwd=tmp_path,
            env=env,
            input=answer,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    review = run("--interactive", answer="n\n")
    assert review.returncode != 0
    assert notice in review.stdout.lower()
    assert not record.exists()
    first = run("--yes", "--no-interactive", "--json")
    assert first.returncode == 0, first.stdout + first.stderr
    journal = json.loads(first.stdout)
    assert journal["observations"][-1]["step"] == f"completion-{kind}"
    assert journal["observations"][-1]["status"] == "success"
    assert len(record.read_text().splitlines()) == 1
    for flags in [
        ("--yes", "--no-interactive", "--json"),
        ("--action", "verify", "--no-interactive", "--json"),
    ]:
        repeated = run(*flags)
        assert repeated.returncode == 0, repeated.stdout + repeated.stderr
        assert all(
            not item["changed"] for item in json.loads(repeated.stdout)["observations"]
        )
    assert len(record.read_text().splitlines()) == 1
    (tmp_path / "completion-cli-evidence.json").write_text(
        json.dumps(
            {
                "review_refused_before_playback": True,
                "first_observations": journal["observations"],
                "playback_count": 1,
                "transport": f"controlled {tool} executable; no real speaker or notification",
            },
            indent=2,
        )
    )
