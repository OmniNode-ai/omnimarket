from pathlib import Path


def test_harness_parity_doc_exists_and_has_required_sections() -> None:
    doc = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "reference"
        / "2026-04-30-harness-parity-followup.md"
    )
    assert doc.exists(), "harness parity follow-up doc missing"
    content = doc.read_text(encoding="utf-8")
    for section in (
        "# Harness Parity Follow-Up",
        "## Boundary",
        "## Out of Scope",
        "## Follow-Up Tickets",
    ):
        assert section in content, f"missing section: {section}"
