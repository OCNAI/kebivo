"""Generate docs/licenses.md, the third-party license inventory of the runtime image.

Usage (from the repository root, after `make install`):
    uv run python scripts/license_inventory.py           # rewrite docs/licenses.md
    uv run python scripts/license_inventory.py --check   # exit 1 if it is out of date

Standard library only. Python packages are the runtime dependency closure of the
`kebivo` package in uv.lock (no dev extras), with environment markers evaluated
for the Linux CPython container image. Their license comes from the installed
distribution metadata (License-Expression, else the license classifiers, else a
short License field). npm packages are the non-dev entries of
frontend/package-lock.json, which are bundled into the built UI. Licenses outside
the Apache-2.0-compatible allowlist are flagged for review.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "docs" / "licenses.md"
ROOT_PACKAGE = "kebivo"

# The container image is Linux CPython; uv.lock markers are evaluated for it.
MARKER_ENV = {
    "sys_platform": "linux",
    "platform_system": "Linux",
    "os_name": "posix",
    "platform_python_implementation": "CPython",
    "implementation_name": "cpython",
    "platform_machine": "x86_64",
    "python_version": "3.12",
    "python_full_version": "3.12.3",
}

# Permissive licenses that may be combined with an Apache-2.0 distribution.
COMPATIBLE = {
    "0BSD",
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "ISC",
    "MIT",
    "MIT-0",
    "MIT-CMU",
    "PSF-2.0",
    "Python-2.0",
    "Unlicense",
    "CC0-1.0",
    "Zlib",
}
# Weak copyleft: usable unmodified, but the notice and source-offer duties need review.
REVIEW = {"MPL-2.0", "LGPL-2.1-or-later", "LGPL-3.0-or-later", "EPL-2.0"}

# Explanations printed under "Review notes" for each flagged license.
REVIEW_NOTES = {
    "MPL-2.0": "File-level copyleft. Kebivo ships the files unmodified, which an Apache-2.0 "
    "distribution may include; keep the MPL notice and point to the upstream source. "
    "Modified MPL files would have to stay under MPL-2.0.",
}

CLASSIFIERS = {
    "Apache Software License": "Apache-2.0",
    "BSD License": "BSD-3-Clause",
    "MIT License": "MIT",
    "ISC License (ISCL)": "ISC",
    "Python Software Foundation License": "PSF-2.0",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
}
ALIASES = {
    "Apache 2.0": "Apache-2.0",
    "Apache License 2.0": "Apache-2.0",
    "Apache Software License": "Apache-2.0",
    "BSD": "BSD-3-Clause",
    "MIT License": "MIT",
}


def marker_applies(marker: str | None) -> bool:
    if not marker:
        return True
    expression = re.sub(
        r"\b(" + "|".join(MARKER_ENV) + r")\b", lambda m: repr(MARKER_ENV[m.group(1)]), marker
    )
    return bool(eval(expression, {"__builtins__": {}}, {}))  # noqa: S307 - uv.lock markers only


def python_closure() -> dict[str, str]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = {package["name"]: package for package in lock["package"]}
    found: dict[str, str] = {}
    pending = [(ROOT_PACKAGE, ())]
    while pending:
        name, extras = pending.pop()
        package = packages[name]
        edges = list(package.get("dependencies", []))
        for extra in extras:
            edges += package.get("optional-dependencies", {}).get(extra, [])
        for edge in edges:
            if not marker_applies(edge.get("marker")):
                continue
            dependency = edge["name"]
            new_extras = tuple(edge.get("extra", []))
            if dependency not in found or new_extras:
                found[dependency] = packages[dependency]["version"]
                pending.append((dependency, new_extras))
    found.pop(ROOT_PACKAGE, None)
    return found


def python_license(name: str) -> str:
    try:
        meta = metadata.metadata(name)
    except metadata.PackageNotFoundError:
        return "UNKNOWN (not installed; run `make install`)"
    expression = meta.get("License-Expression")
    if expression:
        return normalize(expression)
    classified = [
        CLASSIFIERS.get(c.rsplit(" :: ", 1)[-1], c.rsplit(" :: ", 1)[-1])
        for c in meta.get_all("Classifier") or []
        if c.startswith("License :: ")
    ]
    if classified:
        return " OR ".join(sorted(set(classified)))
    text = (meta.get("License") or "").strip()
    if text and "\n" not in text and len(text) <= 60:
        return normalize(text)
    return "UNKNOWN"


def normalize(expression: str) -> str:
    return ALIASES.get(expression.strip(), expression.strip())


def npm_packages() -> list[tuple[str, str, str]]:
    lock = json.loads((ROOT / "frontend" / "package-lock.json").read_text())
    rows = []
    for path, entry in lock["packages"].items():
        if not path or entry.get("dev") or entry.get("devOptional"):
            continue
        name = path.rsplit("node_modules/", 1)[-1]
        rows.append((name, entry.get("version", "?"), normalize(entry.get("license", "UNKNOWN"))))
    return sorted(rows)


def verdict(expression: str) -> str:
    """`OR` means a choice: one compatible alternative is enough. `AND` needs all."""
    options = [part.strip(" ()") for part in re.split(r"\s+OR\s+", expression)]
    for option in options:
        terms = [term.strip(" ()") for term in re.split(r"\s+AND\s+", option)]
        if all(term in COMPATIBLE for term in terms):
            return "Compatible"
    if any(term.strip(" ()") in REVIEW for term in re.split(r"\s+(?:OR|AND)\s+", expression)):
        return "**Review** (weak copyleft)"
    return "**Review**"


def render() -> str:
    python_rows = [(n, v, python_license(n)) for n, v in sorted(python_closure().items())]
    npm_rows = npm_packages()
    flagged = [row for row in python_rows + npm_rows if verdict(row[2]) != "Compatible"]
    lines = [
        "# Third-party licenses",
        "",
        "<!-- Generated by scripts/license_inventory.py. Do not edit by hand; run "
        "`uv run python scripts/license_inventory.py` after changing uv.lock or "
        "frontend/package-lock.json. `make check` fails while this file is stale. -->",
        "",
        "This inventory lists every third-party package that ships in the Kebivo container "
        "image: the runtime Python dependencies resolved in `uv.lock` for Linux CPython, and "
        "the npm packages bundled into the admin UI from `frontend/package-lock.json`. "
        "Development and test tools are not shipped and are not listed. License identifiers "
        "come from the package metadata and are not legal advice.",
        "",
        "Kebivo itself is licensed under Apache-2.0 (the `LICENSE` file in the repository root). "
        "The compatibility column is checked against Apache-2.0: permissive licenses are "
        "marked compatible, and an `OR` expression is compatible when one alternative is.",
        "",
        f"**Packages flagged for review: {len(flagged)}.**"
        + ("" if flagged else " Every listed license is compatible with Apache-2.0."),
        "",
        f"## Python runtime packages ({len(python_rows)})",
        "",
        "| Package | Version | License | Apache-2.0 |",
        "| --- | --- | --- | --- |",
    ]
    lines += [f"| {n} | {v} | {lic} | {verdict(lic)} |" for n, v, lic in python_rows]
    lines += [
        "",
        f"## npm packages in the admin UI bundle ({len(npm_rows)})",
        "",
        "| Package | Version | License | Apache-2.0 |",
        "| --- | --- | --- | --- |",
    ]
    lines += [f"| {n} | {v} | {lic} | {verdict(lic)} |" for n, v, lic in npm_rows]
    if flagged:
        lines += ["", "## Review notes", ""]
        for name, version, lic in flagged:
            note = REVIEW_NOTES.get(lic, "No compatibility rule; review before publication.")
            lines.append(f"- **{name} {version}** ({lic}): {note}")
    lines += [
        "",
        "## Other shipped components",
        "",
        "| Component | License | Notes |",
        "| --- | --- | --- |",
        "| Fonts | None bundled | The UI uses the operating system font stack (`system-ui`). |",
        "| Kebivo mark and favicon (`frontend/public/`) | Project asset | Created for this "
        "project; covered by the project license (Apache-2.0). |",
        "| `python:3.12-slim` base image | PSF-2.0 (Python) and Debian package licenses | "
        "Not inventoried here; inspect the image with an SBOM tool before redistribution. |",
        "| `uv` binary copied into the image | MIT OR Apache-2.0 | Installs the locked "
        "dependencies at build time and remains in the image. |",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if docs/licenses.md is stale")
    args = parser.parse_args()
    text = render()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text() != text:
            print(
                "docs/licenses.md is out of date; run `uv run python scripts/license_inventory.py`",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return
    OUTPUT.write_text(text)
    print(f"Wrote {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
