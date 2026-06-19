# Post

A simple GNOME mail client: **fresh GTK4/Libadwaita UI** on top of **Evolution Data Server** (Camel + evolution-ews). Email only — no calendar, no tasks.

**Post** — read and send mail on GNOME.

This is a Phase 1 spike: list accounts, browse folders, read messages.

## Prerequisites

System packages (Debian/Ubuntu names):

```bash
sudo apt install \
  python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 \
  gir1.2-webkit-6.0 gir1.2-camel-1.2 gir1.2-edataserver-1.2 \
  evolution-data-server evolution-ews
```

You also need **at least one mail account** already configured — via Evolution, or GNOME Online Accounts (Google, Microsoft 365, IMAP). Post reads the same account store; it does not have its own account wizard yet.

The `evolution-source-registry` D-Bus service must be running (usually started automatically when Evolution or GOA mail is set up).

## Quick start

```bash
# Create venv (needs --system-site-packages for PyGObject/GTK)
python3 -m venv .venv --system-site-packages
.venv/bin/pip install -e .

# Headless check (no GUI) — lists accounts and Inbox subjects
.venv/bin/post-probe

# Launch the GUI
./run.sh
```

## Project layout

```
src/post/
  app.py          Adw.Application entry
  window.py       3-pane main window (folders | list | reader)
  probe.py        CLI sanity check
  reader/
    html.py       HTML document builder for WebKit
  mail/
    eds.py        MailService + Camel session (EDS backend)
    helpers.py    Folder tree walking, message parsing
```

## What works (v0.1)

- List mail accounts from EDS
- Browse folder tree (IMAP, Microsoft 365/Graph, EWS — whatever Evolution supports)
- Message list + HTML reading pane (WebKitGTK; remote images off by default)
- Account switcher in header bar

## What comes next

- Compose / reply / per-account signatures
- Own account setup assistant (Libadwaita)
- Conversation threading
- Keyboard shortcuts

## Troubleshooting

**"Could not connect to evolution-source-registry"**

```bash
/usr/libexec/evolution-source-registry &
```

**No accounts listed**

Add a mail account in Evolution first (`File → New → Mail Account`), or via GNOME Settings → Online Accounts.

**Microsoft 365**

Use the **Microsoft 365** account type in Evolution (Graph API), not deprecated EWS-for-cloud. Post uses whatever backend EDS provides.

## License

Copyright (C) 2026 mbrennwa

Post is free software: you can redistribute it and/or modify it under the terms of
the GNU General Public License as published by the Free Software Foundation,
either version 3 of the License, or (at your option) any later version.

See [COPYING](COPYING) for the full licence text.

### Third-party

- **Evolution Data Server / Camel / evolution-ews** — runtime dependencies (LGPL-2.1+); linked dynamically, not bundled in this source tree.
- **GTK 4, Libadwaita, WebKitGTK 6, PyGObject** — LGPL-2.1+ (system libraries).
- Code in `src/post/mail/eds.py` and `src/post/mail/helpers.py` was derived from [EvolutionMCP](https://github.com/affix/EvolutionMCP) (MIT) — see [LICENSES/MIT-EvolutionMCP.txt](LICENSES/MIT-EvolutionMCP.txt).
