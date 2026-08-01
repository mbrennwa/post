#!/usr/bin/env python3
"""One-shot redaction helper for #115 privacy pruning (issues + comments)."""
from __future__ import annotations

import json
import re
import subprocess
import sys

REPO = "mbrennwa/post"

# Real / sensitive addresses → placeholders
EMAIL_MAP = {
    "matthias@brennwald.org": "person@example.org",
    "matthias.brennwald@eawag.ch": "person@institute.example",
    "Matthias.Brennwald@eawag.ch": "person@institute.example",
    "info@gasometrix.com": "info@company.example",
    "info@klotzholz.com": "info@vendor.example",
    "m.martone@ctcag.ch": "colleague@example.com",
    "turicumfit@cc.magicline.com": "newsletter@example.com",
    "xyz@abc.com": "xyz@example.com",
    "email@account.abc": "email@account.example",
    "abc@x.com.extra": "abc@example.com.extra",
    "abc@x.com": "abc@example.com",
}

# Broader word replacements for account/host hints in prose
WORD_MAP = [
    (re.compile(r"\beawag\b", re.I), "institute"),
    (re.compile(r"\bbrennwald\b", re.I), "example"),
    (re.compile(r"\bgasometrix\b", re.I), "company"),
    (re.compile(r"\bklotzholz\b", re.I), "vendor"),
    (re.compile(r"Käferholzstrasse 173, 8046 Zürich", re.I), "Example Street 1, 8000 Example City"),
    (re.compile(r"Vollmacht Wärmepumpe[^\n<]*", re.I), "Example subject about a heat pump"),
]

IMG_MD = re.compile(r"!\[[^\]]*\]\(https://github\.com/user-attachments/assets/[^)]+\)")
IMG_HTML = re.compile(
    r"<img\b[^>]*src=\"https://github\.com/user-attachments/assets/[^\"]+\"[^>]*/?>",
    re.I,
)
# bare URLs on their own line
ATTACH_URL = re.compile(r"https://github\.com/user-attachments/assets/[A-Za-z0-9-]+")

REDACTION_NOTE = "*(Screenshot redacted — #115 privacy audit.)*"


def redact_text(text: str) -> str:
    if not text:
        return text
    out = text
    # Longest emails first
    for old in sorted(EMAIL_MAP, key=len, reverse=True):
        out = out.replace(old, EMAIL_MAP[old])
    for pat, repl in WORD_MAP:
        out = pat.sub(repl, out)

    had_img = bool(IMG_MD.search(out) or IMG_HTML.search(out) or ATTACH_URL.search(out))
    out = IMG_MD.sub(REDACTION_NOTE, out)
    out = IMG_HTML.sub(REDACTION_NOTE, out)
    out = ATTACH_URL.sub(REDACTION_NOTE, out)
    # Collapse duplicate consecutive notes
    out = re.sub(
        r"(\*\(Screenshot redacted — #115 privacy audit\.\)\*\s*){2,}",
        REDACTION_NOTE + "\n\n",
        out,
    )
    return out


def gh_api(method: str, path: str, body: dict | None = None) -> dict | list:
    cmd = ["gh", "api", "-X", method, path]
    if body is not None:
        cmd.extend(["--input", "-"])
        raw = subprocess.check_output(
            cmd, input=json.dumps(body), text=True
        )
    else:
        raw = subprocess.check_output(cmd, text=True)
    return json.loads(raw) if raw.strip() else {}


def patch_issue(num: int) -> bool:
    issue = gh_api("GET", f"repos/{REPO}/issues/{num}")
    body = issue.get("body") or ""
    new = redact_text(body)
    if new == body:
        return False
    gh_api("PATCH", f"repos/{REPO}/issues/{num}", {"body": new})
    print(f"  patched issue #{num} body")
    return True


def patch_comments(num: int) -> int:
    comments = gh_api("GET", f"repos/{REPO}/issues/{num}/comments?per_page=100")
    n = 0
    for c in comments:
        body = c.get("body") or ""
        new = redact_text(body)
        if new == body:
            continue
        gh_api("PATCH", f"repos/{REPO}/issues/comments/{c['id']}", {"body": new})
        print(f"  patched comment {c['id']} on #{num}")
        n += 1
    return n


def main() -> int:
    nums = [int(x) for x in sys.argv[1:]] or [64, 115, 117, 118, 150, 197, 208, 222]
    changed = 0
    for num in nums:
        print(f"#{num}")
        if patch_issue(num):
            changed += 1
        changed += patch_comments(num)
    print(f"done, patches={changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
