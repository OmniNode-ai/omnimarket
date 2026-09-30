# ruff: noqa: SIM115, T201
"""OMN-20032: read prompt/response content for manifest items, read-only as the runtime role. Runs on the lab only.
usage: python content_extract.py ids.json out.jsonl"""

import asyncio
import json
import os
import sys

import asyncpg

_DSN_ENV = "OMNIDASH_ANALYTICS_DB_URL"


def _dsn() -> str:
    return os.environ[_DSN_ENV]  # url-authority-ok: lab one-shot


ids = json.load(open(sys.argv[1]))


async def main():
    c = await asyncpg.connect(_dsn())
    await c.execute("SET default_transaction_read_only = on")
    rows = await c.fetch(
        """select correlation_id, task_type, model_name, delegated_to, quality_gate_passed,
        quality_gates_failed_jsonb, quality_gate_detail, attempt_history, prompt_text, response_text, created_at
        from public.delegation_events where tenant_id='820272f9-4aaf-5add-a2df-0af942852ab2' and correlation_id = any($1::text[])""",
        ids,
    )
    with open(sys.argv[2], "w") as fh:
        for r in rows:
            d = dict(r)
            d["created_at"] = d["created_at"].isoformat()
            for k in ("quality_gates_failed_jsonb", "attempt_history"):
                if isinstance(d[k], str):
                    d[k] = json.loads(d[k])
            fh.write(json.dumps(d) + "\n")
    print(len(rows), "rows for", len(ids), "ids")
    await c.close()


asyncio.run(main())
