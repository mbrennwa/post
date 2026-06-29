# Pre-release checklist

Repeat before tagging a release (e.g. `v0.1.0`).

## Automated checks

```bash
./scripts/check.sh
```

This runs all unit tests (including licensing metadata in `tests/test_licensing.py`) and `reuse lint`.

Ensure no Debian build artifacts are in the working tree before `reuse lint` (run `make deb` only in a clean tree; build outputs belong in `dist/` and untracked `debian/post/` staging).

## License and provenance (#114)

1. Confirm `licensing/third_party.json` matches vendored assets under `data/icons/` and attributed modules (`eds.py`, `helpers.py` — EvolutionMCP MIT).
2. Spot-check high-risk areas: mail parsing (`src/post/mail/eds.py`, `compose.py`), no foreign copyright headers without attribution.
3. Verify `REUSE.toml` annotations cover non-GPL assets (Adwaita icons, LGPL license texts).
4. Record any findings in the release issue or PR; no incompatible unattributed third-party code in the release tree.

## Private snippets (#115)

1. Grep for real emails, tokens, credentials in source, docs, and tests (use `example.com` placeholders).
2. Review GitHub issues for pasted logs or screenshots with private mail.
3. Landing-page screenshots only after this pass.

## Packaging (#4)

1. `debian/changelog` upstream version matches `pyproject.toml` `[project].version`.
2. `make deb` produces `dist/post_<version>_all.deb`.
3. Smoke-test install on Debian 12+ or Ubuntu 24.04+: `sudo apt install ./dist/post_*_all.deb` then `post`.

## Release

1. All P0 child issues for the epic closed.
2. `git tag v<version> && git push origin v<version>` — CI attaches `.deb` to GitHub Releases.
3. Landing page install instructions match README and Releases URL.
