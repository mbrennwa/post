# Post

**Post** is a free and open-source mail app for GNOME. Read and send email with a simple, native desktop experience on Linux, using your accounts from GNOME Online Accounts for IMAP, Gmail, Microsoft 365, and local machine mail. No bloat: no calendars, no to-dos, no newsfeeds, no contacts manager.

Post is in active development. Do not assume it is bulletproof for productive environments.

Project site: [mbrennwa.github.io/post](https://mbrennwa.github.io/post)


## Status

Big-picture capabilities for the current tree (refresh on every release — see
[docs/release-procedure.md](docs/release-procedure.md)).

**Implemented**

- GNOME Online Accounts: IMAP, Gmail, Microsoft 365, and local machine mail
- Read mail: accounts/folders, message list, reader pane
- Compose, reply, forward; file attachments
- Outbox and delayed send
- Per-account online / take-offline
- Offline body cache and search over cached content
- Archive, trash, and junk handling
- Installable `.deb` for Debian 12+ / Ubuntu 24.04+
- Project landing page

**Not yet**

- HTML / rich-text compose
- Conversation threading
- Unified inbox across accounts
- RPM packages
- Spell check in the composer
- Drag-and-drop messages between folders
- Move-to arbitrary folder picker
- GNOME address book for compose autocomplete


## Install

On Debian 12+, Ubuntu 24.04+, or similar DEB-based distros:

**From a release `.deb`** (see [GitHub Releases](https://github.com/mbrennwa/post/releases)):

```bash
sudo apt install ./dist/post_0.1.0_all.deb
post
```

**From source** — install system dependencies (GObject introspection typelibs and
related tools — not installable via pip), then run Post:

```bash
./scripts/install-deps.sh
./run.sh
```

**Build a `.deb` locally:**

```bash
make deb
# artifacts in dist/
```

See `tools/howto-build-deb.txt` for build-machine prerequisites.

The canonical runtime package list lives in **`pyproject.toml`** under
**`[tool.deb].apt_depends`**. Post uses a venv with
`--system-site-packages` so PyGObject and GI typelibs from the distro are
visible to the app.

## Development

Mail I/O runs on a dedicated background thread; see **[docs/mail-threading.md](docs/mail-threading.md)** for architecture, contributor rules, and the manual regression matrix.

On a fresh machine, install system dependencies first:

```bash
./scripts/install-deps.sh
```

```bash
# Run unit tests
./scripts/check.sh

# Always-on diagnostics log: ~/.local/state/post/post.log
# (Settings → About → Open Log File)

# Run with mail I/O debug tracing (file + stderr)
POST_LOG_LEVEL=DEBUG ./run.sh
```

## License

GPL-3.0-or-later — see [LICENSE](LICENSE).

Known third-party code and artwork: EvolutionMCP (MIT) and the Adwaita GNOME Online Accounts `@` glyph (LGPL-3.0-or-later), documented in [LICENSES/](LICENSES/) and [licensing/third_party.json](licensing/third_party.json). The vendored `@` source lives at `data/icons/sources/adwaita-goa-at-symbol.svg`.

This project was developed with substantial assistance from AI coding tools. The code was reviewed through iterative development, discussion, testing, and acceptance by the project maintainer.

### Releases

Every release (testing, final, or otherwise) follows
**[docs/release-procedure.md](docs/release-procedure.md)** — privacy prune,
license/provenance, README Status update, automated checks, packaging smoke
test, then tag. Privacy audit:

```bash
./scripts/audit-issue-privacy.sh
./scripts/check.sh
```

CI runs unit tests and `reuse lint` on every push, pull request, and release:

- **Unit tests** — including licensing checks in `tests/test_licensing.py` (SPDX headers, known third-party attribution, license file consistency).
- **REUSE lint** — verifies REUSE metadata across the repository.

These checks enforce project licensing hygiene but **do not** detect unknown copied code from external sources. Periodic manual review or external scanners (e.g. ScanCode) remain recommended before major releases.
