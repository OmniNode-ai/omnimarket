# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regenerate the factory floor record, then publish the page and the record to the serving host.

The record is the JSON ``node_factory_floor_effect`` writes. This node copies it, and the page that reads
it, to the directory a static server serves, over ssh, one file at a time, each written to a temporary
name and renamed into place so a reader never sees a partial file and a failed copy leaves the previous
file untouched. The host, the directory and the URL are deployment facts from the overlay; the paths,
file names and timeouts are the contract's.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import yaml

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishContract,
    ModelFactoryFloorPublishOverlay,
    ModelFactoryFloorPublishRequest,
    ModelFactoryFloorPublishResult,
    ModelFactoryFloorPublishRuntime,
)

NODE_NAME = "node_factory_floor_publish_effect"
NODE_DIR = Path(__file__).resolve().parents[1]
OWN_CONTRACT = NODE_DIR / "contract.yaml"

Runner = Callable[
    [Sequence[str], bytes | None, int], "subprocess.CompletedProcess[bytes]"
]


class PublishRefusedError(RuntimeError):
    """A source, setting or transfer is missing or failed; the message names it and the fix."""


def run_process(
    argv: Sequence[str], stdin: bytes | None, timeout: int
) -> subprocess.CompletedProcess[bytes]:
    """The one subprocess boundary of this node; tests replace it."""
    try:
        return subprocess.run(
            list(argv),
            input=stdin,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PublishRefusedError(
            f"{argv[0]} did not finish within {timeout} s"
        ) from exc
    except OSError as exc:
        raise PublishRefusedError(
            f"{argv[0]} could not start ({type(exc).__name__})"
        ) from exc


def load_runtime(contract_path: Path = OWN_CONTRACT) -> ModelFactoryFloorPublishRuntime:
    """The ``runtime`` block of this node's contract."""
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    return ModelFactoryFloorPublishContract.model_validate(raw).runtime


def remote_write_command(remote_dir: str, name: str) -> str:
    """The remote shell command that stores stdin as ``remote_dir/name`` atomically.

    The bytes go to a dot-named temporary file in the same directory and are renamed over the final name;
    when the write fails the temporary file is removed and the previous final file is left as it was.
    """
    final = PurePosixPath(remote_dir) / name
    tmp = PurePosixPath(remote_dir) / f".{name}.tmp"
    q_dir, q_final, q_tmp = (shlex.quote(str(p)) for p in (remote_dir, final, tmp))
    return (
        f"umask 022 && mkdir -p {q_dir} && "
        f"{{ cat > {q_tmp} && mv -f {q_tmp} {q_final}; }} || {{ rm -f {q_tmp}; exit 1; }}"
    )


class HandlerFactoryFloorPublish:
    """Boundary of the publish: the refresh, the reads and the copies happen here."""

    def __init__(self, runner: Runner = run_process) -> None:
        self._run = runner

    def handle(
        self,
        request: ModelFactoryFloorPublishRequest,
        environ: Mapping[str, str] | None = None,
    ) -> ModelFactoryFloorPublishResult:
        """Raises ``PublishRefusedError`` or ``NodeOverlayError`` naming what is missing."""
        env = os.environ if environ is None else environ
        rt = load_runtime()
        overlay = load_structured_overlay(
            NODE_NAME,
            ModelFactoryFloorPublishOverlay,
            env,
            explicit=request.overlay,
            file_env=rt.overlay_file_env,
        )
        home_value = (request.home or env.get(rt.home_env, "")).strip()
        if not home_value:
            raise PublishRefusedError(
                f"{rt.home_env} is not set (fail fast, no default): export it or pass --omni-home"
            )
        home = Path(home_value)

        refreshed = False
        if not request.skip_refresh:
            self._refresh(rt, home)
            refreshed = True

        data = self._read_record(home / rt.source.relpath)
        page = (NODE_DIR / rt.page_relpath).read_bytes()
        generated_at = str(json.loads(data)["generated_at"])

        # Page first, record last: a reader that gets the new record always finds a page that reads it.
        for name, payload in ((rt.names.page, page), (rt.names.data, data)):
            self._copy(rt, overlay, name, payload)

        digest = hashlib.sha256(data).hexdigest()
        if request.state_dir:
            self._write_evidence(
                Path(request.state_dir) / rt.evidence_file,
                request.now or datetime.now(UTC),
                generated_at,
                digest,
                (rt.names.page, rt.names.data),
            )
        return ModelFactoryFloorPublishResult(
            published=(rt.names.page, rt.names.data),
            generated_at=generated_at,
            data_sha256=digest,
            serve_url=overlay.serve_url,
            refreshed=refreshed,
        )

    def _refresh(self, rt: ModelFactoryFloorPublishRuntime, home: Path) -> None:
        argv = [sys.executable, "-m", rt.refresh.module, "--home", str(home)]
        done = self._run(argv, None, rt.refresh.timeout_seconds)
        if done.returncode != 0:
            tail = done.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
            raise PublishRefusedError(
                f"the refresh {rt.refresh.module} exited {done.returncode}: {' | '.join(tail)}"
            )

    @staticmethod
    def _read_record(path: Path) -> bytes:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise PublishRefusedError(
                f"the factory floor record {path} is unreadable ({type(exc).__name__}); "
                "run node_factory_floor_effect first"
            ) from exc
        try:
            record = json.loads(data)
        except ValueError as exc:
            raise PublishRefusedError(
                f"the factory floor record {path} is not JSON"
            ) from exc
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("generated_at"), str)
            or not isinstance(record.get("lines"), list)
        ):
            raise PublishRefusedError(
                f"the factory floor record {path} lacks generated_at or lines; refusing to publish it"
            )
        return data

    def _copy(
        self,
        rt: ModelFactoryFloorPublishRuntime,
        overlay: ModelFactoryFloorPublishOverlay,
        name: str,
        payload: bytes,
    ) -> None:
        argv = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={rt.transport.connect_timeout_seconds}",
            "--",
            overlay.ssh_target,
            remote_write_command(overlay.remote_dir, name),
        ]
        done = self._run(argv, payload, rt.transport.transfer_timeout_seconds)
        if done.returncode != 0:
            tail = done.stderr.decode("utf-8", "replace").strip().splitlines()[-2:]
            raise PublishRefusedError(
                f"publishing {name} failed with exit {done.returncode}: {' | '.join(tail)}; "
                "the previous file on the server is untouched"
            )

    @staticmethod
    def _write_evidence(
        path: Path,
        now: datetime,
        generated_at: str,
        digest: str,
        names: tuple[str, ...],
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "published_at": now.astimezone(UTC).isoformat(),
                    "record_generated_at": generated_at,
                    "data_sha256": digest,
                    "published": list(names),
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
