# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""GOA credential refresh must not wait forever (#156)."""

from __future__ import annotations

import unittest
from unittest import mock

from post.mail import auth


class EnsureGoaCredentialsTests(unittest.TestCase):
    def test_ensure_credentials_uses_finite_timeout(self) -> None:
        registry = mock.Mock()
        source = mock.Mock()
        source.get_display_name.return_value = "M365"
        source.get_uid.return_value = "acct-1"
        source.has_extension.return_value = True
        goa = mock.Mock()
        goa.get_account_id.return_value = "goa-1"
        source.get_extension.return_value = goa
        registry.list_sources.return_value = [source]

        bus = mock.Mock()
        with mock.patch("post.mail.auth.Gio.bus_get_sync", return_value=bus):
            auth.ensure_goa_credentials(registry, source, None)

        bus.call_sync.assert_called_once()
        timeout_ms = bus.call_sync.call_args.args[7]
        self.assertEqual(timeout_ms, auth._GOA_ENSURE_CREDENTIALS_TIMEOUT_MS)
        self.assertGreater(timeout_ms, 0)
        self.assertNotEqual(timeout_ms, -1)


if __name__ == "__main__":
    unittest.main()
