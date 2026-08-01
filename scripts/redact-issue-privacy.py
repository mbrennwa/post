#!/usr/bin/env python3
"""Redaction helper for #115 privacy pruning (issues, PRs, comments).

Sensitive address/word maps live in an untracked local file so they are not
committed:

  scripts/redact-issue-privacy.local.json

Copy the example file and fill in real → placeholder pairs when needed:

  cp scripts/redact-issue-privacy.local.json.example \\
     scripts/redact-issue-privacy.local.json
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = "mbrennwa/post"
SCRIPT_DIR = Path(__file__).resolve().parent
LOCAL_MAP = SCRIPT_DIR / "redact-issue-privacy.local.json"

# Safe defaults committed in-tree (no real addresses). Extended by LOCAL_MAP.
EMAIL_MAP: dict[str, str] = {}
WORD_MAP: list[tuple[re.Pattern[str], str]] = []

IMG_MD = re.compile(r"!\[[^\]]*\]\(https://github\.com/user-attachments/assets/[^)]+\)")
IMG_HTML = re.compile(
    r"<img\b[^>]*src=\"https://github\.com/user-attachments/assets/[^\"]+\"[^>]*/?>",
    re.I,
)
ATTACH_URL = re.compile(r"https://github\.com/user-attachments/assets/[A-Za-z0-9-]+")

REDACTION_NOTE = "*(Screenshot redacted — #115 privacy audit.)*"


def _load_local_maps() -> None:
    global EMAIL_MAP, WORD_MAP
    if not LOCAL_MAP.is_file():
        print(
            f"note: {LOCAL_MAP.name} missing — only attachment stripping "
            "and built-in word patterns apply",
            file=sys.stderr,
        )
        return
    data = json.loads(LOCAL_MAP.read_text(encoding="utf-8"))
    emails = data.get("emails") or {}
    if not isinstance(emails, dict):
        raise SystemExit(f"{LOCAL_MAP}: 'emails' must be an object")
    EMAIL_MAP = {str(k): str(v) for k, v in emails.items()}
    extra_words = data.get("words") or []
    for entry in extra_words:
        if not isinstance(entry, dict) or "pattern" not in entry or "repl" not in entry:
            raise SystemExit(f"{LOCAL_MAP}: each words[] entry needs pattern + repl")
        flags = re.I if entry.get("ignore_case", True) else 0
        WORD_MAP.append((re.compile(entry["pattern"], flags), str(entry["repl"])))


def redact_text(text: str) -> str:
    if not text:
        return text
    out = text
    for old in sorted(EMAIL_MAP, key=len, reverse=True):
        out = out.replace(old, EMAIL_MAP[old])
    for pat, repl in WORD_MAP:
        out = pat.sub(repl, out)

    out = IMG_MD.sub(REDACTION_NOTE, out)
    out = IMG_HTML.sub(REDACTION_NOTE, out)
    out = ATTACH_URL.sub(REDACTION_NOTE, out)
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
        raw = subprocess.check_output(cmd, input=json.dumps(body), text=True)
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
    kind = "pull" if issue.get("pull_request") else "issue"
    print(f"  patched {kind} #{num} body")
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
    _load_local_maps()
    nums = [int(x) for x in sys.argv[1:]] or [
        64,
        115,
        117,
        118,
        150,
        171,
        188,
        197,
        208,
        222,
    ]
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
