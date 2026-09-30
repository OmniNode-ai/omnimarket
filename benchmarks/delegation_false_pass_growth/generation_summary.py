# ruff: noqa: SIM115, E741, T201
import asyncio
import glob
import json
import os
from collections import Counter

import asyncpg

_DSN_ENV = "OMNIDASH_ANALYTICS_DB_URL"


def _dsn() -> str:
    return os.environ[_DSN_ENV]  # url-authority-ok: lab one-shot


ids = set()
runs_db = 0
for f in glob.glob("/tmp/omn20032/gen_*.jsonl"):
    for l in open(f):
        d = json.loads(l)
        runs_db += 1
        if d.get("correlation_id"):
            ids.add(d["correlation_id"])
mem = []
for f in glob.glob("/tmp/omn20032/genmem_*.jsonl"):
    for l in open(f):
        mem.append(json.loads(l))
print("persisted-path runs logged", runs_db, "with correlation id", len(ids))
print(
    "in-memory runs logged",
    len(mem),
    Counter((d["cls"], d.get("status"), d.get("quality_gate_passed")) for d in mem),
)
print(
    "in-memory overlay",
    Counter(d.get("overlay", "dev.local.bifrost.yaml") for d in mem),
)
print(
    "in-memory tiers used",
    Counter(tuple(d.get("tiers") or []) for d in mem).most_common(4),
    "cost",
    sum((d.get("cost_usd") or 0) for d in mem),
)


async def main():
    c = await asyncpg.connect(_dsn())
    await c.execute("SET default_transaction_read_only = on")
    rows = await c.fetch(
        "select task_type, delegated_to, quality_gate_passed qg, terminal_ok, count(*) n, sum(coalesce(cost_usd,0)) cost from public.delegation_events where correlation_id = any($1::text[]) group by 1,2,3,4 order by 1,2",
        list(ids),
    )
    for r in rows:
        print(tuple(r.values()))
    print("total metered cost", sum(float(r["cost"]) for r in rows))
    await c.close()


asyncio.run(main())
