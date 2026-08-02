# Release procedure

Run this procedure for **every** release — testing tags (`vX.Y.Z.devN`),
release candidates, and final versions. Do not skip gates for a “just testing”
tag: the same public artifacts, issue history, and README are what people see.

Canonical checklist: follow the sections below in order. Record findings (and
any redactions) in the release issue or PR.

## 1. Version

1. Set `[project].version` in `pyproject.toml` (also `src/post/__init__.py`
   `__version__` fallback).
2. Match `debian/changelog` upstream version to that value.
3. Match `rpm/post.spec` `Version:` (and add a `%changelog` entry).
4. Choose the Git tag `v<version>` (same string as the project version).
   Example: version `1.0.0.dev1` → tag `v1.0.0.dev1`.
   - Trailing `.devN` → GitHub Release is marked **prerelease** (see CI).
   - Otherwise → normal (final) release.
5. Refresh install examples in `README.md` and `tools/howto-build-*.txt`
   so package filenames match.

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
(`mbrennwa@gmail.com`, `matthias@brennwald.org`); intentional git authorship
emails; intentional placeholders; demo/`example.com` landing screenshots
(`./scripts/prepare-demo-screenshot.sh`).

### Checklist

1. Run the privacy audit (requires `gh` auth + network):

   ```bash
   ./scripts/audit-issue-privacy.sh
   ```

   It must exit 0. It scans issues **and** PRs (bodies + comments) for
   `user-attachments` media, non-placeholder emails, and known brand/org
   strings; walks the repo tree for the same; refuses image files under
   `.github/issue-assets/`; and checks git history for deleted issue-asset
   screenshots (commit author emails are allowed). Use `SKIP_HISTORY=1` only
   when you intentionally want tracker/tree checks alone.

2. Scrub anything the audit reports:
   - **Issues / PRs / comments:** remove `<img … user-attachments …>` tags;
     replace real addresses/subjects/brand names in pasted logs with
     placeholders; note `*(Screenshot redacted — #115 privacy audit.)*`.
     Helper: copy `scripts/redact-issue-privacy.local.json.example` to
     `scripts/redact-issue-privacy.local.json` (gitignored), fill maps, then
     `python3 scripts/redact-issue-privacy.py [nums…]`.
   - **Repo:** replace fixtures/docs/examples; delete private files under
     `.github/issue-assets/` (and elsewhere). Prefer sanitized captures only.
     Never commit the local redaction map.
   - **Landing page / README screenshots:** only after this pass is clean.

3. Spot-check release drafts and wiki/Pages content the same way.

4. If private **blobs** (e.g. issue-asset screenshots) remain in **git history**,
   rewrite before making the repo public (deleting a file on `main` alone does
   not remove it from old commits). That requires `git filter-repo` (or
   equivalent) and a force-push — coordinate with anyone who has clones.
   Commit author emails are not treated as a privacy failure.

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

## 4. Update README Implemented vs Not yet (#228)

Before tagging, refresh the **Status** section in `README.md`:

1. **Implemented** — big-picture capabilities that ship and are usable in this
   release.
2. **Not yet** — big-picture gaps users should not assume exist.
3. Move items between the lists when something landed or was dropped; remove
   items that are no longer relevant (do not list speculative roadmap).

Keep the lists short and product-level (accounts, compose, search, packaging,
…), not every open bug. Align the landing-page pitch with the same story if it
drifted.

## 5. Automated checks

```bash
./scripts/check.sh
```

Runs all unit tests (including licensing metadata in `tests/test_licensing.py`)
and `reuse lint`.

Ensure no package build artifacts are in the working tree before `reuse lint`
(run `make deb` / `make rpm` only in a clean tree; build outputs belong in
`dist/`, untracked `debian/post/` staging, and `.rpmbuild/`).

## 6. Packaging (#4, #227)

1. `debian/changelog` upstream version matches `pyproject.toml`.
2. `rpm/post.spec` `Version:` matches `pyproject.toml` (update `%changelog`).
3. `make deb` produces `dist/post_<version>_all.deb`.
4. `make rpm` produces `dist/post-<version>-1.noarch.rpm` (Fedora host or
   container with `rpm-build`).
5. Smoke-test install:

   ```bash
   # Debian 12+ / Ubuntu 24.04+
   sudo apt install ./dist/post_*_all.deb
   post

   # Fedora
   sudo dnf install ./dist/post-*-1.noarch.rpm
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

4. CI (`.github/workflows/release-deb.yml`) builds the `.deb` and `.rpm` and
   attaches both to the GitHub Release (prerelease flag set automatically for
   `.devN` versions).

## 8. Post-tag verify

1. GitHub Release page shows the expected `.deb`, `.rpm`, and prerelease flag.
2. Install instructions on the landing page and README still match the Releases
   URL and version.
3. Privacy audit still passes after any last-minute issue edits tied to the
   release announcement.
