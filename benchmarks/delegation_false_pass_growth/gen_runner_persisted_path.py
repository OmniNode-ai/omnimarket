# ruff: noqa: SIM115
"""OMN-20032 generation runner (runs inside omninode-runtime on the .201 dev lane).
Content-free log: class, prompt hash, run no, backend pin, exit code, receipt fields."""

import json
import os
import random
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

cls, runs_total, conc = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
backends = sys.argv[4].split(",")
maxlen = int(sys.argv[5]) if len(sys.argv) > 5 else 30000
prompts = [
    p
    for p in json.load(open(f"/tmp/omn20032/prompts_{cls}.json"))
    if len(p["prompt"]) <= maxlen
]
random.Random("omn-20032-" + cls).shuffle(prompts)
off = int(os.environ.get("OFFSET", "0"))
jobs = [
    (i + off, prompts[(i + off) % len(prompts)], backends[(i + off) % len(backends)])
    for i in range(runs_total)
]
log = open(f"/tmp/omn20032/gen_{cls}.jsonl", "a")
env = dict(
    os.environ,
    TASK_CLASS_CONTRACT_PATH="/tmp/omn20032/task_class_contracts.v1.yaml",
    DELEGATION_ROUTING_TIERS_PATH="/tmp/omn20032/routing_tiers_local_only.yaml",
)
for k in (
    "OPENROUTER_API_KEY",
    "LLM_GLM_API_KEY",
    "LLM_GLM_URL",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "BIFROST_VERTEX_GEMINI_ENDPOINT_URL",
    "LINEAR_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "SLACK_BOT_TOKEN",
):
    env.pop(k, None)


def one(job):
    i, p, be = job
    t = time.time()
    cmd = [
        "onex",
        "delegate",
        p["prompt"],
        "--task-type",
        cls,
        "--bus",
        "kafka",
        "--kafka-bootstrap",
        "redpanda:9092",
        "--locus",
        "in-process",
        "--ticket",
        "OMN-20032",
        "--caller-lane",
        "false-pass-100",
        "--json",
        "--state-root",
        f"/tmp/omn20032/state_{cls}_{off}_{i % conc}",
    ]
    if be != "none":
        cmd += ["--backend-id", be]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=700, env=env)
        rc, out, err = r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        rc, out, err = 124, "", "timeout"
    rec = {
        "cls": cls,
        "run": i,
        "phash": p["h"],
        "backend_pin": be,
        "rc": rc,
        "sec": round(time.time() - t, 1),
    }
    try:
        j = json.loads(out.strip().splitlines()[-1])
        res = j.get("result") or j
        rec["receipt_keys"] = sorted(j.keys())[:12]
        rec["correlation_id"] = json.dumps(j)[:0] or (
            res.get("correlation_id") if isinstance(res, dict) else None
        )
        rec["summary"] = {
            k: res.get(k)
            for k in (
                "status",
                "model",
                "backend_id",
                "quality_gate_passed",
                "accepted",
                "run_id",
            )
            if isinstance(res, dict) and k in res
        }
    except Exception as e:
        rec["parse_error"] = str(e)[:100]
        rec["err_tail"] = err[-300:]
        rec["out_head"] = out[:300]
    log.write(json.dumps(rec) + "\n")
    log.flush()
    return rec


with ThreadPoolExecutor(conc) as ex:
    list(ex.map(one, jobs))
