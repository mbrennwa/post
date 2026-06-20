# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import tempfile
import unittest

from post.mail.accounts import (
    BUILTIN_LOCAL_UID,
    LocalMailConfig,
    _render_account_source,
    _render_identity_source,
    default_local_mail_config,
    default_spool_path,
    is_maildir_empty,
    is_spool_empty,
    validate_local_mail_config,
)


class MaildirEmptyTests(unittest.TestCase):
    def test_missing_directory_is_empty(self) -> None:
        self.assertTrue(is_maildir_empty("/nonexistent/maildir"))

    def test_empty_subdirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "cur"))
            os.makedirs(os.path.join(tmp, "new"))
            self.assertTrue(is_maildir_empty(tmp))

    def test_message_in_new(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            new_dir = os.path.join(tmp, "new")
            os.makedirs(new_dir)
            with open(os.path.join(new_dir, "msg"), "wb") as handle:
                handle.write(b"From: a\n")
            self.assertFalse(is_maildir_empty(tmp))


class SpoolEmptyTests(unittest.TestCase):
    def test_missing_file_is_empty(self) -> None:
        self.assertTrue(is_spool_empty("/nonexistent/mbox"))

    def test_zero_byte_file_is_empty(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            path = tmp.name
        try:
            self.assertTrue(is_spool_empty(path))
        finally:
            os.unlink(path)

    def test_non_empty_file(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"From: a\n")
            path = tmp.name
        try:
            self.assertFalse(is_spool_empty(path))
        finally:
            os.unlink(path)


class ValidateLocalMailConfigTests(unittest.TestCase):
    def test_disabled_config_is_valid(self) -> None:
        config = default_local_mail_config()
        self.assertIsNone(validate_local_mail_config(config))

    def test_enabled_requires_path(self) -> None:
        config = LocalMailConfig(
            enabled=True,
            mail_type="spool",
            path="",
            from_name="user",
            from_address="user@localhost",
        )
        self.assertIsNotNone(validate_local_mail_config(config))

    def test_enabled_requires_from_address(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            path = tmp.name
        try:
            config = LocalMailConfig(
                enabled=True,
                mail_type="spool",
                path=path,
                from_name="user",
                from_address="",
            )
            self.assertIsNotNone(validate_local_mail_config(config))
        finally:
            os.unlink(path)

    def test_enabled_spool_with_valid_path(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            path = tmp.name
        try:
            config = LocalMailConfig(
                enabled=True,
                mail_type="spool",
                path=path,
                from_name="user",
                from_address="user@localhost",
            )
            self.assertIsNone(validate_local_mail_config(config))
        finally:
            os.unlink(path)

    def test_enabled_maildir_requires_directory(self) -> None:
        config = LocalMailConfig(
            enabled=True,
            mail_type="maildir",
            path="/nonexistent/maildir",
            from_name="user",
            from_address="user@localhost",
        )
        error = validate_local_mail_config(config)
        self.assertIsNotNone(error)
        self.assertIn("Maildir", error or "")


class SourceTemplateTests(unittest.TestCase):
    def test_identity_source_contains_address(self) -> None:
        text = _render_identity_source("Alice", "alice@example.com")
        self.assertIn("Address=alice@example.com", text)
        self.assertIn("Name=Alice", text)

    def test_account_source_uses_post_uid(self) -> None:
        text = _render_account_source(
            enabled=True,
            mail_type="spool",
            path="/var/spool/mail/alice",
        )
        self.assertIn("BackendName=spool", text)
        self.assertIn("Path=/var/spool/mail/alice", text)
        self.assertIn("post-local-mail-identity", text)

    def test_maildir_backend_section(self) -> None:
        text = _render_account_source(
            enabled=False,
            mail_type="maildir",
            path="/home/alice/Maildir",
        )
        self.assertIn("[Maildir Backend]", text)
        self.assertIn("Enabled=false", text)


class DefaultConfigTests(unittest.TestCase):
    def test_default_spool_path_uses_user(self) -> None:
        path = default_spool_path()
        self.assertTrue(path.startswith("/var/spool/mail/"))

    def test_defaults_disabled(self) -> None:
        config = default_local_mail_config()
        self.assertFalse(config.enabled)
        self.assertEqual(config.mail_type, "spool")


class BuiltinLocalUidTests(unittest.TestCase):
    def test_builtin_uid_constant(self) -> None:
        self.assertEqual(BUILTIN_LOCAL_UID, "local")


class BuiltinLocalEmptyIntegrationTests(unittest.TestCase):
    def test_builtin_local_empty_reads_registry_when_available(self) -> None:
        try:
            import gi

            gi.require_version("EDataServer", "1.2")
            from gi.repository import EDataServer

            from post.mail.accounts import is_builtin_local_store_empty

            registry = EDataServer.SourceRegistry.new_sync(None)
            if registry is None:
                self.skipTest("EDS not available")
            # Should not raise; result depends on local maildir contents.
            result = is_builtin_local_store_empty(registry)
            self.assertIsInstance(result, bool)
        except (ImportError, ValueError) as exc:
            self.skipTest(str(exc))


if __name__ == "__main__":
    unittest.main()
