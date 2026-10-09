# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Capture check-policy-gate.sh behaviour into tests/fixtures/handshake_policy_gate_parity.json.

Run once, against the retired script, from a checkout that still has it:

    uv run python tests/nodes/node_handshake_policy_gate_compute/capture_policy_gate_parity.py \\
        <path to architecture-handshakes> <source commit>

Each scenario scripts what `gh` answers per repo; the script runs against a fake `gh` and a
fake `sleep` on PATH, and the fixture records its stdout, stderr, exit code, the endpoints it
read and the sleeps it asked for. The golden-chain test replays every scenario through the
node and must reproduce all five.
"""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

FAKE_GH = r"""#!/usr/bin/env bash
state="${FAKE_GH_STATE}"
args=("$@")
jq_expr=""
for ((i = 0; i < ${#args[@]}; i++)); do
    if [[ "${args[i]}" == "--jq" ]]; then jq_expr="${args[i+1]}"; fi
done
endpoint="${args[1]:-}"
echo "${endpoint}" >> "${state}/endpoints.log"
if [[ "${endpoint}" != *"/actions/workflows/"* ]]; then
    repo="${endpoint##*/}"
    body=$(cat "${state}/branch.${repo}")
    if [[ "${body}" == ERR:* ]]; then echo "${body#ERR:}" >&2; exit 1; fi
    [[ -n "${body}" ]] && echo "${body}"
    exit 0
fi
repo="${endpoint#repos/OmniNode-ai/}"; repo="${repo%%/*}"
n=$(cat "${state}/calls.${repo}" 2>/dev/null || echo 0)
echo $((n + 1)) > "${state}/calls.${repo}"
total=$(ls "${state}/resp.${repo}".* | wc -l)
idx=$((n + 1 > total ? total : n + 1))
body=$(cat "${state}/resp.${repo}.${idx}")
if [[ "${body}" == ERR:* ]]; then echo "${body#ERR:}" >&2; exit 1; fi
printf '%s' "${body}" | jq -r "${jq_expr}"
"""
FAKE_SLEEP = '#!/usr/bin/env bash\necho "$1" >> "${FAKE_GH_STATE}/sleeps.log"\n'


def resp(conclusion: str | None, total: int) -> str:
    return json.dumps(
        {
            "total_count": total,
            "workflow_runs": [] if conclusion is None else [{"conclusion": conclusion}],
        }
    )


OK = [resp("success", 5)]
SCENARIOS: list[dict[str, Any]] = [
    {
        "name": "all_pass_strict",
        "conf": "alpha\nbeta\ngamma\n",
        "strict": True,
        "repos": {
            r: {"branch": "main", "runs": OK} for r in ("alpha", "beta", "gamma")
        },
    },
    {
        "name": "all_pass_report_only",
        "conf": "alpha\nbeta\n",
        "strict": False,
        "repos": {r: {"branch": "main", "runs": OK} for r in ("alpha", "beta")},
    },
    {
        "name": "mixed_strict",
        "conf": "p\nf\nnw\nnr\nerr\nretry_ok\n",
        "strict": True,
        "repos": {
            "p": {"branch": "main", "runs": OK},
            "f": {"branch": "main", "runs": [resp("failure", 4)]},
            "nw": {"branch": "main", "runs": ["ERR:HTTP 404: Not Found"]},
            "nr": {"branch": "main", "runs": [resp(None, 0)]},
            "err": {"branch": "main", "runs": ["ERR:HTTP 500 Server Error"]},
            "retry_ok": {
                "branch": "main",
                "runs": ["ERR:HTTP 502 Bad Gateway", resp(None, 3), resp("success", 3)],
            },
        },
    },
    {
        "name": "mixed_report_only",
        "conf": "p\nf\nnr\n",
        "strict": False,
        "repos": {
            "p": {"branch": "main", "runs": OK},
            "f": {"branch": "main", "runs": [resp("cancelled", 4)]},
            "nr": {"branch": "main", "runs": [resp(None, 0)]},
        },
    },
    {
        "name": "not_found_wording_without_404",
        "conf": "solo\n",
        "strict": True,
        "repos": {"solo": {"branch": "main", "runs": ["ERR:gh: Not Found"]}},
    },
    {
        "name": "persistent_empty_page_with_runs",
        "conf": "thin\n",
        "strict": True,
        "repos": {"thin": {"branch": "main", "runs": [resp(None, 3)]}},
    },
    {
        "name": "default_branch_lookup_fails",
        "conf": "lost\n",
        "strict": True,
        "repos": {"lost": {"branch": "ERR:HTTP 403 Forbidden", "runs": OK}},
    },
    {
        "name": "default_branch_empty_output",
        "conf": "blank\n",
        "strict": True,
        "repos": {"blank": {"branch": "", "runs": OK}},
    },
    {
        "name": "default_branch_needs_encoding",
        "conf": "odd\n",
        "strict": True,
        "repos": {"odd": {"branch": "release/v1 beta+x#2", "runs": OK}},
    },
    {
        "name": "conf_with_comments_and_whitespace",
        "conf": "# header\n\n  alpha  \t# trailing\nbeta#x\n\t\n   # only comment\ngamma\r\n",
        "strict": True,
        "repos": {
            r: {"branch": "main", "runs": OK} for r in ("alpha", "beta", "gamma")
        },
    },
    {
        "name": "conf_without_entries",
        "conf": "# nothing\n\n",
        "strict": True,
        "repos": {},
    },
    {
        "name": "attempts_two",
        "conf": "r\n",
        "strict": True,
        "env": {"POLICY_GATE_RETRY_ATTEMPTS": "2", "POLICY_GATE_RETRY_BASE_DELAY": "5"},
        "repos": {"r": {"branch": "main", "runs": ["ERR:HTTP 500 Server Error"]}},
    },
    {
        "name": "attempts_one_never_retries",
        "conf": "r\n",
        "strict": True,
        "env": {"POLICY_GATE_RETRY_ATTEMPTS": "1"},
        "repos": {"r": {"branch": "main", "runs": ["ERR:HTTP 500 Server Error"]}},
    },
    *[
        {
            "name": f"attempts_raw_{label}",
            "conf": "r\n",
            "strict": True,
            "env": {
                "POLICY_GATE_RETRY_ATTEMPTS": raw,
                "POLICY_GATE_RETRY_BASE_DELAY": "1",
            },
            "repos": {"r": {"branch": "main", "runs": ["ERR:HTTP 500 Server Error"]}},
        }
        for label, raw in (
            ("zero", "0"),
            ("word", "abc"),
            ("negative", "-3"),
            ("empty", ""),
            ("leading_zero", "03"),
            ("three", "3"),
        )
    ],
    *[
        {
            "name": f"base_delay_raw_{label}",
            "conf": "r\n",
            "strict": True,
            "env": {"POLICY_GATE_RETRY_BASE_DELAY": raw},
            "repos": {"r": {"branch": "main", "runs": ["ERR:HTTP 500 Server Error"]}},
        }
        for label, raw in (
            ("zero", "0"),
            ("fraction", "0.5"),
            ("word", "x"),
            ("empty", ""),
            ("three", "3"),
        )
    ],
]


def run_scenario(script_dir: Path, scenario: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        hs = root / "hs"
        hs.mkdir()
        for name in ("check-policy-gate.sh", "_parse_repos_conf.sh"):
            shutil.copy(script_dir / name, hs / name)
        (hs / "repos.conf").write_text(scenario["conf"])
        state = root / "state"
        state.mkdir()
        for repo, spec in scenario["repos"].items():
            (state / f"branch.{repo}").write_text(spec["branch"])
            for i, body in enumerate(spec["runs"], start=1):
                (state / f"resp.{repo}.{i}").write_text(body)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for name, body in (("gh", FAKE_GH), ("sleep", FAKE_SLEEP)):
            tool = bin_dir / name
            tool.write_text(body)
            tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
        env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
            "FAKE_GH_STATE": str(state),
            "GH_TOKEN": "fake",
            **scenario.get("env", {}),
        }
        args = ["bash", str(hs / "check-policy-gate.sh")]
        if scenario["strict"]:
            args.append("--strict")
        proc = subprocess.run(
            args, capture_output=True, text=True, env=env, timeout=60, check=False
        )

        def lines(name: str) -> list[str]:
            path = state / name
            return path.read_text().split() if path.exists() else []

        return {
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "exit_code": proc.returncode,
            "endpoints": lines("endpoints.log"),
            "sleeps": [int(seconds) for seconds in lines("sleeps.log")],
        }


def main() -> None:
    script_dir, commit = Path(sys.argv[1]), sys.argv[2]
    cases = [
        {**scenario, "old": run_scenario(script_dir, scenario)}
        for scenario in SCENARIOS
    ]
    out = (
        Path(__file__).resolve().parents[2]
        / "fixtures/handshake_policy_gate_parity.json"
    )
    out.write_text(
        json.dumps(
            {"source_commit": commit, "cases": cases}, indent=2, ensure_ascii=False
        )
        + "\n"
    )
    print(f"{len(cases)} cases -> {out}")


if __name__ == "__main__":
    main()
