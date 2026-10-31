# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refresh the factory floor's inputs and record, then publish the page and its data to the serving host.

One run executes the contract's input steps in order (the lab host advertisements, the friction report, the
process-health series, and last the factory floor record itself), then copies the page, the status of every
step, the process-health series when there is one, and the record to the directory a static server serves, over
ssh, one file at a time: each is written to a temporary name and renamed into place, so a reader never sees a
partial file and a failed copy leaves the previous file untouched. A step that is not required may fail; the
status file says so and the page shows that source as unread rather than empty. The host, the directory, the
URL and the bus lane are deployment facts from the overlay; the paths, names and time limits are the contract's.
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

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelDeclaredLines,
    ModelFactoryFloorPublishContract,
    ModelFactoryFloorPublishOverlay,
    ModelFactoryFloorPublishRequest,
    ModelFactoryFloorPublishResult,
    ModelFactoryFloorPublishRuntime,
    ModelPublishStep,
    ModelPublishStepResult,
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
            list(argv), input=stdin, capture_output=True, timeout=timeout, check=False
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

    The bytes go to a dot-named temporary file in the final file's own directory (``name`` may carry
    sub-directories, which are created) and are renamed over the final name; when the write fails the temporary
    file is removed and the previous final file is left as it was.
    """
    final = PurePosixPath(remote_dir) / name
    tmp = final.parent / f".{final.name}.tmp"
    q_dir, q_final, q_tmp = (shlex.quote(str(p)) for p in (final.parent, final, tmp))
    return (
        f"umask 022 && mkdir -p {q_dir} && "
        f"{{ cat > {q_tmp} && mv -f {q_tmp} {q_final}; }} || {{ rm -f {q_tmp}; exit 1; }}"
    )


def write_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` through a temporary file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _tail(raw: bytes) -> str:
    return " | ".join(raw.decode("utf-8", "replace").strip().splitlines()[-3:])


def copy_to_server(
    run: Runner,
    rt: ModelFactoryFloorPublishRuntime,
    overlay: ModelFactoryFloorPublishOverlay,
    name: str,
    payload: bytes,
) -> None:
    """Store ``payload`` as ``name`` under the serving host's directory over ssh; a failure leaves the old file."""
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
    done = run(argv, payload, rt.transport.transfer_timeout_seconds)
    if done.returncode != 0:
        raise PublishRefusedError(
            f"publishing {name} failed with exit {done.returncode}: {_tail(done.stderr)}; "
            "the previous file on the server is untouched"
        )


