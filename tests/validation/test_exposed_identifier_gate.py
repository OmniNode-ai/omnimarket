# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17320: tests for the exposed-identifier gate.

Every mechanism test below runs against a **synthetic** denylist minted inside
``tmp_path``. That is not a convenience -- it is the point. Committing a fixture
containing a real denylisted literal would create precisely the fresh, greppable,
current-tree occurrence the gate exists to prevent, and would make this file a
whole-file exemption: the shape that produced the OMN-17288 incident in the first
place. So the mechanism is proven on a synthetic entry, and non-vacuity against the
*real* entries is proven two other ways:

* ``test_incident_replay_omn17288`` replays the actual reintroduced file content out
  of this repo's own git history (no literal is committed -- it is read from objects
  that already exist), and
* an out-of-tree run recorded in the PR body, which exercises every real entry.

That split is stated rather than glossed because a test suite that silently proved
nothing about the real entries is exactly the failure mode this ticket exists to fix.

The gate is invoked as a real subprocess throughout -- no mocking, no reimplementation
of the matching logic.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
GATE = REPO_ROOT / "scripts" / "validation" / "check_exposed_identifiers.py"
REAL_DENYLIST = (
    REPO_ROOT / "scripts" / "validation" / "exposed_identifiers_denylist.json"
)
MINTER = REPO_ROOT / "scripts" / "validation" / "gen_exposed_identifier_entry.py"

# A stand-in that exists only in this test module. It is not a real identifier and
# is deliberately not in the shipped denylist, so the live gate ignores it.
SYNTHETIC = "t-omn17320-synthetic-probe"

# OMN-17288 / OMN-16804 incident coordinates. `e0cb5235` is the hand fix; its parent
# carries the content omnimarket#2239 reintroduced. Both hits were on line 12.
INCIDENT_FIX = "e0cb5235"
INCIDENT_FILES = (
    "src/omnimarket/projection/tenant_registry_resolution.py",
    "tests/test_omn16804_registry_resolved_write_tenant.py",
)

pytestmark = pytest.mark.unit


def _mint(tmp_path: Path, *literals: str) -> Path:
    """Write a synthetic denylist holding digests of `literals` under the real salt."""
    salt = json.loads(REAL_DENYLIST.read_text(encoding="utf-8"))["salt"]
    doc = {
        "schema": "onex.exposed-identifier-denylist/1",
        "ticket": "OMN-17320",
        "salt": salt,
        "annotation_class": "exposed-identifier",
        "plaintext_source": "synthetic test fixture; no real identifier",
        "entries": [
            {
                "id": f"synthetic-{index}",
                "kind": "test-fixture",
                "length": len(literal),
                "sha256": hashlib.sha256(
                    (salt + literal.lower()).encode("utf-8")
                ).hexdigest(),
                "ticket": "OMN-17320",
                "notes": "synthetic probe minted by the test suite",
            }
            for index, literal in enumerate(literals)
        ],
    }
    path = tmp_path / "denylist.json"
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return path


def _run(target: Path, denylist: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(GATE),
            "--denylist",
            str(denylist),
            "--root",
            str(target.parent),
            str(target),
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _probe(
    tmp_path: Path, content: str, *extra: str
) -> subprocess.CompletedProcess[str]:
    target = tmp_path / "sample.py"
    target.write_text(content, encoding="utf-8")
    return _run(target, _mint(tmp_path, SYNTHETIC), *extra)


# --------------------------------------------------------------------------- #
# AC2 -- the three match shapes, plus case folding.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("content", "label"),
    [
        (f'slug = "{SYNTHETIC}"\n', "bare token"),
        (f'slug = "tenant_{SYNTHETIC}_v2"\n', "embedded in a longer token"),
        (
            f'url = "https://api.example.test/v1/tenants/{SYNTHETIC}/events"\n',
            "path segment",
        ),
        (f'slug = "{SYNTHETIC.upper()}"\n', "uppercased"),
        (f'row = ["a", "{SYNTHETIC}", "b"]\n', "inside a list literal"),
    ],
)
def test_denylisted_literal_is_caught(tmp_path: Path, content: str, label: str) -> None:
    result = _probe(tmp_path, content)
    assert result.returncode == 1, (
        f"{label}: gate passed content it must reject\n{result.stdout}"
    )
    assert "findings=1" in result.stdout, f"{label}: {result.stdout}"


def test_clean_content_passes(tmp_path: Path) -> None:
    result = _probe(tmp_path, 'slug = "t-synthetic-fixture-unrelated"\n')
    assert result.returncode == 0, result.stdout
    assert "findings=0" in result.stdout


