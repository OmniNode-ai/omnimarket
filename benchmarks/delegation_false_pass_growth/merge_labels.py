# ruff: noqa: SIM115, E741, E402, T201
"""OMN-20032: merge per-wave rater outputs with the EV.4 labels into one label file per rater (lab side, keeps reasons).
usage: python3 merge_labels.py <outdir> <ev4_labels_<tag>.jsonl> <tag> <wave_dir> [<wave_dir> ...]
Each wave_dir holds items_index.json and labels_<tag>.json (blind id -> {label, reason})."""

import json
import sys

out, ev4, tag, waves = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
rows = {}
for l in open(ev4):
    d = json.loads(l)
    rows[d["correlation_id"]] = {
        "correlation_id": d["correlation_id"],
        "attempt_index": d["attempt_index"],
        "label": d["label"],
        "stratum": d["stratum"],
        "origin": "ev4",
        "reason": d["computed_facts"].get("rater_reason", ""),
    }
for w in waves:
    idx = {i["id"]: i for i in json.load(open(f"{w}/items_index.json"))}
    lab = json.load(open(f"{w}/labels_{tag}.json"))
    for k, a in lab.items():
        i = idx[k]
        rows[i["correlation_id"]] = {
            "correlation_id": i["correlation_id"],
            "attempt_index": i["attempt_index"],
            "label": a["label"],
            "stratum": i["stratum"],
            "origin": w.rstrip("/").split("/")[-1],
            "reason": a.get("reason", ""),
        }
with open(f"{out}/labels_{tag}.jsonl", "w") as fh:
    for r in sorted(rows.values(), key=lambda r: r["correlation_id"]):
        fh.write(json.dumps(r) + "\n")
from collections import Counter

print(tag, len(rows), Counter(r["label"] for r in rows.values()))
