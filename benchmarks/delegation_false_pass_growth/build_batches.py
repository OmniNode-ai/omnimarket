# ruff: noqa: SIM115, E741, T201, C408
"""OMN-20032: build blind rater batches under rubric ev4-blind-v1, the same script facts, blind ids and batching as EV.4.
usage: python build_batches.py <workdir> <manifest.json> <skip_ids.json>
reads <workdir>/content.jsonl; writes <workdir>/items_index.json and <workdir>/batches/bNNN.txt (items not in skip_ids)."""

import hashlib
import json
import os
import re
import sys

S = sys.argv[1]
rows = [json.loads(l) for l in open(f"{S}/content.jsonl")]
m = json.load(open(sys.argv[2]))
skip = set(json.load(open(sys.argv[3])))
key = {i["key"]["correlation_id"]: i for i in m["items"]}


def facts(p, r):
    f = {}
    f["response_chars"] = len(r)
    lines = [l for l in r.splitlines() if l.strip()]
    f["response_nonblank_lines"] = len(lines)
    f["response_sentences_approx"] = len(re.findall(r"[.!?](\s|$)", r))
    reqs = re.findall(
        r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)[- ](?:short |concise |plain |numbered )?(sentences?|lines?|bullets?|bullet points?|words?|paragraphs?|items?|steps?)\b",
        p,
        flags=re.I,
    )
    f["requested_counts_in_prompt"] = sorted(
        {f"{a.lower()} {b.lower()}" for a, b in reqs}
    )[:8]
    tail = r.rstrip()[-1:] if r.strip() else ""
    f["ends_with_terminal_char"] = tail in ".!?)`]}\"'*|>" or r.rstrip().endswith("```")
    pn = set(re.findall(r"\d+(?:\.\d+)?", p))
    rn = re.findall(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])", r)
    f["numbers_in_answer_not_in_prompt"] = sorted(
        {n for n in rn if n not in pn and len(n) > 1}
    )[:25]
    ids = set(re.findall(r"`([^`\n]{2,80})`", r)) | set(
        re.findall(r"\b[\w./-]+\.(?:py|ts|tsx|yaml|yml|md|sql|json|sh)\b", r)
    )
    f["identifiers_in_answer_not_in_prompt"] = sorted(i for i in ids if i not in p)[:25]
    f["refusal_phrases"] = bool(
        re.search(
            r"\b(I (?:cannot|can't|am unable)|as an AI|I don't have access)\b", r, re.I
        )
    )
    return f


items = []
for r in rows:
    if r["correlation_id"] in skip or r["correlation_id"] not in key:
        continue
    it = key[r["correlation_id"]]
    oid = (
        "i" + hashlib.sha256(("blind" + r["correlation_id"]).encode()).hexdigest()[:10]
    )
    fx = facts(r["prompt_text"], r["response_text"])
    items.append(
        dict(
            id=oid,
            correlation_id=r["correlation_id"],
            attempt_index=it["key"]["attempt_index"],
            task_class=r["task_type"],
            stratum=it["stratum"],
            facts=fx,
            size=len(r["prompt_text"]) + len(r["response_text"]),
            prompt=r["prompt_text"],
            response=r["response_text"],
        )
    )
items.sort(key=lambda x: x["id"])
json.dump(
    [{k: v for k, v in i.items() if k not in ("prompt", "response")} for i in items],
    open(f"{S}/items_index.json", "w"),
    indent=1,
)
batches = []
cur = []
cs = 0
for i in items:
    if i["size"] > 90000:
        batches.append([i])
        continue
    if cur and (cs + i["size"] > 90000 or len(cur) >= 12):
        batches.append(cur)
        cur = []
        cs = 0
    cur.append(i)
    cs += i["size"]
if cur:
    batches.append(cur)
os.makedirs(f"{S}/batches", exist_ok=True)
for n, b in enumerate(batches):
    with open(f"{S}/batches/b{n:03d}.txt", "w") as fh:
        fh.write(
            f"Label these {len(b)} items per the rubric. Return only the JSON array.\n\n"
        )
        for i in b:
            fh.write(
                f"=== ITEM {i['id']} (task class: {i['task_class']}) ===\n--- computed_facts ---\n{json.dumps(i['facts'])}\n--- TASK (prompt given to the delegate) ---\n{i['prompt']}\n--- ANSWER (as delivered) ---\n{i['response']}\n=== END ITEM {i['id']} ===\n\n"
            )
print(len(items), "items", len(batches), "batches", [len(b) for b in batches][:40])
