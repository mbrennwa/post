#!/usr/bin/env bash
# Scan GitHub issues and comments for private-mail leaks (#115).
# Requires: gh auth login, network access.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! command -v gh >/dev/null; then
  echo "error: gh CLI required" >&2
  exit 1
fi

python3 << 'PY'
import json, re, subprocess, sys

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
SAFE_DOMAINS = {
    "example.com", "example.org", "example.gov", "company.com",
    "github.com", "users.noreply.github.com", "x.com",
}
# Public maintainer contact in packaging metadata — not a leak in issues.
MAINTAINER = {"mbrennwa@gmail.com"}

issues = json.loads(
    subprocess.check_output(
        ["gh", "issue", "list", "--state", "all", "--limit", "200", "--json", "number"],
        text=True,
    )
)
found = []
for row in issues:
    n = row["number"]
    data = json.loads(
        subprocess.check_output(
            ["gh", "issue", "view", str(n), "--json", "body,comments"],
            text=True,
        )
    )
    texts = [("body", data.get("body") or "")]
    for c in data.get("comments") or []:
        texts.append((f"comment:{c['author']['login']}", c.get("body") or ""))
    for where, text in texts:
        # Require /assets/ so checklist prose mentioning "user-attachments" is OK.
        if "user-attachments/assets/" in text:
            found.append((n, where, "user-attachments screenshot"))
        for m in EMAIL_RE.findall(text):
            if m.lower() in MAINTAINER:
                continue
            domain = m.split("@", 1)[1].lower()
            if (
                domain in SAFE_DOMAINS
                or domain.endswith(".example")
                or "example.com" in domain
                or "example.org" in domain
            ):
                continue
            if "github" in domain:
                continue
            found.append((n, where, m))

if found:
    print("Privacy audit FAILED — review and redact:", file=sys.stderr)
    for n, where, detail in sorted(set(found)):
        print(f"  #{n} {where}: {detail}")
    sys.exit(1)

print(
    "Privacy audit OK: no user-attachments screenshots or "
    "non-placeholder emails in issues/comments."
)
PY

# Repo tree (fast local grep). Prefer ripgrep when available.
TREE_PAT='brennwald|klotzholz|ebaugesuche|gasometrix|matthias@'
EXCLUDE_GLOBS=(
  '--glob' '!scripts/audit-issue-privacy.sh'
  '--glob' '!scripts/redact-issue-privacy.py'
  '--glob' '!scripts/prepare-demo-screenshot.sh'
  '--glob' '!.github/issue-assets/README.md'
  '--glob' '!.git/**'
  '--glob' '!.venv/**'
)
if command -v rg >/dev/null; then
  if rg -n "${EXCLUDE_GLOBS[@]}" -e "$TREE_PAT" . 2>/dev/null; then
    echo "Privacy audit FAILED: sensitive strings in repo tree" >&2
    exit 1
  fi
else
  if grep -RInE "$TREE_PAT" \
      --exclude=audit-issue-privacy.sh \
      --exclude=redact-issue-privacy.py \
      --exclude=prepare-demo-screenshot.sh \
      --exclude-dir=.git --exclude-dir=.venv --exclude-dir=__pycache__ \
      . 2>/dev/null \
      | grep -v '.github/issue-assets/README.md'; then
    echo "Privacy audit FAILED: sensitive strings in repo tree" >&2
    exit 1
  fi
fi

echo "Repo tree OK."
