# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for related credential source lookup (#168)."""

from __future__ import annotations

import unittest
from unittest import mock

from post.mail import auth


def _source(
    *,
    uid: str,
    display_name: str,
    parent: str = "",
    extensions: set[str] | None = None,
    goa_account_id: str | None = None,
):
    source = mock.Mock()
    source.get_uid.return_value = uid
    source.get_display_name.return_value = display_name
    source.get_parent.return_value = parent

    ext = extensions or set()

    def has_extension(name: str) -> bool:
        return name in ext

    source.has_extension.side_effect = has_extension

    def get_extension(name: str):
        if name == "GNOME Online Accounts":
            goa = mock.Mock()
            goa.get_account_id.return_value = goa_account_id
            return goa
        return mock.Mock()

    source.get_extension.side_effect = get_extension
    return source


class RelatedCredentialSourcesTests(unittest.TestCase):
    def test_transport_includes_mail_account_and_goa_siblings(self) -> None:
        collection = _source(
            uid="collection-1",
            display_name="user@example.com",
            extensions={"GNOME Online Accounts"},
            goa_account_id="goa-1",
        )
        store = _source(
            uid="store-1",
            display_name="user@example.com",
            parent="collection-1",
            extensions={"Mail Account"},
        )
        transport = _source(
            uid="smtp-1",
            display_name="user@example.com",
            parent="collection-1",
            extensions={"Mail Transport"},
        )
        other = _source(
            uid="other",
            display_name="other@example.com",
            extensions={"Mail Account"},
        )
        registry = mock.Mock()
        registry.ref_source.side_effect = lambda uid: {
            "collection-1": collection,
            "store-1": store,
            "smtp-1": transport,
        }.get(uid)
        registry.list_sources.return_value = [collection, store, transport, other]

        related = auth._related_credential_sources(registry, transport)
        uids = [source.get_uid() for source in related]
        self.assertEqual(uids[0], "smtp-1")
        self.assertIn("collection-1", uids)
        self.assertIn("store-1", uids)
        self.assertNotIn("other", uids)

    def test_lookup_password_uses_store_when_transport_empty(self) -> None:
        store = _source(
            uid="store-1",
            display_name="user@example.com",
            parent="collection-1",
            extensions={"Mail Account"},
        )
        transport = _source(
            uid="smtp-1",
            display_name="user@example.com",
            parent="collection-1",
            extensions={"Mail Transport"},
        )
        registry = mock.Mock()
        registry.ref_source.side_effect = lambda uid: {
            "collection-1": None,
            "store-1": store,
            "smtp-1": transport,
        }.get(uid)
        registry.list_sources.return_value = [store, transport]

        provider = mock.Mock()

        def lookup_sync(candidate, _cancellable):
            if candidate.get_uid() == "store-1":
                creds = mock.Mock()
                creds.get.return_value = "secret"
                return True, creds
            return False, None

        provider.lookup_sync.side_effect = lookup_sync
        with mock.patch(
            "post.mail.auth.EDataServer.SourceCredentialsProvider.new",
            return_value=provider,
        ):
            password = auth.lookup_stored_password(registry, transport, None)

        self.assertEqual(password, "secret")

    def test_password_prompt_reason_for_source(self) -> None:
        transport = _source(
            uid="smtp-1",
            display_name="user@example.com",
            extensions={"Mail Transport"},
        )
        store = _source(
            uid="store-1",
            display_name="user@example.com",
            extensions={"Mail Account"},
        )
        self.assertEqual(auth.password_prompt_reason_for_source(transport), "send_mail")
        self.assertEqual(auth.password_prompt_reason_for_source(store), "check_mail")

    def test_authenticate_skips_prompt_when_cancelled(self) -> None:
        import gi

        gi.require_version("Gio", "2.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import Gio, GLib

        service = mock.Mock()
        service.get_uid.return_value = "store-1"
        service.get_password.return_value = None
        source = _source(
            uid="store-1",
            display_name="info@example.com",
            extensions={"Mail Account"},
        )
        registry = mock.Mock()
        prompt = mock.Mock(return_value="should-not-be-called")
        cancellable = Gio.Cancellable()
        cancellable.cancel()

        with mock.patch.object(auth, "ensure_goa_credentials"):
            with self.assertRaises(GLib.Error) as ctx:
                auth.authenticate_service_sync(
                    service,
                    source,
                    registry,
                    None,
                    cancellable,
                    prompt,
                )
        self.assertTrue(
            ctx.exception.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED)
        )
        prompt.assert_not_called()

    def test_authenticate_passes_service_uid_to_prompt(self) -> None:
        service = mock.Mock()
        service.get_uid.return_value = "store-1"
        service.get_password.return_value = None
        source = _source(
            uid="store-1",
            display_name="info@example.com",
            extensions={"Mail Account"},
        )
        registry = mock.Mock()
        seen: list[tuple] = []

        def prompt(label, mechanism, reason, service_uid):
            seen.append((label, mechanism, reason, service_uid))
            return None

        with mock.patch.object(auth, "ensure_goa_credentials"):
            with mock.patch.object(auth, "lookup_stored_password", return_value=None):
                ok = auth.authenticate_service_sync(
                    service,
                    source,
                    registry,
                    None,
                    None,
                    prompt,
                )
        self.assertFalse(ok)
        self.assertEqual(
            seen,
            [("info@example.com", None, "check_mail", "store-1")],
        )

    def test_authentication_failed_error_is_cant_authenticate(self) -> None:
        import gi

        gi.require_version("Camel", "1.2")
        from gi.repository import Camel

        exc = auth.authentication_failed_error("Authentication failed")
        self.assertTrue(
            exc.matches(
                Camel.service_error_quark(),
                int(Camel.ServiceError.CANT_AUTHENTICATE),
            )
        )


if __name__ == "__main__":
    unittest.main()
