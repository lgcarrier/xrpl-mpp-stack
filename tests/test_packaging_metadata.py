from __future__ import annotations

from pathlib import Path
import tomllib


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGES_DIR = REPO_ROOT / "packages"
PACKAGE_DIRS = ("core", "facilitator", "middleware", "client", "payer")


def test_each_package_has_editable_build_metadata() -> None:
    for package_dir in PACKAGE_DIRS:
        pyproject_path = PACKAGES_DIR / package_dir / "pyproject.toml"
        assert pyproject_path.exists(), f"missing {pyproject_path}"

        pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

        assert pyproject["build-system"]["build-backend"] == "hatchling.build"
        assert "editables>=0.5" in pyproject["build-system"]["requires"]
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
