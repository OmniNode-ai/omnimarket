# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The self-hosted Temporal server of a lab host, run by its registry_module_tick unit (temporal-server-systemd).

Reads the ``server`` block of this node's contract: one pinned release of the official temporalio CLI by version and
sha256. The release archive is fetched once into ``<home>/<data_directory>/cli/<release>/`` and its sha256 is checked
against the pin on every start, so a changed or truncated archive is refused before anything runs. The server then
replaces this process (``temporal server start-dev``) with its SQLite store at ``<home>/<data_directory>/<db_filename>``,
a durable path under the home, so its history survives a restart of the unit and of the host. ``--probe`` validates
the settings and returns without a fetch, a write or a start.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tarfile
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

CONTRACT = Path(__file__).with_name("contract.yaml")
RELEASE_URL = "https://github.com/temporalio/cli/releases/download/{release}/{asset}"  # url-authority-ok: the official temporalio CLI release asset; the contract pins release, asset and sha256
REQUIRED = (
    "release",
    "asset",
    "sha256",
    "data_directory",
    "db_filename",
    "ip",
    "port",
    "ui_port",
    "namespace",
)


def server_settings() -> dict[str, Any]:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    settings = dict(document.get("server") or {})
    missing = [key for key in REQUIRED if not str(settings.get(key, "")).strip()]
    if missing:
        raise ValueError(
            f"the server block of {CONTRACT.name} lacks {', '.join(missing)}"
        )
    if len(str(settings["sha256"])) != 64:
        raise ValueError("the server block's sha256 is not 64 hex characters")
    return settings


def release_url(settings: Mapping[str, Any]) -> str:
    return RELEASE_URL.format(release=settings["release"], asset=settings["asset"])


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def release_directory(settings: Mapping[str, Any], home: Path) -> Path:
    return home / str(settings["data_directory"]) / "cli" / str(settings["release"])


def ensure_binary(settings: Mapping[str, Any], home: Path) -> Path:
    """The pinned CLI binary, fetched once and checked against the pinned sha256 on every call."""
    directory = release_directory(settings, home)
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / str(settings["asset"])
    if not archive.exists():
        part = archive.with_name(archive.name + ".part")
        with (
            urllib.request.urlopen(
                release_url(settings),
                timeout=int(settings.get("download_timeout_s") or 300),
            ) as response,
            part.open("wb") as out,
        ):
            for block in iter(lambda: response.read(1 << 20), b""):
                out.write(block)
        part.replace(archive)
    actual = sha256_of(archive)
    if actual != str(settings["sha256"]):
        raise ValueError(
            f"{archive.name} sha256 {actual} is not the pinned {settings['sha256']}"
        )
    binary = directory / "temporal"
    with tarfile.open(archive, "r:gz") as bundle:
        member = bundle.getmember("temporal")
        source = bundle.extractfile(member)
        if source is None:
            raise ValueError(f"{archive.name} holds no temporal binary")
        body = source.read()
    if not binary.exists() or binary.read_bytes() != body:
        staged = binary.with_name("temporal.part")
        staged.write_bytes(body)
        staged.chmod(0o755)
        staged.replace(binary)
    return binary


def server_argv(settings: Mapping[str, Any], home: Path, binary: Path) -> list[str]:
    store = home / str(settings["data_directory"]) / str(settings["db_filename"])
    return [
        str(binary),
        "server",
        "start-dev",
        "--db-filename",
        str(store),
        "--ip",
        str(settings["ip"]),
        "--port",
        str(settings["port"]),
        "--ui-port",
        str(settings["ui_port"]),
        "--namespace",
        str(settings["namespace"]),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="runtime_temporal_server")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--omni-home", default="")
    parser.add_argument("--state-dir", default="")
    args = parser.parse_args(argv)
    settings = server_settings()
    home = Path(os.environ.get("HOME") or Path.home())
    if args.probe:
        sys.stdout.write(
            json.dumps(
                {
                    "probe": "ok",
                    "release": settings["release"],
                    "port": settings["port"],
                }
            )
            + "\n"
        )
        return 0
    binary = ensure_binary(settings, home)
    command = server_argv(settings, home, binary)
    sys.stdout.write(
        json.dumps({"start": command, "sha256": settings["sha256"]}) + "\n"
    )
    sys.stdout.flush()
    os.execv(command[0], command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
