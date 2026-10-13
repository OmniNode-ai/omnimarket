# ruff: noqa: SIM115, E741, E402, T201
"""OMN-20032: turn the in-memory generation records into content rows (lab side) and candidate metadata lines (content-free).
Only accepted local Qwen answers enter the pool; a run that failed its gate is not a candidate.

Deployment facts come from the environment (OMN-20935): ONEX_TENANT_ID names the tenant the rows
belong to (required), and MEM_TO_CONTENT_MODEL_SUFFIXES is an optional JSON object mapping
"<overlay>|<backend>" (or "<overlay>|*" for any backend) to the suffix appended to the model name
of a run served by a distinct host."""

import csv
import glob
import hashlib
import json
import os
import sys

HOUSE = os.environ.get("ONEX_TENANT_ID", "").strip()
if not HOUSE:
    sys.exit(
        "ONEX_TENANT_ID is not configured: set it to the tenant id the content rows belong to"
    )
SUFFIXES = json.loads(os.environ.get("MEM_TO_CONTENT_MODEL_SUFFIXES", "{}"))
seen = set()
content = []
meta = []
for f in sorted(glob.glob("/tmp/omn20032/genmem_*.jsonl")):
    for l in open(f):
        d = json.loads(l)
        cid = d.get("correlation_id")
        if (
            not cid
            or cid in seen
            or d.get("status") != "completed"
            or not d.get("quality_gate_passed")
            or not d.get("response_text")
        ):
            continue
        if (
            not (d.get("model_name") or "").startswith("Qwen3.8-27B")
            or set(d.get("tiers") or []) != {"local"}
            or (d.get("cost_usd") or 0) != 0
        ):
            continue
        seen.add(cid)
        ov = d.get("overlay", "dev.local.bifrost.yaml")
        be = (d.get("backends") or [""])[-1]
        suffix = SUFFIXES.get(f"{ov}|{be}") or SUFFIXES.get(f"{ov}|*") or ""
        d["model_name"] = d["model_name"] + suffix
        content.append(
            {
                "correlation_id": cid,
                "task_type": d["cls"],
                "model_name": d["model_name"],
                "delegated_to": d["model_name"],
                "quality_gate_passed": True,
                "quality_gates_failed_jsonb": d.get("failed_gates") or [],
                "quality_gate_detail": "",
                "attempt_history": d.get("attempts") or [],
                "prompt_text": d["prompt_text"],
                "response_text": d["response_text"],
                "created_at": "2026-09-30T19:00:00+00:00",
            }
        )
        meta.append(
            [
                cid,
                0,
                HOUSE,
                d["cls"],
                "accepted",
                d["model_name"],
                d["model_name"],
                "true",
                "OMN-20032",
                "2026-09-30T19:00:00Z",
                len(d["prompt_text"]),
                len(d["response_text"]),
                hashlib.md5(d["prompt_text"].encode()).hexdigest()[:12],
            ]
        )
with open("/tmp/omn20032/content_mem.jsonl", "w") as fh:
    for c in content:
        fh.write(json.dumps(c) + "\n")
with open("/tmp/omn20032/meta_mem.tsv", "w") as fh:
    csv.writer(fh, delimiter="\t", lineterminator="\n").writerows(meta)
json.dump(sorted(seen), open("/tmp/omn20032/generated_mem_ids.json", "w"))
from collections import Counter

print(len(content), Counter(c["task_type"] for c in content))
