# ruff: noqa: SIM115
"""OMN-20032 generation runner, in-memory form. Local-only by construction: the routing file holds the local tier only,
the overlay binds only local-coder and local-heavy-reasoning, cloud keys are removed from the child environment, and the bus is in-memory
so nothing is written to a lab database. Writes one lab-side record per run (content stays on the lab).
usage: OFFSET=n python gen_runner4.py <class> <runs> <concurrency>"""

import json
import os
import random
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

cls, runs_total, conc = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
maxlen = 30000
prompts = [
    p
    for p in json.load(open(f"/tmp/omn20032/prompts_{cls}.json"))
    if len(p["prompt"]) <= maxlen
]
random.Random("omn-20032-" + cls).shuffle(prompts)
off = int(os.environ.get("OFFSET", "0"))
jobs = [(i + off, prompts[(i + off) % len(prompts)]) for i in range(runs_total)]
log = open(f"/tmp/omn20032/genmem_{cls}.jsonl", "a")
env = dict(
    os.environ,
    BIFROST_CONTRACT_PATH="/app/.venv/lib/python3.12/site-packages/omnimarket/configs/bifrost_delegation.yaml",
    BIFROST_OVERLAY_PATH=os.environ.get(
        "OVERLAY", "/tmp/omn20032/dev.local.bifrost.yaml"
    ),
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
    i, p = job
    t = time.time()
    cmd = [
        "onex",
        "delegate",
        p["prompt"],
        "--task-type",
        cls,
        "--bus",
        "inmemory",
        "--locus",
        "in-process",
        "--json",
        "--state-root",
        f"/tmp/omn20032/statem_{cls}_{off}_{i % conc}",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=700, env=env)
        rc, out = r.returncode, r.stdout
    except subprocess.TimeoutExpired:
        rc, out = 124, ""
    rec = {
        "cls": cls,
        "run": i,
        "phash": p["h"],
        "rc": rc,
        "overlay": os.path.basename(env["BIFROST_OVERLAY_PATH"]),
        "sec": round(time.time() - t, 1),
    }
    try:
        res = json.loads(out.strip().splitlines()[-1])["result"]
        att = res.get("attempts") or []
        rec.update(
            correlation_id=res.get("correlation_id"),
            status=res.get("status"),
            quality_gate_passed=bool(res.get("quality_gate_passed")),
            model_name=res.get("model_name") or (att[-1]["model_id"] if att else ""),
            backends=[a.get("backend_id") for a in att],
            tiers=[a.get("tier") for a in att],
            escalation_count=res.get("escalation_count"),
            cost_usd=(res.get("metrics") or {}).get("cost_usd"),
            failed_gates=res.get("quality_gates_failed"),
            error_message=(res.get("error_message") or "")[:200],
            prompt_text=res.get("prompt_text") or p["prompt"],
            response_text=res.get("response") or "",
            attempts=att,
        )
    except Exception as e:
        rec["parse_error"] = str(e)[:100]
    log.write(json.dumps(rec) + "\n")
    log.flush()


with ThreadPoolExecutor(conc) as ex:
    list(ex.map(one, jobs))
