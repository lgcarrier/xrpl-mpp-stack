from __future__ import annotations

from pathlib import Path
import tomllib


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGES_DIR = REPO_ROOT / "packages"
PACKAGE_DIRS = ("core", "facilitator", "middleware", "client", "payer", "mcp")


def test_each_package_has_editable_build_metadata() -> None:
    for package_dir in PACKAGE_DIRS:
        pyproject_path = PACKAGES_DIR / package_dir / "pyproject.toml"
        assert pyproject_path.exists(), f"missing {pyproject_path}"

        pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

        assert pyproject["build-system"]["build-backend"] == "hatchling.build"
        assert "editables>=0.5" in pyproject["build-system"]["requires"]
        hatchling_requirement = next(
            requirement
            for requirement in pyproject["build-system"]["requires"]
            if requirement.startswith("hatchling")
        )
        assert hatchling_requirement == "hatchling>=1.27,<1.33"
        assert pyproject["project"]["name"].startswith("xrpl-mpp-")


def test_dev_requirements_install_all_packages_editable() -> None:
    requirements_dev = (REPO_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")

    for package_dir in PACKAGE_DIRS:
        assert f"-e ./packages/{package_dir}" in requirements_dev


def test_each_package_exposes_pypi_project_urls() -> None:
    for package_dir in PACKAGE_DIRS:
        pyproject_path = PACKAGES_DIR / package_dir / "pyproject.toml"
        pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        urls = pyproject["project"]["urls"]

        assert set(urls) == {"Documentation", "Source", "Issues", "Changelog"}
        assert urls["Documentation"].endswith(f"/packages/{package_dir}/")
        assert urls["Source"].endswith(f"/packages/{package_dir}")
        assert urls["Issues"].endswith("/issues")
        assert urls["Changelog"].endswith("/blob/main/CHANGELOG.md")


def test_each_wheel_uses_distribution_scoped_pep639_license_metadata() -> None:
    for package_dir in PACKAGE_DIRS:
        package_path = PACKAGES_DIR / package_dir
        pyproject = tomllib.loads(
            (package_path / "pyproject.toml").read_text(encoding="utf-8")
        )

        assert pyproject["project"]["license-files"] == ["LICENSE"]
        assert (package_path / "LICENSE").read_text(encoding="utf-8") == (
            REPO_ROOT / "LICENSE"
        ).read_text(encoding="utf-8")
        wheel = pyproject.get("tool", {}).get("hatch", {}).get("build", {}).get(
            "targets", {}
        ).get("wheel", {})
        assert "force-include" not in wheel


def test_docker_image_installs_payer_mcp_extra_for_agent_profile() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    ci = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "'/app/packages/payer[mcp]'" in dockerfile
    assert 'command: ["xrpl-mpp", "mcp"]' in compose
    assert "import fastmcp, xrpl_mpp_payer.mcp as module" in ci


def test_publish_workflow_is_verification_gated_and_testpypi_provenance_safe() -> None:
    publish = (
        REPO_ROOT / ".github/workflows/publish-package.yml"
    ).read_text(encoding="utf-8")
    conformance = (
        REPO_ROOT / ".github/workflows/conformance.yml"
    ).read_text(encoding="utf-8")

    assert "workflow_call:" in conformance
    assert "needs: [resolve, release-verification, conformance]" in publish
    assert '"xrpl-mpp-core==${PACKAGE_VERSION}"' in publish
    assert '"xrpl-mpp-client==${PACKAGE_VERSION}"' in publish
    assert "download --no-cache-dir --no-deps --only-binary=:all:" in publish
    assert 'artifacts=("$artifact_dir"/*.whl)' in publish
    assert '--extra-index-url "$EXTRA_INDEX_URL"' not in publish


def test_publish_workflow_bypasses_stale_index_caches_during_verification() -> None:
    publish = (
        REPO_ROOT / ".github/workflows/publish-package.yml"
    ).read_text(encoding="utf-8")

    assert publish.count("for attempt in $(seq 1 41); do") == 2
    assert publish.count("--no-cache-dir") == 4
    assert publish.count("if python -m pip install --no-cache-dir") == 2
    assert '--no-cache-dir --index-url "$DEPENDENCY_INDEX_URL"' in publish
    assert '--index-url "$INDEX_URL" "$requirement"; then' in publish
    assert publish.count('if [ "$attempt" -eq 41 ]; then') == 2
