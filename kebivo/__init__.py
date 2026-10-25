"""Backend package for Kebivo."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version


def version() -> str:
    """Return the package version from pyproject's installed metadata."""
    try:
        return distribution_version("kebivo")
    except PackageNotFoundError:
        # Source checkout before installation; pyproject remains the only source.
        import tomllib
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "pyproject.toml"
        return str(tomllib.loads(path.read_text(encoding="utf-8"))["project"]["version"])
