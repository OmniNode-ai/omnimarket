# ruff: noqa: SIM115, E402, T201
import glob
import json
import re
import sys

S = sys.argv[1]
tag = sys.argv[2]
idx = {i["id"]: i for i in json.load(open(f"{S}/items_index.json"))}
out = {}
bad = []
cost = 0
for f in sorted(glob.glob(f"{S}/grades/{tag}_*.json")):
    j = json.load(open(f))
    cost += j.get("total_cost_usd", 0)
    t = j["result"].strip()
    m = re.search(r"\[.*\]", t, re.S)
    try:
        arr = json.loads(m.group(0))
    except Exception:
        bad.append(f)
        continue
    for a in arr:
        out[a["id"]] = a
missing = [i for i in idx if i not in out]
print(
    tag, "graded", len(out), "missing", len(missing), "bad", bad, "cost", round(cost, 2)
)
json.dump(out, open(f"{S}/labels_{tag}.json", "w"), indent=1)
from collections import Counter

print(Counter(a["label"] for a in out.values()))
