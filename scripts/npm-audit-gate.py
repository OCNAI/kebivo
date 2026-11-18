"""Fail on any high or critical npm advisory in the full tree, except known unfixable ones.

Usage (from frontend/): npm audit --json | python ../scripts/npm-audit-gate.py

An allowed advisory must still be reported; once it is gone, drop it from ALLOWED
so the gate is back to plain `npm audit --audit-level=high`.
"""

import json
import sys

# Advisories with no patched release, reachable only through dev tooling.
ALLOWED = {
    "https://github.com/advisories/GHSA-fc5q-39xc-vjpf",  # picomatch ReDoS, <=4.0.7 (vite, vitest, typescript-eslint)
}

report = json.load(sys.stdin)
if "vulnerabilities" not in report:
    sys.exit(f"npm audit failed: {report.get('error', report)}")

found = {
    via["url"]: via
    for vuln in report["vulnerabilities"].values()
    for via in vuln["via"]
    if isinstance(via, dict) and via["severity"] in ("high", "critical")
}
failed = False
for url, via in sorted(found.items()):
    if url not in ALLOWED:
        print(f"{via['severity']}: {via['name']} {via['range']} {via['title']} {url}")
        failed = True
for url in sorted(ALLOWED - found.keys()):
    print(f"allowed advisory no longer reported, remove it from scripts/npm-audit-gate.py: {url}")
    failed = True
sys.exit(1 if failed else 0)
