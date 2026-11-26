# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Codex attempt tests do not depend on the ambient API-key variables."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_ATTEMPT_TESTS = Path(__file__).with_name("test_codex_harness_attempt.py")


def test_tests_start_without_ambient_api_keys() -> None:
    assert "OPENAI_API_KEY" not in os.environ
    assert "CODEX_API_KEY" not in os.environ


def test_attempt_tests_pass_with_api_keys_set_in_the_environment() -> None:
    env = {
        **os.environ,
        "OPENAI_API_KEY": "sk-fixture-not-a-secret",
        "CODEX_API_KEY": "sk-fixture-not-a-secret",
    }
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(_ATTEMPT_TESTS),
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
