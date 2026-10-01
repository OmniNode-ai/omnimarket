# ruff: noqa: SIM115, E741, T201, C408, B007, E731
"""OMN-20032: the eval readout, computed in memory with the EV.4 run handler (omnimarket#3127) and no database write.
usage: PYTHONPATH=<omnimarket src> python readout.py <dir> <rater_tag> <manifest.json> <gate_version>
reads <dir>/content.jsonl (lab content, read-only extract), <dir>/labels_<rater_tag>.jsonl (content-free labels);
writes <dir>/readout_<rater_tag>.json: the run payload results (recorded and replayed arms) and the by-backend / by-source cuts."""

import json
import sys
from collections import Counter, defaultdict
from uuid import UUID

from omnimarket.nodes.node_delegation_eval_run_orchestrator.handlers.handler_delegation_eval_run import (
    HandlerDelegationEvalRun,
)

from omnimarket.events.delegation_eval import ModelDelegationEvalRunRequest
from omnimarket.nodes.node_delegation_eval_orchestrator.scrubber import scrub_snapshot
from omnimarket.nodes.node_delegation_gate_eval_compute.handlers.handler_delegation_gate_eval import (
    wilson_interval,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.models.enum_gate_eval_label import (
    EnumGateEvalLabel,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.models.enum_gate_verdict import (
    EnumGateVerdict,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.models.model_gate_eval_item import (
    ModelGateEvalItem,
)

D, tag, manifest_path, gate_version = sys.argv[1:5]
HOUSE = UUID("820272f9-4aaf-5add-a2df-0af942852ab2")
m = json.load(open(manifest_path))
strata = {
    i["key"]["correlation_id"]: (
        i["task_class"],
        i["stratum"],
        i["key"]["attempt_index"],
    )
    for i in m["items"]
}
content = {}
for l in open(f"{D}/content.jsonl"):
    d = json.loads(l)
    content[d["correlation_id"]] = d
labels = {}
for l in open(f"{D}/labels_{tag}.jsonl"):
    d = json.loads(l)
    labels[d["correlation_id"]] = d
generated = set(json.load(open(f"{D}/generated_ids.json")))


def deciding_check(row):
    failed = row.get("quality_gates_failed_jsonb")
    if isinstance(failed, list) and failed:
        return ",".join(str(c) for c in failed)
    return str(row.get("quality_gate_detail") or "")[:200]


rows, meta = [], {}
for cid, (tc, stratum, ai) in strata.items():
    if cid not in labels or cid not in content:
        continue
    c = content[cid]
    lab = labels[cid]["label"]
    key = f"{cid}:{ai}"
    rows.append(
        ModelGateEvalItem(
            item_id=key,
            task_class=tc,
            stratum=stratum,
            label=EnumGateEvalLabel(lab),
            prompt_text=scrub_snapshot(c["prompt_text"] or ""),
            recorded_answer=scrub_snapshot(c["response_text"] or ""),
            recorded_verdict=EnumGateVerdict.ACCEPTED
            if c["quality_gate_passed"]
            else EnumGateVerdict.REFUSED,
            recorded_deciding_check=deciding_check(c) or None,
        )
    )
    meta[key] = dict(
        cid=cid,
        task_class=tc,
        backend=(c.get("model_name") or c.get("delegated_to") or "unknown"),
        source="generated" if cid in generated else "real",
        prompt_chars=len(c["prompt_text"] or ""),
        accepted=bool(c["quality_gate_passed"]),
        label=lab,
    )


class Port:
    def get_labelled_items(self, manifest_id, rater_role, rubric_version):
        return tuple(rows)


req = ModelDelegationEvalRunRequest(
    tenant_id=HOUSE,
    manifest_id=m["manifest_id"],
    rater_role={
        "opus": "blind_model:claude-opus-5-5",
        "sonnet": "blind_model:claude-sonnet-5-5",
    }.get(tag, f"blind_model:{tag}"),
    rubric_version="ev4-blind-v1",
    gate_version=gate_version,
)
payload = HandlerDelegationEvalRun(Port()).handle(req).payload.model_dump(mode="json")


def cut(keyfn, only_accepted=True):
    g = defaultdict(lambda: [0, 0])
    for k, v in meta.items():
        if v["label"] == "unlabelable" or (only_accepted and not v["accepted"]):
            continue
        a = g[keyfn(v)]
        a[0] += 1
        a[1] += v["label"] == "inadequate"
    out = {}
    for kk, (n, fp) in sorted(g.items()):
        w = wilson_interval(fp, n)
        out[" | ".join(kk)] = {
            "accepted_n": n,
            "false_pass": fp,
            "rate": fp / n if n else None,
            "wilson_low": w.low,
            "wilson_high": w.high,
        }
    return out


band = lambda v: (
    "short_prompt_lt200" if v["prompt_chars"] < 200 else "long_prompt_ge200"
)
extras = {
    "by_class_backend": cut(lambda v: (v["task_class"], v["backend"])),
    "by_class_source": cut(lambda v: (v["task_class"], v["source"])),
    "by_class_prompt_band": cut(lambda v: (v["task_class"], band(v))),
    "by_backend": cut(lambda v: (v["backend"],)),
    "distinct_prompts_note": "see report: computed from manifest metadata",
    "unlabelable": dict(
        Counter(v["task_class"] for v in meta.values() if v["label"] == "unlabelable")
    ),
    "labelled_items": len(rows),
}
json.dump(
    {"rater": tag, "gate_version": gate_version, "payload": payload, "extras": extras},
    open(f"{D}/readout_{tag}.json", "w"),
    indent=1,
    default=str,
)
print(
    tag,
    "items",
    len(rows),
    "status",
    payload["status"],
    payload["failure_reasons"],
    "run",
    payload["eval_run_id"],
)
for r in payload["results"]:
    if r["arm"] == "recorded" and r["stratum"] == "all":
        print(
            f"{r['task_class']:16} acc={r['accepted_n']:4} fp={r['false_pass_count']:3} rate={r['false_pass_rate'] if r['false_pass_rate'] is None else round(r['false_pass_rate'], 3)} line={r['false_pass_line_verdict']} refused_n={r['refused_n']} fr={r['false_refusal_count']}"
        )
