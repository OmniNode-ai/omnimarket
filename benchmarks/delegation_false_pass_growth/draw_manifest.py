# ruff: noqa: SIM115, T201, C408
"""OMN-20032: draw the grown manifest. Imports every EV.4 item (their labels carry over), then tops each
accepted stratum up to its quota. Content-free: reads candidates_meta.tsv, writes manifest.json."""

import csv
import json
import sys
from collections import Counter
from pathlib import Path

from omnimarket.nodes.node_delegation_eval_sample_compute.handlers.handler_delegation_eval_sample import (
    HandlerDelegationEvalSample,
)
from omnimarket.nodes.node_delegation_eval_sample_compute.models.model_delegation_eval_sample_request import (
    ModelDelegationEvalSampleRequest,
)

S = Path(sys.argv[1])
window_end = sys.argv[2]
accepted_quota = int(sys.argv[3])
prior_name = sys.argv[4] if len(sys.argv) > 4 else "ev4_manifest.json"
out_name = sys.argv[5] if len(sys.argv) > 5 else "manifest.json"
gen_ids = set(json.load(open(S / "generated_ids.json")))
prior = json.load(open(S / prior_name))
rows = list(csv.reader(open(S / "candidates_meta.tsv"), delimiter="\t"))
cands, dropped = [], Counter()
for r in rows:
    (
        cid,
        ai,
        tenant,
        tc,
        outcome,
        deleg,
        model,
        tok,
        ticket,
        created,
        pch,
        rch,
        phash,
    ) = r
    if ticket == "OMN-20032" and cid not in gen_ids:
        dropped["ticket-tagged-not-in-generation-log"] += 1
        continue
    if cid in gen_ids and tok != "true":
        dropped["generated-not-terminal-ok"] += 1
        continue
    if cid in gen_ids and not model.startswith("Qwen3.8-27B"):
        dropped["generated-not-local-qwen (escalated before the local-only guard)"] += 1
        continue
    cands.append(
        dict(
            correlation_id=cid,
            attempt_index=int(ai),
            tenant_id=tenant,
            task_class=tc,
            gate_outcome=outcome,
        )
    )
req = ModelDelegationEvalSampleRequest.model_validate(
    dict(
        seed="omn-20032-false-pass-100-2026-09-30",
        window_start="2026-08-25T00:00:00Z",
        window_end=window_end,
        query_text=(S / "candidates_meta.sql")
        .read_text()
        .replace("__WINDOW_END__", window_end),
        house_tenant_id="820272f9-4aaf-5add-a2df-0af942852ab2",
        sampling=dict(
            quotas=[
                dict(name="accepted", count=accepted_quota),
                dict(name="refused", count=12),
                dict(name="undetermined", count=0),
            ],
            holdout_buckets=10,
            reserved_bucket=0,
            order_key="sha256(seed + correlation_id + attempt_index)",
        ),
        candidates=cands,
        imported_keys=[i["key"] for i in prior["items"]],
    )
)
m = HandlerDelegationEvalSample().handle(req)
(S / out_name).write_text(m.model_dump_json(indent=1))
print(
    "manifest",
    m.manifest_id,
    "items",
    len(m.items),
    "holdout",
    m.excluded_holdout_bucket,
    "customer",
    m.excluded_customer_tenant,
    "rejected_imports",
    len(m.rejected_imports),
    dict(dropped),
)
print(
    sorted(
        Counter(
            (i.task_class, i.stratum.split("/")[1], i.source) for i in m.items
        ).items()
    )
)
print([s.model_dump() for s in m.shortfalls])
