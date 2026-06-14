"""Surface probe implementations for node_integration_sweep_orchestrator.

Each probe returns a structured dict:
    {
        "surface": "<NAME>",
        "status": "pass" | "fail" | "error",
        "details": { ... surface-specific fields ... },
    }

Probes MUST NOT raise. A connection failure is always captured as
status="error" with details["error"] set to the exception message.

The infrastructure-surface probes (KAFKA, DB, PROJECTION, GOLDEN_CHAIN) reach
the runtime lane over SSH and ``docker exec`` against the lane's Redpanda and
Postgres containers, mirroring the rpk/psql technique already proven in
``node_data_flow_sweep.collector`` and the closeout DB-truth method
(docs/evidence/2026-06-14-closeout/GOLDEN_CHAIN_DB_TRUTH.md).
"""

from __future__ import annotations

import json
import shlex
import subprocess
from typing import Any


def probe_runtime_health(url: str) -> dict[str, Any]:
    """HTTP GET the runtime /health endpoint on the stability-test lane.

    Uses curl with a short connect timeout; returns the HTTP status code
    and a snippet of the response body.
    """
    surface = "RUNTIME_HEALTH"
    health_url = url.rstrip("/") + "/health"
    try:
        result = subprocess.run(
            [
                "curl",
                "-fsS",
                "--connect-timeout",
                "5",
                health_url,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return {
                "surface": surface,
                "status": "pass",
                "details": {
                    "url": health_url,
                    "response": result.stdout[:500],
                },
            }
        return {
            "surface": surface,
            "status": "fail",
            "details": {
                "url": health_url,
                "returncode": result.returncode,
                "stderr": result.stderr[:500],
            },
        }
    except Exception as exc:
        return {
            "surface": surface,
            "status": "error",
            "details": {"url": health_url, "error": str(exc)},
        }


def probe_container_health(runtime_host: str) -> dict[str, Any]:
    """List Docker containers on the runtime host via SSH and report health.

    Parses the output of ``docker ps`` to count running vs unhealthy containers.
    """
    surface = "CONTAINER_HEALTH"
    try:
        remote_command = "docker ps --format " + shlex.quote("{{.Names}}\t{{.Status}}")
        result = subprocess.run(
            [
                "ssh",
                f"jonah@{runtime_host}",
                remote_command,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return {
                "surface": surface,
                "status": "fail",
                "details": {
                    "host": runtime_host,
                    "returncode": result.returncode,
                    "stderr": result.stderr[:500],
                },
            }
        lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        containers: list[dict[str, str]] = []
        running_count = 0
        unhealthy_count = 0
        for line in lines:
            parts = line.split("\t", 1)
            name = parts[0].strip() if parts else ""
            status = parts[1].strip() if len(parts) > 1 else ""
            containers.append({"name": name, "status": status})
            status_lower = status.lower()
            if "unhealthy" in status_lower:
                unhealthy_count += 1
            elif status_lower.startswith("up"):
                running_count += 1
        probe_status = "fail" if unhealthy_count > 0 else "pass"
        return {
            "surface": surface,
            "status": probe_status,
            "details": {
                "host": runtime_host,
                "total_containers": len(containers),
                "running": running_count,
                "unhealthy": unhealthy_count,
                "containers": containers[:20],
            },
        }
    except Exception as exc:
        return {
            "surface": surface,
            "status": "error",
            "details": {"host": runtime_host, "error": str(exc)},
        }


def probe_github_ci(repo: str) -> dict[str, Any]:
    """Query recent GitHub Actions run results for the given repo.

    Uses the ``gh`` CLI to fetch the last 5 runs and counts pass/fail.
    """
    surface = "GITHUB_CI"
    try:
        result = subprocess.run(
            [
                "gh",
                "run",
                "list",
                "--repo",
                f"OmniNode-ai/{repo}",
                "--limit",
                "5",
                "--json",
                "conclusion,name,status",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return {
                "surface": surface,
                "status": "fail",
                "details": {
                    "repo": repo,
                    "returncode": result.returncode,
                    "stderr": result.stderr[:500],
                },
            }
        runs: list[dict[str, Any]] = []
        try:
            parsed = json.loads(result.stdout)
            if isinstance(parsed, list):
                runs = parsed
        except json.JSONDecodeError:
            pass
        pass_count = sum(1 for r in runs if r.get("conclusion") == "success")
        fail_count = sum(
            1
            for r in runs
            if r.get("conclusion") in ("failure", "cancelled", "timed_out")
        )
        probe_status = "pass" if fail_count == 0 and pass_count > 0 else "fail"
        if not runs:
            probe_status = "error"
        return {
            "surface": surface,
            "status": probe_status,
            "details": {
                "repo": repo,
                "run_count": len(runs),
                "pass": pass_count,
                "fail": fail_count,
                "runs": runs[:5],
            },
        }
    except Exception as exc:
        return {
            "surface": surface,
            "status": "error",
            "details": {"repo": repo, "error": str(exc)},
        }


# ---------------------------------------------------------------------------
# Infrastructure-surface probes (KAFKA / DB / PROJECTION / GOLDEN_CHAIN)
#
# These reach the runtime lane over SSH and run rpk / psql inside the lane's
# Redpanda / Postgres containers via ``docker exec``. The shape mirrors the
# already-proven probes above: never raise, always return a structured dict.
# ---------------------------------------------------------------------------


def _ssh_docker_exec(
    *, runtime_host: str, container: str, inner_argv: list[str], timeout: int
) -> tuple[int, str, str]:
    """Run ``docker exec <container> <inner_argv>`` on ``runtime_host`` over SSH.

    Returns ``(returncode, stdout, stderr)``. The inner argv is shell-quoted so
    a single remote shell parses it as one ``docker exec`` invocation.
    """
    remote_command = "docker exec " + container + " " + shlex.join(inner_argv)
    result = subprocess.run(
        ["ssh", f"jonah@{runtime_host}", remote_command],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.returncode, result.stdout, result.stderr


def probe_kafka_topics(
    runtime_host: str,
    redpanda_container: str,
    topics: list[str],
    consumer_groups: list[str],
) -> dict[str, Any]:
    """Verify topic existence and consumer-group presence on the lane's Redpanda.

    Runs ``rpk topic list`` and ``rpk group list`` inside the Redpanda
    container. The probe passes only when every requested topic exists and
    every requested consumer group is present (registered consumers).
    """
    surface = "KAFKA"
    if not topics and not consumer_groups:
        return {
            "surface": surface,
            "status": "error",
            "details": {
                "host": runtime_host,
                "error": "no topics or consumer_groups configured for KAFKA probe",
            },
        }
    try:
        topic_rc, topic_out, topic_err = _ssh_docker_exec(
            runtime_host=runtime_host,
            container=redpanda_container,
            inner_argv=["rpk", "topic", "list"],
            timeout=20,
        )
        if topic_rc != 0:
            return {
                "surface": surface,
                "status": "fail",
                "details": {
                    "host": runtime_host,
                    "container": redpanda_container,
                    "returncode": topic_rc,
                    "stderr": topic_err[:500],
                },
            }
        present_topics = {
            line.split()[0]
            for line in topic_out.splitlines()
            if line.strip() and not line.startswith("NAME")
        }
        missing_topics = [t for t in topics if t not in present_topics]

        group_rc, group_out, _group_err = _ssh_docker_exec(
            runtime_host=runtime_host,
            container=redpanda_container,
            inner_argv=["rpk", "group", "list"],
            timeout=20,
        )
        # `rpk group list` columns are: BROKER  GROUP  STATE. The group name is
        # column index 1 (group names carry no spaces); STATE is the last column.
        present_groups = (
            {
                line.split()[1]
                for line in group_out.splitlines()
                if line.strip()
                and not line.startswith("BROKER")
                and len(line.split()) >= 2
            }
            if group_rc == 0
            else set()
        )
        missing_groups = [g for g in consumer_groups if g not in present_groups]

        probe_status = "pass" if not missing_topics and not missing_groups else "fail"
        return {
            "surface": surface,
            "status": probe_status,
            "details": {
                "host": runtime_host,
                "container": redpanda_container,
                "topics_checked": topics,
                "topics_present": sorted(t for t in topics if t in present_topics),
                "topics_missing": missing_topics,
                "consumer_groups_checked": consumer_groups,
                "consumer_groups_present": sorted(
                    g for g in consumer_groups if g in present_groups
                ),
                "consumer_groups_missing": missing_groups,
            },
        }
    except Exception as exc:
        return {
            "surface": surface,
            "status": "error",
            "details": {"host": runtime_host, "error": str(exc)},
        }


def probe_db_tables(
    runtime_host: str,
    postgres_container: str,
    postgres_user: str,
    database: str,
    tables: list[str],
) -> dict[str, Any]:
    """Verify tail-table reachability and row presence on the lane's Postgres.

    For each table, runs ``to_regclass`` (NULL => absent, no error) and, when
    present, ``count(*)``. The probe passes only when every table exists and
    holds at least one row — the DB-truth method that catches empty tail
    tables (docs/evidence/2026-06-14-closeout/GOLDEN_CHAIN_DB_TRUTH.md).
    """
    surface = "DB"
    if not tables:
        return {
            "surface": surface,
            "status": "error",
            "details": {
                "host": runtime_host,
                "error": "no tables configured for DB probe",
            },
        }
    try:
        table_results: list[dict[str, Any]] = []
        absent: list[str] = []
        empty: list[str] = []
        for table in tables:
            qualified = table if "." in table else f"public.{table}"
            reg_rc, reg_out, _reg_err = _ssh_docker_exec(
                runtime_host=runtime_host,
                container=postgres_container,
                inner_argv=[
                    "psql",
                    "-U",
                    postgres_user,
                    "-d",
                    database,
                    "-tAc",
                    f"SELECT to_regclass('{qualified}');",
                ],
                timeout=15,
            )
            exists = reg_rc == 0 and reg_out.strip() not in ("", "NULL")
            row_count: int | None = None
            if not exists:
                absent.append(table)
            else:
                cnt_rc, cnt_out, _cnt_err = _ssh_docker_exec(
                    runtime_host=runtime_host,
                    container=postgres_container,
                    inner_argv=[
                        "psql",
                        "-U",
                        postgres_user,
                        "-d",
                        database,
                        "-tAc",
                        f"SELECT count(*) FROM {qualified};",
                    ],
                    timeout=15,
                )
                if cnt_rc == 0 and cnt_out.strip().isdigit():
                    row_count = int(cnt_out.strip())
                    if row_count == 0:
                        empty.append(table)
            table_results.append(
                {"table": qualified, "exists": exists, "row_count": row_count}
            )

        probe_status = "pass" if not absent and not empty else "fail"
        return {
            "surface": surface,
            "status": probe_status,
            "details": {
                "host": runtime_host,
                "container": postgres_container,
                "database": database,
                "tables": table_results,
                "tables_absent": absent,
                "tables_empty": empty,
            },
        }
    except Exception as exc:
        return {
            "surface": surface,
            "status": "error",
            "details": {"host": runtime_host, "error": str(exc)},
        }


def probe_projection_api(projection_api_url: str, topics: list[str]) -> dict[str, Any]:
    """Verify the projection API serves each requested projection topic.

    Reads ``<projection_api_url>/projection/<topic>`` for each topic. A topic
    passes when the endpoint returns HTTP 200 with a JSON body; the probe
    passes only when every topic responds.
    """
    surface = "PROJECTION"
    if not topics:
        return {
            "surface": surface,
            "status": "error",
            "details": {
                "url": projection_api_url,
                "error": "no projection topics configured for PROJECTION probe",
            },
        }
    base = projection_api_url.rstrip("/")
    topic_results: list[dict[str, Any]] = []
    failed: list[str] = []
    try:
        for topic in topics:
            endpoint = f"{base}/projection/{topic}"
            result = subprocess.run(
                [
                    "curl",
                    "-fsS",
                    "-o",
                    "/dev/null",
                    "-w",
                    "%{http_code}",
                    "--connect-timeout",
                    "5",
                    endpoint,
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            http_code = result.stdout.strip()
            ok = result.returncode == 0 and http_code == "200"
            if not ok:
                failed.append(topic)
            topic_results.append({"topic": topic, "http_code": http_code, "ok": ok})
        probe_status = "pass" if not failed else "fail"
        return {
            "surface": surface,
            "status": probe_status,
            "details": {
                "url": base,
                "topics": topic_results,
                "topics_failed": failed,
            },
        }
    except Exception as exc:
        return {
            "surface": surface,
            "status": "error",
            "details": {"url": base, "error": str(exc)},
        }


def probe_golden_chain(
    runtime_host: str,
    redpanda_container: str,
    postgres_container: str,
    postgres_user: str,
    chain_name: str,
    command_topic: str,
    consumer_group: str,
    tail_database: str,
    tail_table: str,
) -> dict[str, Any]:
    """Compose a head->consumer->tail assertion for one golden chain.

    Asserts (1) the command topic exists, (2) the consumer group is registered,
    and (3) the tail table exists and holds rows. Passes only when all three
    hold — a real end-to-end signal rather than a health ping.
    """
    surface = "GOLDEN_CHAIN"
    try:
        kafka = probe_kafka_topics(
            runtime_host=runtime_host,
            redpanda_container=redpanda_container,
            topics=[command_topic],
            consumer_groups=[consumer_group],
        )
        db = probe_db_tables(
            runtime_host=runtime_host,
            postgres_container=postgres_container,
            postgres_user=postgres_user,
            database=tail_database,
            tables=[tail_table],
        )
        topic_present = command_topic not in kafka["details"].get(
            "topics_missing", [command_topic]
        )
        group_present = consumer_group not in kafka["details"].get(
            "consumer_groups_missing", [consumer_group]
        )
        tail_rows = 0
        for table_result in db["details"].get("tables", []):
            if table_result["table"].endswith(tail_table):
                tail_rows = table_result.get("row_count") or 0
        tail_has_rows = tail_rows > 0

        probe_status = (
            "pass" if topic_present and group_present and tail_has_rows else "fail"
        )
        return {
            "surface": surface,
            "status": probe_status,
            "details": {
                "chain_name": chain_name,
                "command_topic": command_topic,
                "topic_present": topic_present,
                "consumer_group": consumer_group,
                "consumer_group_present": group_present,
                "tail_database": tail_database,
                "tail_table": tail_table,
                "tail_row_count": tail_rows,
                "tail_has_rows": tail_has_rows,
            },
        }
    except Exception as exc:
        return {
            "surface": surface,
            "status": "error",
            "details": {"chain_name": chain_name, "error": str(exc)},
        }
