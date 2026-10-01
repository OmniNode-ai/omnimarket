# ruff: noqa: SIM115, E741, E402, T201, B007
"""OMN-20032: third blind rater, gpt-oss-120b on the .200 planner host (local, keyless), same rubric ev4-blind-v1, one item per call.
usage: python3 rate_third.py <workdir> <sample.json> <content.jsonl> <out.json> [concurrency]
Content stays on the lab; out.json holds ids, labels and reasons on the lab host."""

import json
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

W, sample_path, content_path, out_path = sys.argv[1:5]
conc = int(sys.argv[5]) if len(sys.argv) > 5 else 2
import os

URL = os.environ["THIRD_RATER_URL"]  # url-authority-ok: lab one-shot
rubric = open(f"{W}/rubric.md").read()
sample = json.load(open(sample_path))  # list of {"id":..., "correlation_id":...}
want = {s["correlation_id"]: s["id"] for s in sample}
idx = {i["id"]: i for i in json.load(open(f"{W}/items_index_all.json"))}
content = {}
for l in open(content_path):
    d = json.loads(l)
    if d["correlation_id"] in want:
        content[d["correlation_id"]] = d


def user_text(i, c):
    return (
        f"Label these 1 items per the rubric. Return only the JSON array.\n\n=== ITEM {i['id']} (task class: {i['task_class']}) ===\n--- computed_facts ---\n{json.dumps(i['facts'])}\n"
        f"--- TASK (prompt given to the delegate) ---\n{c['prompt_text']}\n--- ANSWER (as delivered) ---\n{c['response_text']}\n=== END ITEM {i['id']} ===\n"
    )


def one(cid):
    oid = want[cid]
    i = idx[oid]
    c = content[cid]
    body = json.dumps(
        {
            "model": "gpt-oss-120b",
            "messages": [
                {"role": "system", "content": rubric},
                {"role": "user", "content": user_text(i, c)},
            ],
            "max_tokens": 8000,
        }
    ).encode()
    t = time.time()
    last = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(
                URL, data=body, headers={"content-type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=900) as r:
                j = json.loads(r.read())
            txt = j["choices"][0]["message"].get("content") or ""
            m = re.search(r"\[.*\]", txt, re.S)
            arr = json.loads(m.group(0))
            a = arr[0]
            return {
                "id": oid,
                "label": a["label"],
                "reason": a.get("reason", ""),
                "sec": round(time.time() - t, 1),
                "tokens": j.get("usage", {}).get("total_tokens"),
            }
        except Exception as e:
            last = str(e)[:200]
    return {"id": oid, "label": None, "error": last, "sec": round(time.time() - t, 1)}


with ThreadPoolExecutor(conc) as ex:
    out = list(
        ex.map(
            one, [s["correlation_id"] for s in sample if s["correlation_id"] in content]
        )
    )
json.dump({o["id"]: o for o in out}, open(out_path, "w"), indent=1)
from collections import Counter

print(
    len(out),
    Counter(o["label"] for o in out),
    "missing content",
    len(sample) - len(content),
)
