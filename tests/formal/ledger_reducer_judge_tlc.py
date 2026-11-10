# SPDX-License-Identifier: MIT
"""Run TLC over the ledger_reducer_judge model matrix and write formal/ledger_reducer_judge/results/.

One ``<config>.out`` per config and ``SUMMARY.txt`` with the sha256 of the model, each
config and each config's output. Each config has an expected outcome: the adopted
configs must exit 0 with "No error has been found"; each control must report a
counterexample to the one invariant or property it is named for; each witness must
report a counterexample to its witness invariant. The exit status is 0 only when every
config matched. ``tests/formal/test_ledger_reducer_judge_model.py`` reads SUMMARY.txt and
fails when the model or a config changes without a re-run.

Usage: ``python3 tests/formal/ledger_reducer_judge_tlc.py`` (always the whole matrix, on
a lab host).

tla2tools.jar: ``TLA2TOOLS_JAR`` when set, else the pinned release download (TLC 2.19,
v1.7.4); the sha256 is checked either way. Java: ``JAVA``, then ``java`` on PATH, then
the eclipse-temurin:17-jre-alpine image through the container runtime. ``TLC_JOBS``
(configs at once, default 4), ``TLC_WORKERS`` (per config, default 4) and ``TLC_HEAP``
(default 8g) size the run.
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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MODEL_DIR = Path(__file__).resolve().parents[2] / "formal" / "ledger_reducer_judge"
MODEL = "LedgerReducerJudge.tla"
JAR_SHA = "936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88"
JAR_URL = "https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar"
# config -> PASS, or the invariant / property TLC must name as violated
MATRIX = {
    "LedgerReducerJudge_adopted": "PASS",
    "LedgerReducerJudge_adopted_conflict": "PASS",
    "C1_receipt_before_commit": "ReceiptTruth",
    "C2_seq_outside_tx": "SeqGapless",
    "C3_guard_lagging_projection": "GuardSeesAllPriorRows",
    "C3b_guard_lagging_projection_effect": "GuardEffective",
    "C4_no_lock": "PrincipalInterleavingSerial",
    "C5_relay_unordered": "SeqMatchesEventOrder",
    "C6_no_dedup": "NoDuplicateRow",
    "C7_commit_offset_first": "EveryRequestAnswered",
    "W1_both_rows_committed": "WitnessBothRowsCommitted",
    "W2_duplicate_after_crash": "WitnessDuplicateReceipt",
    "W3_duplicate_after_replay": "WitnessDuplicateReceipt",
    "W4_refused_receipt": "WitnessRefusedReceipt",
    "W5_event_republished": "WitnessEventRepublished",
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _jar() -> Path:
    jar = MODEL_DIR / "tla2tools.jar"
    if not jar.exists():
        source = os.environ.get("TLA2TOOLS_JAR")
        if source:
            shutil.copy(source, jar)
        else:
            urllib.request.urlretrieve(JAR_URL, jar)
    if _sha(jar) != JAR_SHA:
        raise SystemExit("tla2tools.jar does not match the pinned sha256")
    return jar


def _java() -> list[str]:
    if os.environ.get("JAVA"):
        return [os.environ["JAVA"]]
    if shutil.which("java"):
        return ["java"]
    if shutil.which("docker"):
        user = f"{os.getuid()}:{os.getgid()}"
        mount = f"{MODEL_DIR}:/work"
        return [
            "docker", "run", "--rm", "--user", user, "-v", mount, "-w", "/work",
            "eclipse-temurin:17-jre-alpine", "java",
        ]  # fmt: skip
    raise SystemExit("no working java")


def _tlc(java: list[str], name: str) -> None:
    heap = os.environ.get("TLC_HEAP", "8g")
    workers = os.environ.get("TLC_WORKERS", "4")
    with tempfile.TemporaryDirectory() as meta:
        proc = subprocess.run(
            [
                *java, "-XX:+UseParallelGC", f"-Xmx{heap}", "-cp", "tla2tools.jar",
                "tlc2.TLC", "-config", f"{name}.cfg", "-workers", workers,
                "-metadir", meta, "-cleanup", MODEL,
            ],
            cwd=MODEL_DIR, capture_output=True, text=True, check=False,
        )  # fmt: skip
    text = (
        (proc.stdout + proc.stderr).replace(f"{MODEL_DIR}/", "").replace("/work/", "")
    )
    (MODEL_DIR / "results" / f"{name}.out").write_text(
        f"{text}exit={proc.returncode}\n", encoding="utf-8"
    )


def _got(name: str) -> str:
    out = (MODEL_DIR / "results" / f"{name}.out").read_text(encoding="utf-8")
    if re.search(r"^exit=0$", out, re.MULTILINE) and "No error has been found" in out:
        return "PASS"
    invariant = re.search(r"Invariant ([A-Za-z0-9_]+) is violated", out)
    if invariant:
        return invariant.group(1)
    if "Temporal properties were violated" in out:
        cfg = (MODEL_DIR / f"{name}.cfg").read_text(encoding="utf-8")
        prop = re.search(r"^PROPERTY (\w+)", cfg, re.MULTILINE)
        return prop.group(1) if prop else "?"
    return "ERROR"


def _row(name: str) -> str:
    got = _got(name)
    verdict = "PASS" if got == "PASS" else f"VIOLATED {got}"
    ok = "OK" if got == MATRIX[name] else f"MISMATCH(expected {MATRIX[name]})"
    return (
        f"{name} cfg_sha256={_sha(MODEL_DIR / f'{name}.cfg')} "
        f"out_sha256={_sha(MODEL_DIR / 'results' / f'{name}.out')} "
        f"result={verdict} {ok}"
    )


def main() -> int:
    _jar()
    java = _java()
    (MODEL_DIR / "results").mkdir(exist_ok=True)
    with ThreadPoolExecutor(max_workers=int(os.environ.get("TLC_JOBS", "4"))) as pool:
        list(pool.map(lambda name: _tlc(java, name), MATRIX))
    lines = [f"model: {MODEL} sha256={_sha(MODEL_DIR / MODEL)}"]
    lines += [_row(name) for name in MATRIX]
    summary = MODEL_DIR / "results" / "SUMMARY.txt"
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sys.stdout.write(summary.read_text(encoding="utf-8"))
    return 0 if all(line.endswith(" OK") for line in lines[1:]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
