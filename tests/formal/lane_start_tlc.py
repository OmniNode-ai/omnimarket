# SPDX-License-Identifier: MIT
"""Run TLC over the lane start (LaneStart.tla) model matrix and write docs/formal/lane_start/results/.

One ``<config>.out`` per config and ``SUMMARY.txt`` with the sha256 of the model, each
config and each config's output. ``Model.cfg`` must exit 0 with "No error has been
found"; each mutation must report a counterexample to the one property its config
breaks; each witness must violate its invariant, which shows the state is reachable.
``tests/formal/test_lane_start_model.py`` reads SUMMARY.txt and fails when the
model or a config changes without a re-run.

Usage: ``python3 tests/formal/lane_start_tlc.py`` (always the whole matrix).

tla2tools.jar: ``TLA2TOOLS_JAR`` when set, else the pinned release download (TLC 2.19,
v1.7.4); the sha256 is checked either way. Java: ``JAVA``, then ``java`` on PATH.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

MODEL_DIR = Path(__file__).resolve().parents[2] / "docs" / "formal" / "lane_start"
MODEL = "LaneStart.tla"
JAR_SHA = "936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88"
JAR_URL = "https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar"
MATRIX = (
    "Model",
    "obs_model",
    "mut_retry_any",
    "mut_retry_after_started",
    "mut_retry_inflight",
    "mut_engine_before_claim",
    "mut_no_single_closer",
    "mut_terminal_without_claim",
    "mut_observe_claims",
    "mut_no_reaper",
    "mut_stopper_closes_prestart",
    "haz_late_start",
    "asbuilt_stopper",
    "asbuilt_stopper_liveness",
    "asbuilt_outcomes",
    "chk_unresolved_stays_dead",
    "wit_NeverStarted",
    "wit_NeverClosedByLane",
    "wit_NeverClosedByLeader",
    "wit_NeverClosedByStopper",
    "wit_NeverStartFailed",
    "wit_NeverRetriedAfterFailedStart",
    "wit_NeverUnresolvedRan",
    "wit_NeverLeaderStatus",
    "wit_NeverRetryAfterRefused",
    "wit_NeverThirdStarted",
    "wit_NeverStopperAfterDeadLeaderRan",
    "wit_NeverObserved",
)
# What varies between runs of the same model: clock times, temp paths, the machine.
VOLATILE = re.compile(
    r"^(TLC2 Version|Warning: Please run|\(Use the -nowarning|Running breadth-first|"
    r"Starting\.\.\.|Finished in|Finished computing|Progress\(|Computed \d+ initial|"
    r"Finished checking temporal|Checking \d+ branches)"
)
STANDARD_MODULE = re.compile(
    r"[^\s(]*/(Naturals|Integers|Sequences|FiniteSets|TLC)\.tla"
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _jar(work: Path) -> Path:
    jar = work / "tla2tools.jar"
    source = os.environ.get("TLA2TOOLS_JAR")
    if source:
        shutil.copy(source, jar)
    else:
        urllib.request.urlretrieve(JAR_URL, jar)
    if _sha(jar) != JAR_SHA:
        raise SystemExit("tla2tools.jar does not match the pinned sha256")
    return jar


def _java() -> str:
    found = os.environ.get("JAVA") or shutil.which("java")
    if not found:
        raise SystemExit("no working java")
    return found


def _tlc(java: str, jar: Path, work: Path, name: str, model_dir: Path) -> str:
    proc = subprocess.run(
        [
            java, "-XX:+UseParallelGC", f"-Xmx{os.environ.get('HEAP', '2g')}",
            "-cp", str(jar), "tlc2.TLC", "-config", f"{name}.cfg",
            "-workers", os.environ.get("WORKERS", "2"),
            "-metadir", str(work / "states" / name), "-cleanup", MODEL,
        ],
        cwd=model_dir, capture_output=True, text=True, check=False,
    )  # fmt: skip
    banner = re.search(r"^TLC2 Version .*$", proc.stdout + proc.stderr, re.MULTILINE)
    kept = [
        STANDARD_MODULE.sub(r"\1.tla", line).replace(f"{model_dir}/", "")
        for line in (proc.stdout + proc.stderr).splitlines()
        if not VOLATILE.match(line)
    ]
    (model_dir / "results" / f"{name}.out").write_text(
        "\n".join(kept) + f"\nexit={proc.returncode}\n", encoding="utf-8"
    )
    return banner.group(0) if banner else "unknown"


def _verdict(name: str, model_dir: Path) -> str:
    out = (model_dir / "results" / f"{name}.out").read_text(encoding="utf-8")
    if re.search(r"^exit=0$", out, re.MULTILINE) and "No error has been found" in out:
        return "PASS"
    invariant = re.search(r"Invariant ([A-Za-z0-9_]+) is violated", out)
    if invariant:
        return f"VIOLATED {invariant.group(1)}"
    if "Temporal properties were violated" in out:
        cfg = (model_dir / f"{name}.cfg").read_text(encoding="utf-8")
        prop = re.search(r"^PROPERTIES (\w+)", cfg, re.MULTILINE)
        return f"VIOLATED {prop.group(1) if prop else '?'}"
    return "ERROR"


def main() -> int:
    model_dir = MODEL_DIR
    java = _java()
    (model_dir / "results").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        jar = _jar(work)
        lines = ["tlc: pending", f"model: {MODEL} sha256={_sha(model_dir / MODEL)}"]
        for name in MATRIX:
            banner = _tlc(java, jar, work, name, model_dir)
            lines[0] = f"tlc: {banner} jar_sha256={JAR_SHA}"
            out = model_dir / "results" / f"{name}.out"
            lines.append(
                f"{name} cfg_sha256={_sha(model_dir / f'{name}.cfg')} "
                f"out_sha256={_sha(out)} result={_verdict(name, model_dir)}"
            )
    summary = model_dir / "results" / "SUMMARY.txt"
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sys.stdout.write(summary.read_text(encoding="utf-8"))
    # A run on a host whose files the caller cannot read prints them, framed, so the log carries them.
    for name in MATRIX:
        out = model_dir / "results" / f"{name}.out"
        sys.stdout.write(
            f"RESULT-FILE {name}.out\n{out.read_text(encoding='utf-8')}END-RESULT-FILE\n"
        )
    errors = [name for name in MATRIX if _verdict(name, model_dir) == "ERROR"]
    if errors:
        sys.stderr.write(f"TLC gave no verdict for: {' '.join(errors)}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