def test_a_shorter_prefix_of_the_literal_does_not_match(tmp_path: Path) -> None:
    """Guards against an off-by-one turning the window scan into a prefix match."""
    result = _probe(tmp_path, f'slug = "{SYNTHETIC[:-1]}"\n')
    assert result.returncode == 0, result.stdout


# --------------------------------------------------------------------------- #
# AC5 -- the annotation is the ONLY escape hatch, and it must be ticketed+reasoned.
# --------------------------------------------------------------------------- #


def test_annotation_with_ticket_and_reason_exempts(tmp_path: Path) -> None:
    content = (
        f'slug = "{SYNTHETIC}"  # onex-allow-exposed-identifier OMN-17320 '
        f'reason="historical narrative; a stand-in would assert a false fact"\n'
    )
    result = _probe(tmp_path, content)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    ("suffix", "label"),
    [
        ("# onex-allow-exposed-identifier", "bare annotation, no ticket, no reason"),
        ("# onex-allow-exposed-identifier OMN-17320", "ticket but no reason"),
        ('# onex-allow-exposed-identifier reason="no ticket"', "reason but no ticket"),
        (
            '# onex-allow-internal-ip OMN-17320 reason="wrong class"',
            "a different onex-allow class",
        ),
    ],
)
def test_malformed_or_wrong_class_annotation_does_not_exempt(
    tmp_path: Path, suffix: str, label: str
) -> None:
    result = _probe(tmp_path, f'slug = "{SYNTHETIC}"  {suffix}\n')
    assert result.returncode == 1, (
        f"{label}: annotation wrongly accepted\n{result.stdout}"
    )


def test_annotation_does_not_exempt_a_different_line(tmp_path: Path) -> None:
    content = (
        '# onex-allow-exposed-identifier OMN-17320 reason="this is on its own line"\n'
        f'slug = "{SYNTHETIC}"\n'
    )
    result = _probe(tmp_path, content)
    assert result.returncode == 1, result.stdout


def test_there_is_no_file_level_exemption_for_this_class(tmp_path: Path) -> None:
    """The leaked-literals gate honours `# onex-allow-file`. This one must not.

    A whole-file waiver is how a forbidden value survives in a corner nobody reads,
    which is the OMN-17288 failure mode. Per-line only, deliberately.
    """
    content = (
        '# onex-allow-file OMN-17320 reason="attempting a whole-file waiver"\n'
        f'slug = "{SYNTHETIC}"\n'
    )
    result = _probe(tmp_path, content)
    assert result.returncode == 1, f"file-level waiver was honoured\n{result.stdout}"


# --------------------------------------------------------------------------- #
# The gate must not leak what it guards.
# --------------------------------------------------------------------------- #


def test_findings_never_print_the_matched_value(tmp_path: Path) -> None:
    result = _probe(tmp_path, f'slug = "{SYNTHETIC}"\n')
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert SYNTHETIC not in combined, "the gate printed the value it exists to suppress"
    # It must still pinpoint the token: line 1, column 9, length 26.
    assert "sample.py:1:9:" in result.stdout, result.stdout
    assert f"match_len={len(SYNTHETIC)}" in result.stdout, result.stdout


def test_shipped_denylist_contains_no_plaintext(tmp_path: Path) -> None:
    doc = json.loads(REAL_DENYLIST.read_text(encoding="utf-8"))
    for entry in doc["entries"]:
        assert set(entry) == {"id", "kind", "length", "sha256", "ticket", "notes"}, (
            entry["id"]
        )


def test_denylist_loader_refuses_a_plaintext_field(tmp_path: Path) -> None:
    """Fail closed if someone 'helpfully' adds the literal back for readability."""
    denylist = _mint(tmp_path, SYNTHETIC)
    doc = json.loads(denylist.read_text(encoding="utf-8"))
    doc["entries"][0]["value"] = SYNTHETIC
    denylist.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n", encoding="utf-8")
    result = _run(target, denylist)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "must never carry plaintext" in result.stderr, result.stderr


# --------------------------------------------------------------------------- #
# Shipped denylist shape + overlap collapse + modes.
# --------------------------------------------------------------------------- #


