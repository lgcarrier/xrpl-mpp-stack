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
    assert "sessionId" not in doc
    assert "channelId" in doc
    assert "cumulativeAmount" in doc
    assert "InvoiceID" in doc


def test_package_chooser_links_all_guides_and_live_pypi_versions() -> None:
    doc = read_repo_text("docs/index.md")

    assert "## Package Chooser" in doc
    assert "| Package | PyPI | Install | Use when |" in doc
    assert "install only when it shows a compatible `0.2.x` release" in doc

    for package_dir in (
        "core",
        "facilitator",
        "middleware",
        "client",
        "payer",
        "mcp",
    ):
        package_name = f"xrpl-mpp-{package_dir}"
        assert f"(packages/{package_dir}.md)" in doc
        assert (
            f"https://img.shields.io/pypi/v/{package_name}"
            "?logo=pypi&logoColor=white"
        ) in doc
        assert f"(https://pypi.org/project/{package_name}/)" in doc
        assert f"`pip install {package_name}`" in doc


def test_release_playbook_lists_every_trusted_publisher_environment() -> None:
    doc = read_repo_text("docs/release.md")
    normalized_doc = " ".join(doc.split())

    for package_dir in (
        "core",
        "facilitator",
        "middleware",
        "client",
        "payer",
        "mcp",
    ):
        assert f"`testpypi-{package_dir}`" in doc
        assert f"`pypi-{package_dir}`" in doc

    assert "configure the pending publisher on TestPyPI and PyPI" in normalized_doc
