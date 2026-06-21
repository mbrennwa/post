# Post

**Post** is a mail app for GNOME — read and send email with a simple, native desktop experience. It is free and open source, built for Linux. Post picks up online accounts from GNOME Online Accounts and optionally also local system email.

## Install
[WIP -- follow Tunes Player README]

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