def test_shipped_denylist_is_wellformed() -> None:
    doc = json.loads(REAL_DENYLIST.read_text(encoding="utf-8"))
    assert doc["schema"] == "onex.exposed-identifier-denylist/1"
    assert doc["annotation_class"] == "exposed-identifier"
    assert doc["salt"], "an empty salt makes every digest a plain sha256"
    entries = doc["entries"]
    assert len(entries) >= 5, (
        "the OMN-17288 pair expands to 5 shapes; fewer means one was dropped"
    )
    ids = [entry["id"] for entry in entries]
    assert len(ids) == len(set(ids)), f"duplicate entry ids: {ids}"
    for entry in entries:
        assert len(entry["sha256"]) == 64, entry["id"]
        assert entry["sha256"].islower(), entry["id"]
        assert int(entry["sha256"], 16) >= 0
        assert entry["length"] >= 4, (
            f"{entry['id']}: a <4 char literal would match everywhere"
        )
        assert entry["ticket"].startswith("OMN-"), entry["id"]
        assert entry["notes"].strip(), (
            f"{entry['id']}: every entry must say where it leaked"
        )
    # The five OMN-17288 shapes must all be present by kind.
    kinds = {entry["kind"] for entry in entries}
    assert {
        "tenant-slug",
        "tenant-slug-body",
        "tenant-uuid",
        "tenant-uuid-prefix",
        "tenant-uuid-hex",
    } <= kinds, kinds


def test_overlapping_entries_collapse_to_the_longest(tmp_path: Path) -> None:
    """A full value also matches a shorter entry nested inside it; report it once."""
    denylist = _mint(tmp_path, SYNTHETIC, SYNTHETIC[:12])
    target = tmp_path / "sample.py"
    target.write_text(f'slug = "{SYNTHETIC}"\n', encoding="utf-8")
    result = _run(target, denylist)
    assert result.returncode == 1
    assert "findings=1" in result.stdout, f"overlap not collapsed:\n{result.stdout}"
    assert f"match_len={len(SYNTHETIC)}" in result.stdout, result.stdout


def test_advisory_mode_reports_but_exits_zero(tmp_path: Path) -> None:
    result = _probe(tmp_path, f'slug = "{SYNTHETIC}"\n', "--mode", "advisory")
    assert result.returncode == 0, result.stdout
    assert "findings=1" in result.stdout


def test_minter_never_echoes_the_literal(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(MINTER),
            "--id",
            "probe",
            "--kind",
            "test-fixture",
            "--ticket",
            "OMN-17320",
            "--notes",
            "probe",
            "--denylist",
            str(REAL_DENYLIST),
        ],
        input=SYNTHETIC,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert SYNTHETIC not in result.stdout + result.stderr, "the minter echoed its input"
    minted = json.loads(result.stdout)
    salt = json.loads(REAL_DENYLIST.read_text(encoding="utf-8"))["salt"]
    expected = hashlib.sha256((salt + SYNTHETIC).encode("utf-8")).hexdigest()
    assert minted["sha256"] == expected
    assert minted["length"] == len(SYNTHETIC)


# --------------------------------------------------------------------------- #
# AC4 -- real-incident replay (OMN-15547 convention), and the standing regression.
# --------------------------------------------------------------------------- #


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_incident_replay_omn17288(tmp_path: Path) -> None:
    """Replay omnimarket#2239's reintroduction against the shipped denylist.

    No literal is committed by this test: the content is read out of git objects
    that already exist in this repository's history. Skips on a shallow clone --
    the durable proof for the real entries is the out-of-tree run recorded on the
    PR, and this test is the in-repo half of it.
    """
    if _git("cat-file", "-e", f"{INCIDENT_FIX}^{{commit}}").returncode != 0:
        pytest.skip(
            f"{INCIDENT_FIX} unreachable (shallow clone); see PR body for the recorded run"
        )

    flagged: list[str] = []
    for path in INCIDENT_FILES:
        pre = _git("show", f"{INCIDENT_FIX}^:{path}")
        post = _git("show", f"{INCIDENT_FIX}:{path}")
        assert pre.returncode == 0, f"{path} not in history at {INCIDENT_FIX}^"
        assert post.returncode == 0, f"{path} not in history at {INCIDENT_FIX}"

        pre_file = tmp_path / f"pre_{Path(path).name}"
        pre_file.write_text(pre.stdout, encoding="utf-8")
        pre_result = _run(pre_file, REAL_DENYLIST)
        assert pre_result.returncode == 1, (
            f"RED control is vacuous: the gate PASSED {path} at {INCIDENT_FIX}^, "
            f"which is the content that reintroduced the slug\n{pre_result.stdout}"
        )
        flagged.append(pre_result.stdout)

        post_file = tmp_path / f"post_{Path(path).name}"
        post_file.write_text(post.stdout, encoding="utf-8")
        post_result = _run(post_file, REAL_DENYLIST)
        assert post_result.returncode == 0, (
            f"the gate rejects the merged fix for {path}\n{post_result.stdout}"
        )

    # Both recorded hits were on line 12; if that moves, the replay is not replaying.
    assert all(":12:" in out for out in flagged), flagged


