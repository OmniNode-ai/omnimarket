# ruff: noqa: SIM115, E741, E402, T201, E731
"""OMN-20032: the stratified third-rater sample: per class, 20 accepted items, up to 10 labelled inadequate by the first rater and the rest adequate,
ordered by sha256(seed + correlation id); items above 40,000 characters (prompt plus answer) are left out of the draw.
usage: python3 sample_third.py <labels_opus.jsonl> <items_index_all.json> <manifest.json> <out.json>"""

import hashlib
import json
import sys
from collections import defaultdict

SEED = "omn-20032-third-rater-2026-09-30"
lab = {json.loads(l)["correlation_id"]: json.loads(l) for l in open(sys.argv[1])}
idx = {i["correlation_id"]: i for i in json.load(open(sys.argv[2]))}
man = json.load(open(sys.argv[3]))
by = defaultdict(lambda: {"inadequate": [], "adequate": []})
for it in man["items"]:
    cid = it["key"]["correlation_id"]
    if (
        "/accepted" not in it["stratum"]
        or cid not in lab
        or cid not in idx
        or idx[cid]["size"] > 40000
    ):
        continue
    l = lab[cid]["label"]
    if l in by[it["task_class"]]:
        by[it["task_class"]][l].append(cid)
order = lambda c: hashlib.sha256((SEED + c).encode()).hexdigest()
out = []
for tc, d in sorted(by.items()):
    bad = sorted(d["inadequate"], key=order)
    good = sorted(d["adequate"], key=order)
    n_bad = min(10, len(bad))
    n_good = min(20 - n_bad, len(good))
    n_bad = min(len(bad), 20 - n_good)
    for c in bad[:n_bad] + good[:n_good]:
        out.append(
            {
                "id": idx[c]["id"],
                "correlation_id": c,
                "task_class": tc,
                "first_rater_label": lab[c]["label"],
            }
        )
json.dump(out, open(sys.argv[4], "w"), indent=1)
from collections import Counter

print(len(out), Counter((o["task_class"], o["first_rater_label"]) for o in out))
