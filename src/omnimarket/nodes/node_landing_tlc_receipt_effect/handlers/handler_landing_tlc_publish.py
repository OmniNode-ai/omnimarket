# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the TLC approval receipt for the controller's decision pin and pack it for release."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tarfile

from ..models import ModelLandingTlcPublishRequest, ModelLandingTlcPublishResult
from .handler_landing_tlc_gate import (
    PIN_DIR_PREFIX,
    TAG_PREFIX,
    check_tlc_receipt,
    load_gate,
)


def _refused(*reasons: str, pin: str = "") -> ModelLandingTlcPublishResult:
    return ModelLandingTlcPublishResult(
        status="refused", pin=pin, reasons=tuple(reasons)
    )


class HandlerLandingTlcPublish:
    """The controller's gate builds and judges the receipt; this handler feeds it and packs it."""

    def handle(
        self, request: ModelLandingTlcPublishRequest
    ) -> ModelLandingTlcPublishResult:
        try:
            pin = str(
                json.loads(request.pin_file.read_text(encoding="utf-8"))["commit"]
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return _refused(
                f"cannot read the decision pin from {request.pin_file}: {exc}"
            )
        outs = sorted(request.results_dir.glob("*.out"))
        if not outs:
            return _refused(
                f"no .out file in {request.results_dir}: the model check did not run",
                pin=pin,
            )
        if not request.model.is_file():
            return _refused(f"the model {request.model} is not a file", pin=pin)
        try:
            gate = load_gate(request.gate_script)
        except (ImportError, OSError, SyntaxError) as exc:
            return _refused(str(exc), pin=pin)

        name = f"{PIN_DIR_PREFIX}{pin[:8]}"
        directory = request.out_dir / name
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
        for out in outs:
            shutil.copy2(out, directory / out.name)
        model = directory / request.model.name
        shutil.copy2(request.model, model)
        build = subprocess.run(
            [
                sys.executable,
                "-I",
                str(request.gate_script),
                "build",
                "tlc",
                "--results-dir",
                str(directory),
                "--model",
                str(model),
                "--t3-commit",
                pin,
                "--host",
                request.host,
                "--out",
                str(directory / "tlc.json"),
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if build.returncode:
            return _refused(
                f"the gate could not build the receipt: {build.stderr.strip()}", pin=pin
            )
        reasons = check_tlc_receipt(gate, directory, pin)
        if reasons:
            return _refused(*reasons, pin=pin)

        archive = request.out_dir / f"{name}.tar.gz"
        with tarfile.open(archive, "w:gz") as packed:
            packed.add(directory, arcname=name)
        model8 = hashlib.sha256(request.model.read_bytes()).hexdigest()[:8]
        return ModelLandingTlcPublishResult(
            status="published",
            pin=pin,
            tag=f"{TAG_PREFIX}{pin[:8]}-{model8}",
            archive=archive,
        )
