# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for compose spell checking (#134)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import gi

gi.require_version("WebKit", "6.0")

from gi.repository import WebKit

import post.preferences as preferences
from post.spell_check import (
    action_name_for_language,
    apply_spell_check,
    clear_installed_spell_languages_cache,
    default_spell_languages,
    ensure_spell_check_initialized,
    filter_installed_languages,
    language_code_from_action_name,
    list_installed_spell_languages,
    normalize_locale_code,
    set_spell_language_active,
    spell_language_label,
)


class NormalizeLocaleCodeTests(unittest.TestCase):
    def test_standard_codes(self) -> None:
        self.assertEqual(normalize_locale_code("en_US"), "en_US")
        self.assertEqual(normalize_locale_code("de-DE.UTF-8"), "de_DE")
        self.assertEqual(normalize_locale_code("en-us"), "en_US")

    def test_rejects_invalid(self) -> None:
        self.assertIsNone(normalize_locale_code(""))
        self.assertIsNone(normalize_locale_code("123"))


class SpellLanguageLabelTests(unittest.TestCase):
    def test_label(self) -> None:
        self.assertEqual(spell_language_label("en_US"), "English (United States)")
        self.assertEqual(spell_language_label("de_DE"), "German (Germany)")

    def test_prefer_regional_over_bare(self) -> None:
        from post.spell_check import _prefer_regional_locale_codes

        self.assertEqual(
            _prefer_regional_locale_codes(["en", "en_US", "de_DE"]),
            ["en_US", "de_DE"],
        )


class FilterInstalledLanguagesTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_installed_spell_languages_cache()

    def tearDown(self) -> None:
        clear_installed_spell_languages_cache()

    def test_keeps_installed_only(self) -> None:
        with mock.patch(
            "post.spell_check.installed_spell_language_codes",
            return_value={"en_US", "de_DE"},
        ):
            self.assertEqual(
                filter_installed_languages(["en_US", "fr_FR", "de_DE"]),
                ["en_US", "de_DE"],
            )


class DefaultSpellLanguagesTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_installed_spell_languages_cache()

    def tearDown(self) -> None:
        clear_installed_spell_languages_cache()

    def test_prefers_lang_env(self) -> None:
        with (
            mock.patch.dict(os.environ, {"LANG": "de_DE.UTF-8"}, clear=False),
            mock.patch(
                "post.spell_check.installed_spell_language_codes",
                return_value={"en_US", "de_DE"},
            ),
        ):
            self.assertEqual(default_spell_languages(), ["de_DE"])


class PreferencesRoundtripTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._pref_path = Path(self._tmpdir.name) / "preferences.json"
        self._patch = mock.patch.object(preferences, "_PREF_PATH", str(self._pref_path))
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmpdir.cleanup()

    def test_roundtrip(self) -> None:
        preferences.set_spell_check_languages(["en_US", "de_DE"])
        self.assertEqual(preferences.get_spell_check_languages(), ["en_US", "de_DE"])


class ApplySpellCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_installed_spell_languages_cache()
        self._context = mock.Mock()
        self._patch = mock.patch(
            "post.spell_check.WebKit.WebContext.get_default",
            return_value=self._context,
        )
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        clear_installed_spell_languages_cache()

    def test_enables_with_installed_languages(self) -> None:
        with mock.patch(
            "post.spell_check.filter_installed_languages",
            return_value=["en_US", "de_DE"],
        ):
            active = apply_spell_check(["en_US", "de_DE", "fr_FR"])
        self.assertEqual(active, ["en_US", "de_DE"])
        self._context.set_spell_checking_enabled.assert_called_once_with(True)
        self._context.set_spell_checking_languages.assert_called_once_with(
            ["en_US", "de_DE"]
        )

    def test_disables_when_empty(self) -> None:
        with mock.patch(
            "post.spell_check.filter_installed_languages",
            return_value=[],
        ):
            active = apply_spell_check([])
        self.assertEqual(active, [])
        self._context.set_spell_checking_enabled.assert_called_once_with(False)
        self._context.set_spell_checking_languages.assert_not_called()


class EnsureSpellCheckInitializedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._pref_path = Path(self._tmpdir.name) / "preferences.json"
        self._pref_patch = mock.patch.object(
            preferences, "_PREF_PATH", str(self._pref_path)
        )
        self._pref_patch.start()
        clear_installed_spell_languages_cache()
        self._context = mock.Mock()
        self._ctx_patch = mock.patch(
            "post.spell_check.WebKit.WebContext.get_default",
            return_value=self._context,
        )
        self._ctx_patch.start()

    def tearDown(self) -> None:
        self._ctx_patch.stop()
        self._pref_patch.stop()
        self._tmpdir.cleanup()
        clear_installed_spell_languages_cache()

    def test_persists_defaults_when_missing(self) -> None:
        with (
            mock.patch(
                "post.spell_check.default_spell_languages",
                return_value=["en_US"],
            ),
            mock.patch(
                "post.spell_check.filter_installed_languages",
                side_effect=lambda langs: langs,
            ),
        ):
            active = ensure_spell_check_initialized()
        self.assertEqual(active, ["en_US"])
        self.assertEqual(preferences.get_spell_check_languages(), ["en_US"])


class ToggleSpellLanguageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._pref_path = Path(self._tmpdir.name) / "preferences.json"
        self._pref_patch = mock.patch.object(
            preferences, "_PREF_PATH", str(self._pref_path)
        )
        self._pref_patch.start()
        preferences.set_spell_check_languages(["en_US"])
        self._context = mock.Mock()
        self._ctx_patch = mock.patch(
            "post.spell_check.WebKit.WebContext.get_default",
            return_value=self._context,
        )
        self._ctx_patch.start()

    def tearDown(self) -> None:
        self._ctx_patch.stop()
        self._pref_patch.stop()
        self._tmpdir.cleanup()

    def test_add_and_remove(self) -> None:
        with mock.patch(
            "post.spell_check.filter_installed_languages",
            side_effect=lambda langs: langs,
        ):
            active = set_spell_language_active("de_DE", True)
            self.assertEqual(active, ["en_US", "de_DE"])
            active = set_spell_language_active("en_US", False)
            self.assertEqual(active, ["de_DE"])


class ActionNameTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        name = action_name_for_language("en_US")
        self.assertEqual(language_code_from_action_name(name), "en_US")


class ListInstalledFromHunspellTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_installed_spell_languages_cache()
        self._tmpdir = tempfile.TemporaryDirectory()
        hunspell_dir = Path(self._tmpdir.name)
        (hunspell_dir / "en_US.dic").write_text("", encoding="utf-8")
        (hunspell_dir / "de_DE.dic").write_text("", encoding="utf-8")
        self._dirs_patch = mock.patch(
            "post.spell_check._HUNSPELL_DIRS",
            (hunspell_dir,),
        )
        self._dirs_patch.start()

    def tearDown(self) -> None:
        self._dirs_patch.stop()
        self._tmpdir.cleanup()
        clear_installed_spell_languages_cache()

    def test_discovers_dic_files(self) -> None:
        codes = [code for code, _ in list_installed_spell_languages(refresh=True)]
        self.assertEqual(codes, ["de_DE", "en_US"])


if __name__ == "__main__":
    unittest.main()
