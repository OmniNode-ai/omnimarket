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


ids = {}
for f in glob.glob("/tmp/omn20032/gen_*.jsonl"):
    for l in open(f):
        d = json.loads(l)
        if d.get("correlation_id"):
            ids[d["correlation_id"]] = d


async def main():
    c = await asyncpg.connect(_dsn())
    await c.execute("SET default_transaction_read_only = on")
    rows = await c.fetch(
        "select correlation_id, task_type, delegated_to, quality_gate_passed qg, terminal_ok, cost_usd, created_at from public.delegation_events where correlation_id = any($1::text[])",
        list(ids),
    )
    guard = "2026-09-30T18:28:41Z"
    from datetime import datetime

    g = datetime.fromisoformat(guard.replace("Z", "+00:00"))
    post = [r for r in rows if r["created_at"] >= g]
    print("logged", len(ids), "rows", len(rows), "post-guard rows", len(post))
    print(
        "post-guard by model/qg/cost>0:",
        Counter(
            (r["task_type"], r["delegated_to"], r["qg"], float(r["cost_usd"] or 0) > 0)
            for r in post
        ),
    )
    print("ALL by model:", Counter((r["delegated_to"], r["qg"]) for r in rows))
    print("paid total:", sum(float(r["cost_usd"] or 0) for r in rows))
    await c.close()


asyncio.run(main())
