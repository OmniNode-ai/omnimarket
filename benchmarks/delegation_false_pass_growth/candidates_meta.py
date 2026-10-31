# ruff: noqa: SIM115
import asyncio
import csv
import os
import sys

import asyncpg

_DSN_ENV, WORK_DIR = "OMNIDASH_ANALYTICS_DB_URL", os.environ["OMN20032_WORK_DIR"]


def _dsn() -> str:
    return os.environ[_DSN_ENV]  # url-authority-ok: lab one-shot


SQL = open(f"{WORK_DIR}/cands_meta.sql").read()


async def main():
    c = await asyncpg.connect(_dsn())
    await c.execute("SET default_transaction_read_only = on")
    rows = await c.fetch(SQL)
    w = csv.writer(sys.stdout, delimiter="\t", lineterminator="\n")
    for r in rows:
        w.writerow(list(r.values()))
    await c.close()


asyncio.run(main())