def test_the_whole_repository_is_clean() -> None:
    """The standing regression. This is what omnimarket#2239 would have tripped."""
    result = subprocess.run(
        [sys.executable, str(GATE), "--mode", "blocking", "--scope", "all"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "a denylisted identifier is present in the working tree:\n"
        + result.stdout
        + result.stderr
    )


def test_delegation_resolves_the_sibling_gate_from_a_foreign_cwd(
    tmp_path: Path,
) -> None:
    """check_leaked_literals.sh must find its sibling regardless of the CWD.

    Regression: the delegation first named the sibling by a repo-relative path,
    so every caller with a different working directory -- pre-commit, CI, and
    the validation tests that build a throwaway fixture repo under tmp_path --
    saw it as "missing" and tripped the fail-closed branch, turning a healthy
    gate into a hard exit 2. Caught by the governed pre-push selector, not by
    hand. A gate that hard-errors on its own healthy configuration gets
    disabled by the next person it blocks, so this is pinned.
    """
    host = REPO_ROOT / "scripts" / "validation" / "check_leaked_literals.sh"
    fixture = tmp_path / "elsewhere"
    fixture.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=fixture, check=True)
    (fixture / "clean.md").write_text("nothing forbidden here\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=fixture, check=True)

    result = subprocess.run(
        ["bash", str(host), "blocking", "all"],
        cwd=fixture,
        capture_output=True,
        text=True,
        check=False,
    )
    combined = result.stdout + result.stderr
    assert "is missing (OMN-17320)" not in combined, combined
    assert "exposed-id-gate:" in combined, (
        "the delegated gate did not run at all from a foreign CWD:\n" + combined
    )
    assert result.returncode == 0, combined


# --------------------------------------------------------------------------- #
# AC7 -- cross-repo parity.
# --------------------------------------------------------------------------- #

# The scanner and its denylist ship byte-identical in omnimarket and
# omnibase_infra -- the two PUBLIC repos the OMN-17288 occurrence map names. These
# digests are the SAME two strings in both repos' copies of this test, so drift is
# visible to a one-line grep across the pair.
#
# Honest limit, stated rather than implied: this pin fails in the repo being
# EDITED, which forces whoever changes the scanner to update the pin and notice the
# other copy. It does not, by itself, prove the other repo was updated too. A hard
# cross-repo gate would have omnibase_infra CI resolve omnimarket's live dev HEAD --
# and OMN-17292 is an open ticket about exactly that coupling reddening every infra
# PR whenever omnimarket merges something unrelated. Adding a second instance of an
# anti-pattern the team just filed against would trade a real problem for a worse
# one, so the weaker-but-local check is deliberate.
PINNED_SCANNER_SHA256 = (
    "3bcae6e1d4c884ec9cc21d76ad75ef2a394ae5376b0f3fa8263f3ba607ce6826"
)
PINNED_DENYLIST_SHA256 = (
    "0ca020357e5745885bd120c9e96ba95e069c34ea7fb1eb745b7f9895f635e8b6"
)


def test_cross_repo_fingerprint_pin() -> None:
    scanner = hashlib.sha256(GATE.read_bytes()).hexdigest()
    denylist = hashlib.sha256(REAL_DENYLIST.read_bytes()).hexdigest()
    assert scanner == PINNED_SCANNER_SHA256, (
        "check_exposed_identifiers.py changed. Apply the SAME edit to the other "
        "public repo's copy (omnimarket <-> omnibase_infra), then update "
        f"PINNED_SCANNER_SHA256 in BOTH copies of this test to {scanner}."
    )
    assert denylist == PINNED_DENYLIST_SHA256, (
        "exposed_identifiers_denylist.json changed. A denylist entry added in one "
        "public repo but not the other leaves that repo unguarded against the very "
        "identifier someone just decided was forbidden. Apply the same edit to the "
        f"other repo, then update PINNED_DENYLIST_SHA256 in both to {denylist}."
    )


def test_fingerprint_subcommand_reports_both_digests() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(GATE),
            "--denylist",
            str(REAL_DENYLIST),
            "--print-fingerprint",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert PINNED_SCANNER_SHA256 in result.stdout, result.stdout
    assert PINNED_DENYLIST_SHA256 in result.stdout, result.stdout
