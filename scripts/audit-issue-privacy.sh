#!/usr/bin/env bash
# Scan GitHub issues/PRs/comments and the local tree for private-mail leaks
# (#274 standing policy; historical #115).
# Requires: gh auth login, network access.
#
# Env:
#   SKIP_HISTORY=1  — skip git-history deleted issue-asset blob checks
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! command -v gh >/dev/null; then
  echo "error: gh CLI required" >&2
  exit 1
fi

export SKIP_HISTORY="${SKIP_HISTORY:-0}"

python3 << 'PY'
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(".").resolve()
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
SAFE_DOMAINS = {
    "example.com",
    "example.org",
    "example.gov",
    "example.net",
    "company.com",
    "github.com",
    "users.noreply.github.com",
    "x.com",
    # Public mailing-list examples used in unsubscribe tests / docs
    "gnu.org",
    "lists.gnu.org",
    "octave.org",
}
# Public maintainer identities (packaging + intentional git authorship).
MAINTAINER_EMAILS = {
    "mbrennwa@gmail.com",
    "matthias@brennwald.org",
}
# Public packaging identity — not a privacy leak by itself.
PUBLIC_IDENTITY = re.compile(r"Matthias\s+Brennwald", re.I)
SUSPECT_WORDS = re.compile(
    r"(?<![A-Za-z])brennwald(?![A-Za-z])|"
    r"klotzholz|ebaugesuche|gasometrix|turicumfit|(?<![A-Za-z])turicum(?![A-Za-z])|"
    r"(?<![A-Za-z])eawag(?![A-Za-z])|(?<![A-Za-z])martone(?![A-Za-z])|"
    r"(?<![A-Za-z])kigam(?![A-Za-z])|magicline|matthias@|käferholzstrasse",
    re.I,
)
WORD_ALLOW = {
    "scripts/audit-issue-privacy.sh",
    "scripts/prepare-demo-screenshot.sh",
    "scripts/redact-issue-privacy.local.json.example",
}
TREE_SKIP_DIRS = {".git", ".venv", "__pycache__", "dist", "build", "node_modules"}
TREE_SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
    ".mo", ".so", ".pyc", ".woff", ".woff2", ".ttf", ".otf",
}
DOTDIR_ALLOW = {".github"}

found: list[tuple[str, str, str]] = []


def email_ok(addr: str) -> bool:
    low = addr.lower()
    if low in MAINTAINER_EMAILS:
        return True
    domain = low.split("@", 1)[1]
    if domain in SAFE_DOMAINS:
        return True
    if domain.endswith(".example") or domain.endswith(".test") or domain.endswith(".invalid"):
        return True
    if "example.com" in domain or "example.org" in domain or "example.net" in domain:
        return True
    if domain.endswith(".localhost") or domain == "localhost":
        return True
    if "github" in domain:
        return True
    return False


def scrub_public(text: str) -> str:
    cleaned = PUBLIC_IDENTITY.sub("PUBLIC_MAINTAINER", text or "")
    # Drop allowed maintainer addresses so brand/name tokens inside them
    # (e.g. brennwald.org) do not trip SUSPECT_WORDS.
    for addr in MAINTAINER_EMAILS:
        cleaned = re.sub(re.escape(addr), "PUBLIC_MAINTAINER_EMAIL", cleaned, flags=re.I)
    return cleaned


def scan_text(source: str, where: str, text: str) -> None:
    raw = text or ""
    cleaned = scrub_public(raw)
    if "user-attachments/assets/" in cleaned:
        found.append((source, where, "user-attachments screenshot"))
    for m in EMAIL_RE.findall(raw):
        if not email_ok(m):
            found.append((source, where, m))
    for m in SUSPECT_WORDS.finditer(cleaned):
        found.append((source, where, f"suspect word: {m.group(0)}"))


def gh_api(args: list[str]) -> object:
    return json.loads(subprocess.check_output(["gh", "api", *args], text=True))




