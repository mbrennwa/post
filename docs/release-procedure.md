# Release procedure

Run this procedure for **every** release — testing tags (`vX.Y.Z.devN`),
release candidates, and final versions. Do not skip gates for a “just testing”
tag: the same public artifacts, issue history, and README are what people see.

Canonical checklist: follow the sections below in order. Record findings (and
any redactions) in the release issue or PR.

## 1. Version

1. Set `[project].version` in `pyproject.toml`.
2. Match `debian/changelog` upstream version to that value.
3. Choose the Git tag `v<version>` (same string as the project version).
   - Trailing `.devN` → GitHub Release is marked **prerelease** (see CI).
   - Otherwise → normal (final) release.

## 2. Privacy prune (#115) — hard gate

Private details must be gone from the **issue tracker**, the **git tree**, and
any other public project surface before tagging.

### What counts as private

- Real email addresses, names, hostnames, subjects, or message bodies (use
  `example.com` / `example.org` / RFC 5737 docs IPs in logs and fixtures).
- Screenshots or attachments that show real mail (`user-attachments` on GitHub,
  or files under `.github/issue-assets/`).
- Tokens, credentials, internal URLs, or debug dumps with personal data.

**Allowed:** public maintainer contact in packaging metadata
(`mbrennwa@gmail.com`); intentional placeholders; demo/`example.com` landing
screenshots (`./scripts/prepare-demo-screenshot.sh`).

### Checklist

1. Run the privacy audit (requires `gh` auth + network):

   ```bash
   ./scripts/audit-issue-privacy.sh
   ```

   It must exit 0. It scans all issues/comments for `user-attachments` media and
   non-placeholder emails, and greps the repo tree for known sensitive strings.

2. Scrub anything the audit reports:
   - **Issues / comments:** remove `<img … user-attachments …>` tags; replace
     real addresses/subjects in pasted logs with placeholders; note
     `*(Screenshot redacted — #115 privacy audit.)*`.
   - **Repo:** replace fixtures/docs/examples; delete or replace private files
     under `.github/issue-assets/` (and elsewhere). Prefer sanitized captures
     only.
   - **Landing page / README screenshots:** only after this pass is clean.

3. Spot-check recent PRs, release drafts, and wiki/Pages content the same way.

4. If private blobs were ever committed, decide whether a history rewrite is
   required before making the repo (more) public — deleting the file on `main`
   alone does not remove it from old commits.

**Do not tag until step 1 exits 0 and remaining manual items are done.**

## 3. License and provenance (#114)

1. Confirm `licensing/third_party.json` matches vendored assets under
   `data/icons/` and attributed modules (`eds.py`, `helpers.py` — EvolutionMCP
   MIT).
2. Spot-check high-risk areas: mail parsing (`src/post/mail/eds.py`,
   `compose.py`); no foreign copyright headers without attribution.
3. Verify `REUSE.toml` annotations cover non-GPL assets (Adwaita icons, LGPL
   license texts).
4. Record any findings in the release issue or PR; no incompatible unattributed
   third-party code in the release tree.

## 4. README feature status (#228)

Update the **Status** section in `README.md` so it reflects this release:

- **Implemented** — big-picture capabilities that ship and are usable.
- **Not yet** — big-picture gaps users should not assume exist.

Keep the lists short and product-level (accounts, compose, search, packaging,
…), not every open bug. Bump or date the section so it matches the version
being tagged. Align the landing-page pitch with the same story if it drifted.

## 5. Automated checks

```bash
./scripts/check.sh
```

Runs all unit tests (including licensing metadata in `tests/test_licensing.py`)
and `reuse lint`.

Ensure no Debian build artifacts are in the working tree before `reuse lint`
(run `make deb` only in a clean tree; build outputs belong in `dist/` and
untracked `debian/post/` staging).

## 6. Packaging (#4)

1. `debian/changelog` upstream version matches `pyproject.toml`.
2. `make deb` produces `dist/post_<version>_all.deb`.
3. Smoke-test install on Debian 12+ or Ubuntu 24.04+:

   ```bash
   sudo apt install ./dist/post_*_all.deb
   post
   ```

## 7. Tag and publish

1. Release blockers for this version are closed (or explicitly deferred with a
   note in the release issue).
2. Commit version + README Status + any checklist fixes on the release branch.
3. Tag and push:

   ```bash
   git tag v<version>
   git push origin v<version>
   ```

4. CI (`.github/workflows/release-deb.yml`) builds the `.deb` and attaches it to
   the GitHub Release (prerelease flag set automatically for `.devN` versions).

## 8. Post-tag verify

1. GitHub Release page shows the expected `.deb` and prerelease flag.
2. Install instructions on the landing page and README still match the Releases
   URL and version.
3. Privacy audit still passes after any last-minute issue edits tied to the
   release announcement.
