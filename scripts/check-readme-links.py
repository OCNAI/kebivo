"""Fail if a relative link in README.md points at a missing file or a missing heading anchor."""

import re
import sys
from pathlib import Path
from urllib.parse import unquote

root = Path(__file__).resolve().parent.parent
LINK = re.compile(r'(?<!\!)\[[^\]]*\]\(([^)\s]+)\)|!\[[^\]]*\]\(([^)\s]+)\)|<(\.?\w[\w./-]*)>|\b(?:src|srcset)="([^"\s]+)"')


def slug(heading: str) -> str:
    text = re.sub(r"[`*_]", "", heading.strip().lower())
    return re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", text))


def anchors(path: Path) -> set[str]:
    return {slug(m.group(1)) for m in re.finditer(r"^#+\s+(.*)$", path.read_text(), re.M)}


readme = root / "README.md"
bad = []
for match in LINK.finditer(readme.read_text()):
    target = next(g for g in match.groups() if g)
    if re.match(r"[a-z]+:", target) or not (target.startswith("#") or "/" in target or "." in target):
        continue
    file_part, _, anchor = unquote(target).partition("#")
    path = readme if not file_part else root / file_part
    if not path.exists():
        bad.append(target)
    elif anchor and path.suffix == ".md" and anchor not in anchors(path):
        bad.append(target)
if bad:
    sys.exit("Broken README links: " + ", ".join(sorted(set(bad))))
print("README links ok")
