# Post

**Post** is a mail app for GNOME — read and send email with a simple, native desktop experience. It is free and open source, built for Linux.

- **Your accounts, one place** — work across multiple mail accounts without clutter
- **Read mail comfortably** — folders, message list, and a clean reading pane
- **Native and lightweight** — a focused mail client, not a full personal information manager
- **Uses what you already have** — picks up online accounts from GNOME Online Accounts; optional local spool or Maildir in Settings

## Today

- Browse folders and read messages (paginated)
- Multiple accounts with a unified Inbox view
- HTML messages with remote images off by default (enable in Settings → Reading)
- Unread indicators, dates, and attachment hints in the message list
- Mark messages as read when opened; folder counts update
- Open attachments from the reading pane (right-click to save or open with…)
- Clear empty-folder and error states when loading fails
- Context menu: mark read/unread, flag, archive, move to trash (with undo)
- Compose plain-text **New Message** (Ctrl+N), **Reply**, **Reply All** (Ctrl+Shift+R), and **Forward**; send via account SMTP
- **Settings** (gear icon) — configure local mail from a system spool file or Maildir folder

## Planned

Compose & organization roadmap ([milestone](https://github.com/mbrennwa/post/milestone/1)):

1. ~~[Reply-all](https://github.com/mbrennwa/post/issues/20)~~
2. ~~[Forward](https://github.com/mbrennwa/post/issues/25)~~
3. [Save draft](https://github.com/mbrennwa/post/issues/8)
4. [Compose attachments](https://github.com/mbrennwa/post/issues/23)
5. [HTML compose](https://github.com/mbrennwa/post/issues/24)
6. [Conversation threading](https://github.com/mbrennwa/post/issues/26)

## Run

```bash
./run.sh
```

You need at least one mail account: add one in **Settings → Online Accounts**, or enable **local mail** in Post’s Settings (gear icon in the header).

## License

GPL-3.0-or-later — see [LICENSE](LICENSE).

Known third-party code and artwork: EvolutionMCP (MIT) and the Adwaita GNOME Online Accounts `@` glyph (LGPL-3.0-or-later), documented in [LICENSES/](LICENSES/) and [licensing/third_party.json](licensing/third_party.json). The vendored `@` source lives at `data/icons/sources/adwaita-goa-at-symbol.svg`.

This project was developed with substantial assistance from AI coding tools. The code was reviewed through iterative development, discussion, testing, and acceptance by the project maintainer.

### Compliance checks

Before releases, run:

```bash
./scripts/check.sh
```

CI runs the same checks on every push, pull request, and release:

- **Unit tests** — including licensing checks in `tests/test_licensing.py` (SPDX headers, known third-party attribution, license file consistency).
- **REUSE lint** — verifies REUSE metadata across the repository.

These checks enforce project licensing hygiene but **do not** detect unknown copied code from external sources. Periodic manual review or external scanners (e.g. ScanCode) remain recommended before major releases.
