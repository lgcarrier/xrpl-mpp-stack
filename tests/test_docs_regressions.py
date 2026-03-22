from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def read_repo_text(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_changelog_is_mpp_only() -> None:
    changelog = read_repo_text("CHANGELOG.md")

    assert "x402" not in changelog
    assert "/verify" not in changelog
    assert "/settle" not in changelog


def test_replay_and_freshness_doc_matches_current_mpp_flow() -> None:
    doc = read_repo_text("docs/how-it-works/replay-and-freshness.md")

    assert "/verify" not in doc
    assert "/settle" not in doc
    assert "falls back to the first 32 hex characters" not in doc
    assert "invoiceId" in doc
    assert "sessionId" in doc
    assert "InvoiceID" in doc
