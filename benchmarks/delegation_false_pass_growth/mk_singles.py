# ruff: noqa: SIM115, T201
import glob
import json
import os
import re
import sys

W = sys.argv[1]
tag = sys.argv[2]
idx = {i["id"] for i in json.load(open(f"{W}/items_index.json"))}
have = (
    json.load(open(f"{W}/labels_{tag}.json"))
    if os.path.exists(f"{W}/labels_{tag}.json")
    else {}
)
miss = sorted(idx - set(have))
chunks = {}
for f in sorted(glob.glob(f"{W}/batches/*.txt")):
    t = open(f).read()
    for m in re.finditer(r"=== ITEM (i[0-9a-f]+) .*?=== END ITEM \1 ===\n", t, re.S):
        chunks[m.group(1)] = m.group(0)
os.makedirs(f"{W}/batches_single_{tag}", exist_ok=True)
for i in miss:
    open(f"{W}/batches_single_{tag}/s_{i}.txt", "w").write(
        "Label these 1 items per the rubric. Return only the JSON array.\n\n"
        + chunks[i]
        + "\n"
    )
print(tag, "missing", len(miss))
