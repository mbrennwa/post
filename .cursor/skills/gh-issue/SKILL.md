---
name: gh-issue
description: >-
  Runs the phased GitHub issue workflow: analyze root cause, plan on a
  dedicated branch (posting analysis/discussion to the GH issue), implement,
  then ship (commit, push, PR, merge, update issue). Use when the user says
  analyze #N, plan, implement, or ship for a GitHub issue, or asks to work an
  issue through the standard issue loop.
---

# GitHub issue workflow

Phased workflow for working a GH issue. Human gates (discuss, refine plan, manual test) stay interactive — do not skip them or auto-advance phases.

## Triggers

| Phrase | Phase |
|--------|--------|
| `analyze #<n>` / `analyze issue <n>` | Analyze |
| `plan` (after analyze/discuss) | Plan |
| `implement` (after plan approved) | Implement |
| `ship` (after manual testing) | Ship |

If the issue number is missing and not clear from conversation, ask once.

## Conventions

- Branch: `issue-<n>/<short-slug>` from up-to-date `main` (e.g. `issue-104/inbound-volume-sync`)
- Integrate via PR into `main` (do not push commits directly to `main`)
- Reference the issue in commits/PR body (`#<n>`)
- Follow the user's git commit / PR rules when shipping
- Never force-push `main`; never skip hooks unless the user asks

## Phase: Analyze

**Goal:** Root-cause analysis only. No code changes, no branch, no commits.

1. Fetch the issue: `gh issue view <n> --comments`
2. Explore the codebase for relevant code paths
3. Report:
   - **Summary** — what the issue asks for
   - **Root cause** — why it happens (or why the feature is missing), with file/symbol pointers
   - **Options** — 1–3 approaches with trade-offs
   - **Open questions** — anything blocking a good plan
4. Stop. Wait for discussion before planning.

## Phase: Plan

**Goal:** Agreed implementation plan on a dedicated branch. Prefer Plan mode when available.

1. Confirm the chosen approach from discussion
2. Update the GH issue with analysis + discussion details (before or right as planning starts):
   - Comment via `gh issue comment <n> --body "$(cat <<'EOF' … EOF)"`
   - Include: root-cause summary, options considered, chosen approach (and why), key discussion decisions, remaining open questions
   - Do not close the issue
3. Ensure `main` is current; create/checkout `issue-<n>/<short-slug>`
4. Write a concise plan:
   - Scope / non-goals
   - Steps (ordered)
   - Files likely touched
   - Test plan (what the user should verify manually)
5. Stop for refinement. Do not implement until the user says `implement` (or clearly approves).

## Phase: Implement

**Goal:** Execute the approved plan on the issue branch.

1. Confirm you are on `issue-<n>/…` (create it if plan skipped branch creation)
2. Implement only what the plan covers; match existing project style
3. Run relevant automated checks if cheap/obvious (e.g. targeted tests)
4. Summarize what changed and remind the user to **manually test** before `ship`
5. Do not commit unless the user asks (or they say `ship`)

## Phase: Ship

**Goal:** Land the work and update GitHub. Only after the user has tested (or explicitly waives testing).

1. Status check: branch, diff, recent commit style
2. Commit (user's commit protocol; HEREDOC message; include `#<n>`)
3. Push branch and open PR with `gh pr create` (summary + test plan; link `#<n>`)
4. Merge PR into `main` when appropriate (`gh pr merge`), unless the user asked to leave it open
5. Update the issue: comment with PR/outcome; close if fixed (`gh issue close <n>` or close via PR keywords)
6. Report PR URL and final issue state

If anything is dirty/unrelated or checks fail, stop and report — do not force through.
