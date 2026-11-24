# Post

**Post** is a free and open-source mail app for GNOME. Read and send email with a simple, native desktop experience on Linux, using your accounts from GNOME Online Accounts for IMAP, Gmail, Microsoft 365, and local machine mail. No bloat: no calendars, no to-dos, no newsfeeds, no contacts manager.

Post is in public alpha. Do not assume it is bulletproof for productive environments.

Project site: [mbrennwa.github.io/post](https://mbrennwa.github.io/post)


## Status

Big-picture capabilities for the current tree (refresh on every release — see
[docs/release-procedure.md](docs/release-procedure.md)).

**Implemented**

- IMAP, Gmail, Microsoft 365, and local machine mail (through GNOME Online Accounts)
- Read mail: accounts/folders, message list, HTML reader (inline + separate window); clickable links in HTML and plain text; destination URL on hover
- Calendar invites: meeting details and Add to Calendar (Evolution-checked calendars)
- Search over message headers and bodies
- Compose: new / reply / reply-all / forward; attachments; drafts; signatures; correspondent autocomplete
- Outbox with delayed send
- Offline: per-account take-offline, body-cache prefs, queued send / moves / drafts
- Archive and trash with undo
- Live folder sync; create / rename / delete folders; Empty Trash
- Desktop mailto: handling
- Unsubscribe from mailing lists
- .DEB installer package (Debian and derived distros that ship Evolution Data Server 3.62 or newer)
- .RPM installer package (Fedora releases that ship Evolution Data Server 3.62 or newer)

**Not yet**

- HTML / rich-text compose, spell check, inline media
- Unified inbox (single merged cross-account message list)
- Drag-and-drop of messages onto folders, arbitrary message move with folder picker
- Access GNOME address book for compose autocomplete
- Full header details view
- Desktop notifications for new mail


## Install

**Platform requirement:** Post needs **Evolution Data Server (EDS) 3.62 or
newer**. EDS 3.62 removed the Camel search call that older Post versions used, and
the old and new calls do not exist together in any release, so there is no fallback.
Distros that ship an older EDS cannot install Post: this includes Debian stable
(Debian 13 "trixie" ships EDS 3.56), Debian testing (also 3.56 when this was
written) and any Ubuntu or Fedora release whose EDS is older than 3.62. Debian
unstable (sid) ships 3.62. Check
`apt policy evolution-data-server` or `dnf info evolution-data-server` before
installing. The installers and Post itself check the version and stop with a
clear message on an older system.

On a DEB-based distro with EDS 3.62 or newer (for example Debian unstable):

**From a release `.deb`** (see [GitHub Releases](https://github.com/mbrennwa/post/releases)):

```bash
sudo apt install ./dist/post_1.0.0a2_all.deb
post
```

On Fedora with EDS 3.62 or newer:

**From a release `.rpm`** (see [GitHub Releases](https://github.com/mbrennwa/post/releases)):

```bash
sudo dnf install ./dist/post-1.0.0a2-1.noarch.rpm
post
```

**From source** — install system dependencies (GObject introspection typelibs and
related tools — not installable via pip), then run Post:

```bash
./scripts/install-deps.sh
./run.sh
```

**Build packages locally:**

```bash
make deb        # .deb → dist/ (Debian/Ubuntu build host)
make rpm        # .rpm → dist/ (Fedora build host)
make packages   # both (requires both toolchains)
```

See `tools/howto-build-deb.txt` and `tools/howto-build-rpm.txt` for
build-machine prerequisites.

Canonical runtime package lists live in **`pyproject.toml`** under
**`[tool.deb].apt_depends`** (DEB) and **`[tool.rpm].dnf_requires`** (RPM).
Post uses a venv with `--system-site-packages` so PyGObject and GI typelibs
from the distro are visible to the app.

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

Known third-party code and artwork: EvolutionMCP (MIT) and the Adwaita GNOME Online Accounts `@` glyph (LGPL-3.0-or-later), documented in [LICENSES/](LICENSES/) and [licensing/third_party.json](licensing/third_party.json). The `@` geometry is composed into the app icon at `data/icons/hicolor/scalable/apps/io.github.mbrennwa.Post.svg`.

This project was developed with substantial assistance from AI coding tools. The code was reviewed through iterative development, discussion, testing, and acceptance by the project maintainer.

### Releases

Every release (testing, final, or otherwise) follows
**[docs/release-procedure.md](docs/release-procedure.md)** — privacy prune,
license/provenance, update README Implemented / Not yet, automated checks,
packaging smoke test, then tag. Privacy audit:

```bash
./scripts/audit-issue-privacy.sh
./scripts/check.sh
```

CI runs unit tests and `reuse lint` on every push, pull request, and release:

- **Unit tests** — including licensing checks in `tests/test_licensing.py` (SPDX headers, known third-party attribution, license file consistency).
- **REUSE lint** — verifies REUSE metadata across the repository.

These checks enforce project licensing hygiene but **do not** detect unknown copied code from external sources. Periodic manual review or external scanners (e.g. ScanCode) remain recommended before major releases.