class HandlerFactoryFloorPublish:
    """Boundary of the publish: the input steps, the reads and the copies happen here."""

    def __init__(self, runner: Runner = run_process) -> None:
        self._run = runner

    def handle(
        self,
        request: ModelFactoryFloorPublishRequest,
        *,
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
        now = request.now or datetime.now(UTC)

        results: list[ModelPublishStepResult] = []
        if not request.skip_refresh:
            for step in rt.steps:
                result = self._step(step, home, overlay, rt.plugin_root_env, env)
                results.append(result)
                if result.status == "failed" and step.required:
                    raise PublishRefusedError(
                        f"the required step {step.name} failed ({result.detail}): {result.diagnostic}"
                    )

        data = self._read_record(home / rt.source.relpath)
        record = json.loads(data)
        generated_at = str(record["generated_at"])
        results = self._flag_undrawn(
            results, rt, self._declared_fragments(rt, env), record["lines"]
        )
        page = (NODE_DIR / rt.page_relpath).read_bytes()
        floor_page = (NODE_DIR / rt.floor_page_relpath).read_bytes()
        health = self._read_process_health(home / rt.process_health_relpath)
        delegation = self._read_delegation(home / rt.delegation_relpath)
        status = json.dumps(
            {
                "published_at": now.astimezone(UTC).isoformat(),
                "steps": [r.model_dump(exclude={"diagnostic"}) for r in results],
                "process_health": "published" if health is not None else "absent",
                "delegation": "published" if delegation is not None else "absent",
                "lines_declared": self._declared_lines(rt, env),
            },
            sort_keys=True,
        ).encode("utf-8")

        # The record goes last: a reader that gets the new record always finds a page that reads it.
        files: list[tuple[str, bytes]] = [
            (rt.names.floor_page, floor_page),
            (rt.names.page, page),
            (rt.names.status, status),
        ]
        if health is not None:
            files.append((rt.names.process_health, health))
        if delegation is not None:
            files.append((rt.names.delegation, delegation))
        files.append((rt.names.data, data))
        for name, payload in files:
            self._copy(rt, overlay, name, payload)

        digest = hashlib.sha256(data).hexdigest()
        if request.state_dir:
            self._write_evidence(
                Path(request.state_dir) / rt.evidence_file,
                now,
                generated_at,
                digest,
                tuple(name for name, _ in files),
            )
        return ModelFactoryFloorPublishResult(
            published=tuple(name for name, _ in files),
            generated_at=generated_at,
            data_sha256=digest,
            serve_url=overlay.serve_url,
            steps=tuple(results),
            process_health_published=health is not None,
            delegation_published=delegation is not None,
        )

    def _step(
        self,
        step: ModelPublishStep,
        home: Path,
        overlay: ModelFactoryFloorPublishOverlay,
        plugin_root_env: str,
        env: Mapping[str, str],
    ) -> ModelPublishStepResult:
        def fill(values: Sequence[str]) -> list[str]:
            plugin_root = env.get(plugin_root_env, "").strip()
            if not plugin_root and any("{plugin_root}" in v for v in values):
                raise PublishRefusedError(
                    f"{plugin_root_env} is not set (fail fast, no default): export the plugin root"
                )
            return [
                v.replace("{home}", str(home))
                .replace("{bus_lane}", overlay.bus_lane)
                .replace("{plugin_root}", plugin_root)
                for v in values
            ]

        def failed(detail: str, diagnostic: str = "") -> ModelPublishStepResult:
            return ModelPublishStepResult(
                name=step.name, status="failed", detail=detail, diagnostic=diagnostic
            )

        try:
            if step.kind == "copy":
                if step.source_relpath is None or step.output_relpath is None:
                    raise ValueError("a copy step names a source and an output")
                raw = (home / step.source_relpath).read_bytes()
                document = json.loads(raw)
                for key in step.select:
                    if not isinstance(document, dict) or key not in document:
                        raise ValueError(f"the source has no {'.'.join(step.select)}")
                    document = document[key]
                if step.select:
                    raw = json.dumps(document, sort_keys=True).encode("utf-8")
                write_atomic(home / step.output_relpath, raw)
                return ModelPublishStepResult(name=step.name, status="ok")
            if step.kind == "module":
                if step.module is None:
                    raise ValueError("a module step names a module")
                argv = [sys.executable, "-m", step.module, *fill(step.args)]
            else:
                argv = fill(step.argv)
            done = self._run(argv, None, step.timeout_seconds)
        except (OSError, ValueError, PublishRefusedError) as exc:
            return failed(type(exc).__name__, str(exc))
        if done.returncode != 0:
            return failed(f"exit {done.returncode}", _tail(done.stderr))
        if step.kind == "capture":
            if step.output_relpath is None:
                return failed("ValueError", "a capture step names an output")
            try:
                parsed = json.loads(done.stdout)
            except ValueError:
                return failed("output is not JSON", _tail(done.stdout))
            if not isinstance(parsed, list):
                return failed("output is not a JSON list")
            write_atomic(home / step.output_relpath, done.stdout)
        return ModelPublishStepResult(name=step.name, status="ok")

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

    @staticmethod
    def _read_process_health(path: Path) -> bytes | None:
        """The process-health series, or ``None`` when absent or not a series (the page then says unread)."""
        try:
            data = path.read_bytes()
            record = json.loads(data)
        except (OSError, ValueError):
            return None
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("generated_at"), str)
            or not isinstance(record.get("days"), list)
            or not isinstance(record.get("summary"), dict)
        ):
            return None
        return data

    @staticmethod
    def _read_delegation(path: Path) -> bytes | None:
        """The delegation counts, or ``None`` when absent or not that shape (the page then draws the card unread)."""
        try:
            data = path.read_bytes()
            record = json.loads(data)
        except (OSError, ValueError):
            return None
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("generated_at"), str)
            or not isinstance(record.get("outcomes"), list)
        ):
            return None
        return data

    @staticmethod
    def _declared_fragments(
        rt: ModelFactoryFloorPublishRuntime, env: Mapping[str, str]
    ) -> tuple[str, ...] | None:
        """The plan texts the factory floor effect's overlay declares as lines, or ``None`` when that is unread."""
        try:
            declared = load_structured_overlay(
                rt.lines_overlay.overlay_id,
                ModelDeclaredLines,
                env,
                file_env=rt.lines_overlay.file_env,
            )
        except NodeOverlayError:
            return None
        return declared.plan_path_contains

    @staticmethod
    def _declared_lines(
        rt: ModelFactoryFloorPublishRuntime, env: Mapping[str, str]
    ) -> dict[str, object]:
        """Which plans the factory floor effect's overlay declares as lines, or why that is unread."""
        tokens = HandlerFactoryFloorPublish._declared_fragments(rt, env)
        if tokens is None:
            return {
                "state": "unread",
                "reason": "the factory floor effect's overlay is missing or invalid",
            }
        return {"state": "read", "plan_path_contains": list(tokens)}

    @staticmethod
    def _flag_undrawn(
        results: list[ModelPublishStepResult],
        rt: ModelFactoryFloorPublishRuntime,
        declared: tuple[str, ...] | None,
        lines: Sequence[object],
    ) -> list[ModelPublishStepResult]:
        """Mark the record step not ok, naming the declared lines the record does not draw."""
        drawn = [line for line in lines if isinstance(line, dict)]
        missing = [
            token
            for token in declared or ()
            if not any(token in str(line.get("plan_path", "")) for line in drawn)
        ]
        if not missing:
            return results
        flagged = ModelPublishStepResult(
            name=rt.record_step,
            status="failed",
            detail="declared lines not drawn: " + ", ".join(missing),
        )
        if any(r.name == rt.record_step for r in results):
            return [flagged if r.name == rt.record_step else r for r in results]
        return [*results, flagged]

    def _copy(
        self,
        rt: ModelFactoryFloorPublishRuntime,
        overlay: ModelFactoryFloorPublishOverlay,
        name: str,
        payload: bytes,
    ) -> None:
        copy_to_server(self._run, rt, overlay, name, payload)

    @staticmethod
    def _write_evidence(
        path: Path,
        now: datetime,
        generated_at: str,
        digest: str,
        names: tuple[str, ...],
    ) -> None:
        write_atomic(
            path,
            (
                json.dumps(
                    {
                        "published_at": now.astimezone(UTC).isoformat(),
                        "record_generated_at": generated_at,
                        "data_sha256": digest,
                        "published": list(names),
                    },
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8"),
        )


__all__ = [
    "NODE_DIR",
    "HandlerFactoryFloorPublish",
    "PublishRefusedError",
    "copy_to_server",
    "load_runtime",
    "remote_write_command",
    "run_process",
]
