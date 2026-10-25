"""Point documentation links that leave docs/ at the repository, README anchors at the quickstart,
and `website:` links at the Kebivo product website (extra.website_url)."""

import re

LINK = re.compile(r"\]\((\.\./[^)#\s]*)(#[^)\s]*)?\)")
WEBSITE_LINK = re.compile(r"\]\(website:([^)\s]*)\)")


def on_page_markdown(markdown, page, config, files):
    repo = (config.get("repo_url") or "").rstrip("/")
    website = (config.get("extra", {}).get("website_url") or "").rstrip("/") + "/"

    def repl(match):
        target, anchor = match.group(1)[3:], match.group(2) or ""
        if target == "README.md" and anchor == "#quickstart":
            return "](quickstart.md)"
        kind = "tree" if target.endswith("/") else "blob"
        return f"]({repo}/{kind}/main/{target}{anchor})"

    markdown = WEBSITE_LINK.sub(lambda match: f"]({website}{match.group(1)})", markdown)
    return LINK.sub(repl, markdown)
