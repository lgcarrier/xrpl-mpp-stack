from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import tomllib

PACKAGE_NAME = "xrpl-mpp-facilitator"


def _read_local_pyproject_version() -> str | None:
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        with pyproject_path.open("rb") as handle:
            project = tomllib.load(handle)["project"]
    except (FileNotFoundError, KeyError, tomllib.TOMLDecodeError, TypeError):
        return None

    resolved_version = project.get("version")
    if isinstance(resolved_version, str) and resolved_version.strip():
        return resolved_version.strip()
    return None


def resolve_version() -> str:
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return _read_local_pyproject_version() or "0.0.0"


__version__ = resolve_version()