def fetch_tracker() -> list[tuple[str, dict]]:
    """Return (label, node) for all issues and PRs with bodies/comments."""
    out: list[tuple[str, dict]] = []
    for kind, states in (
        ("issues", "OPEN"),
        ("issues", "CLOSED"),
        ("pullRequests", "OPEN"),
        ("pullRequests", "MERGED"),
        ("pullRequests", "CLOSED"),
    ):
        cursor = None
        while True:
            after = f', after: "{cursor}"' if cursor else ""
            query = f"""
            query {{
              repository(owner: "mbrennwa", name: "post") {{
                {kind}(first: 50{after}, states: {states}) {{
                  pageInfo {{ hasNextPage endCursor }}
                  nodes {{
                    number
                    title
                    body
                    comments(first: 100) {{
                      nodes {{ body author {{ login }} }}
                    }}
                  }}
                }}
              }}
            }}
            """
            data = gh_api(["graphql", "-f", f"query={query}"])
            if "errors" in data:
                print(json.dumps(data["errors"], indent=2), file=sys.stderr)
                raise SystemExit("graphql error")
            conn = data["data"]["repository"][kind]
            for node in conn["nodes"]:
                out.append((f"#{node['number']}", node))
            if not conn["pageInfo"]["hasNextPage"]:
                break
            cursor = conn["pageInfo"]["endCursor"]
    # Dedupe by number (an issue appears once; PRs are separate numbers usually)
    seen: set[int] = set()
    unique: list[tuple[str, dict]] = []
    for label, node in out:
        n = node["number"]
        if n in seen:
            continue
        seen.add(n)
        unique.append((label, node))
    return unique


# --- GitHub tracker ---------------------------------------------------------
for label, node in fetch_tracker():
    scan_text(label, "title", node.get("title") or "")
    scan_text(label, "body", node.get("body") or "")
    for c in (node.get("comments") or {}).get("nodes") or []:
        author = ((c.get("author") or {}) or {}).get("login") or "?"
        scan_text(label, f"comment:{author}", c.get("body") or "")

# --- Repo tree --------------------------------------------------------------
assets = ROOT / ".github" / "issue-assets"
if assets.is_dir():
    for p in sorted(assets.iterdir()):
        if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            found.append(
                (
                    "tree",
                    str(p.relative_to(ROOT)),
                    "private screenshot file (delete; use README stub only)",
                )
            )

local_map = ROOT / "scripts" / "redact-issue-privacy.local.json"
if local_map.is_file():
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(local_map.relative_to(ROOT))],
        capture_output=True,
        text=True,
    )
    if tracked.returncode == 0:
        found.append(
            (
                "tree",
                str(local_map.relative_to(ROOT)),
                "local redaction map is tracked (must be gitignored)",
            )
        )

for dirpath, dirnames, filenames in os.walk(ROOT):
    kept = []
    for d in dirnames:
        if d in TREE_SKIP_DIRS:
            continue
        if d.startswith(".") and d not in DOTDIR_ALLOW:
            continue
        kept.append(d)
    dirnames[:] = kept
    rel_dir = Path(dirpath).relative_to(ROOT)
    if any(part in TREE_SKIP_DIRS for part in rel_dir.parts):
        continue
    for name in filenames:
        path = Path(dirpath) / name
        rel = str(path.relative_to(ROOT))
        if path.suffix.lower() in TREE_SKIP_SUFFIXES:
            continue
        if rel.endswith(".local.json"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        cleaned = scrub_public(text)
        for m in EMAIL_RE.findall(text):
            if not email_ok(m):
                found.append(("tree", rel, m))
        if rel in WORD_ALLOW:
            continue
        for m in SUSPECT_WORDS.finditer(cleaned):
            found.append(("tree", rel, f"suspect word: {m.group(0)}"))

# --- Git history ------------------------------------------------------------
# Commit author emails are allowed (transparency). History is only scanned for
# private issue-asset screenshots that remain reachable after deletion.
skip_history = os.environ.get("SKIP_HISTORY", "0") == "1"
if not skip_history:
    hist = subprocess.run(
        [
            "git",
            "log",
            "--all",
            "--diff-filter=A",
            "--name-only",
            "--pretty=format:",
            "--",
            ".github/issue-assets/",
        ],
        capture_output=True,
        text=True,
    )
    for line in hist.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if Path(line).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            found.append(
                (
                    "history",
                    line,
                    "private screenshot still reachable via git history "
                    "(rewrite + force-push required)",
                )
            )

if found:
    print("Privacy audit FAILED — review and redact:", file=sys.stderr)
    for source, where, detail in sorted(set(found)):
        print(f"  {source} {where}: {detail}", file=sys.stderr)
    if any(s == "history" for s, _, _ in found):
        print(
            "\nHistory findings need a rewrite (e.g. git filter-repo) and "
            "force-push. Re-run with SKIP_HISTORY=1 to check tracker/tree only.",
            file=sys.stderr,
        )
    sys.exit(1)

print(
    "Privacy audit OK: issues/PRs/comments, repo tree"
    + ("" if skip_history else ", and git history issue-asset blobs")
    + " look clean."
)
PY
