# ruff: noqa: SIM115, E741, E402, T201
"""OMN-20032: turn the in-memory generation records into content rows (lab side) and candidate metadata lines (content-free).
Only accepted local Qwen answers enter the pool; a run that failed its gate is not a candidate."""

import csv
import glob
import hashlib
import json

HOUSE = "820272f9-4aaf-5add-a2df-0af942852ab2"
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
        host202 = ov == "dev.local202b.bifrost.yaml" or (
            ov == "dev.local202.bifrost.yaml" and be == "local-heavy-reasoning"
        )
        d["model_name"] = d["model_name"] + ("@omnipc2-llamacpp" if host202 else "")
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
