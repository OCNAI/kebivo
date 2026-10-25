import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_license_inventory_is_current() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/license_inventory.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_license_inventory_flags_incompatible_licenses() -> None:
    spec = importlib.util.spec_from_file_location(
        "license_inventory", ROOT / "scripts" / "license_inventory.py"
    )
    assert spec and spec.loader
    inventory = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inventory)
    assert inventory.verdict("MIT") == "Compatible"
    assert inventory.verdict("Apache-2.0 OR BSD-3-Clause") == "Compatible"
    assert inventory.verdict("GPL-3.0-only OR MIT") == "Compatible"
    assert inventory.verdict("MIT AND GPL-3.0-only") == "**Review**"
    assert inventory.verdict("GPL-3.0-only") == "**Review**"
    assert inventory.verdict("MPL-2.0") == "**Review** (weak copyleft)"
    assert inventory.verdict("UNKNOWN") == "**Review**"
    assert not inventory.marker_applies("sys_platform == 'win32'")
    assert inventory.marker_applies("sys_platform != 'emscripten'")
