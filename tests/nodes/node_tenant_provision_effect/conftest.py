# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scratch PostgreSQL 16 these tests write to: CI's provisioned database, else an owned localhost-only container."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from uuid import uuid4

import pytest

from .postgres_support import DATABASE, accepts_sql

_DOCKER = shutil.which("docker")


@pytest.fixture(scope="module")
def port() -> Iterator[int]:
    """CI's provisioned PostgreSQL 16, else an owned localhost-only container."""
    if os.environ.get("INTEGRATION_POSTGRES_PASSWORD"):
        yield int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
        return
    if _DOCKER is None:
        pytest.skip("docker unavailable")
    created = subprocess.run(
        [
            _DOCKER,
            "run",
            "--detach",
            "--rm",
            "--name",
            f"tenant-provision-pg-{uuid4().hex[:12]}",
            "--label",
            "omnimarket.tenant-provision=local-test",
            "--env",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            "--env",
            f"POSTGRES_DB={DATABASE}",
            "--publish",
            "127.0.0.1::5432",
            "postgres:16-alpine",
        ],
        check=True,
        capture_output=True,
    )
    container = created.stdout.decode().strip()
    try:
        published = subprocess.run(
            [_DOCKER, "port", container, "5432/tcp"],
            check=True,
            capture_output=True,
        ).stdout.decode()
        chosen = int(published.strip().splitlines()[0].rsplit(":", maxsplit=1)[1])
        for _ in range(150):
            if asyncio.run(accepts_sql(chosen)):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("owned PostgreSQL 16 did not accept SELECT 1")
        yield chosen
    finally:
        subprocess.run(
            [_DOCKER, "rm", "--force", container], check=False, capture_output=True
        )
