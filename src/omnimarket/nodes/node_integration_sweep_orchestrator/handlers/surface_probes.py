"""Surface probe implementations for node_integration_sweep_orchestrator.

Each probe returns a structured dict:
    {
        "surface": "<NAME>",
        "status": "pass" | "fail" | "error",
        "details": { ... surface-specific fields ... },
    }

Probes MUST NOT raise. A connection failure is always captured as
status="error" with details["error"] set to the exception message.
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
