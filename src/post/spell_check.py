# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""WebKit spell checking for the compose editor (#134)."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

import gi

gi.require_version("WebKit", "6.0")

from gi.repository import WebKit

from post.preferences import get_spell_check_languages, set_spell_check_languages

log = logging.getLogger(__name__)

_HUNSPELL_DIRS = (
    Path("/usr/share/hunspell"),
    Path("/usr/share/myspell/dicts"),
)

_LOCALE_CODE_RE = re.compile(r"^[a-z]{2,3}(?:_[A-Z]{2})?$")

# Common WebKit/enchant locale codes → readable menu labels.
_DISPLAY_NAMES: dict[str, str] = {
    "en": "English",
    "en_US": "English (United States)",
    "en_GB": "English (United Kingdom)",
    "en_AU": "English (Australia)",
    "en_CA": "English (Canada)",
    "de": "German",
    "de_DE": "German (Germany)",
    "de_AT": "German (Austria)",
    "de_CH": "German (Switzerland)",
    "fr": "French",
    "fr_FR": "French (France)",
    "fr_CA": "French (Canada)",
    "es": "Spanish",
    "es_ES": "Spanish (Spain)",
    "it": "Italian",
    "it_IT": "Italian (Italy)",
    "nl": "Dutch",
    "nl_NL": "Dutch (Netherlands)",
    "pt": "Portuguese",
    "pt_PT": "Portuguese (Portugal)",
    "pt_BR": "Portuguese (Brazil)",
    "sv": "Swedish",
    "sv_SE": "Swedish (Sweden)",
    "da": "Danish",
    "da_DK": "Danish (Denmark)",
    "nb": "Norwegian Bokmål",
    "nb_NO": "Norwegian Bokmål (Norway)",
    "nn": "Norwegian Nynorsk",
    "nn_NO": "Norwegian Nynorsk (Norway)",
    "pl": "Polish",
    "pl_PL": "Polish (Poland)",
    "ru": "Russian",
    "ru_RU": "Russian (Russia)",
}

_INSTALLED_CACHE: list[tuple[str, str]] | None = None


def normalize_locale_code(raw: str) -> str | None:
    """Normalize a locale tag to WebKit ``lang_COUNTRY`` form."""
    text = (raw or "").strip()
    if not text:
        return None
    text = text.split(".")[0].split("@")[0].replace("-", "_")
    if "_" in text:
        lang, region = text.split("_", 1)
        text = f"{lang.lower()}_{region.upper()}"
    else:
        text = text.lower()
    if not _LOCALE_CODE_RE.match(text):
        return None
    return text


def spell_language_label(code: str) -> str:
    """Return a human-readable label for a spell-check locale code."""
    if code in _DISPLAY_NAMES:
        return _DISPLAY_NAMES[code]
    if "_" in code:
        language, country = code.split("_", 1)
        return f"{language} ({country})"
    return code


def _discover_locale_codes() -> list[str]:
    """Collect installed spell-check locale codes from hunspell files and enchant."""
    codes: list[str] = []
    seen: set[str] = set()
    for source in (_codes_from_hunspell_dirs(), _codes_from_enchant()):
        for code in source:
            if code not in seen:
                seen.add(code)
                codes.append(code)
    return _prefer_regional_locale_codes(codes)


def _prefer_regional_locale_codes(codes: list[str]) -> list[str]:
    """Drop bare language codes when a regional variant is available (``en`` vs ``en_US``)."""
    code_set = set(codes)
    result: list[str] = []
    for code in codes:
        if "_" not in code and any(
            other.startswith(f"{code}_") for other in code_set if other != code
        ):
            continue
        result.append(code)
    return result


def _codes_from_hunspell_dirs() -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()
    for directory in _HUNSPELL_DIRS:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.dic")):
            code = normalize_locale_code(path.stem)
            if code and code not in seen:
                seen.add(code)
                codes.append(code)
    return codes


def _codes_from_enchant() -> list[str]:
    try:
        proc = subprocess.run(
            ["enchant-2", "-l"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    codes: list[str] = []
    seen: set[str] = set()
    for line in proc.stdout.splitlines():
        for part in line.split():
            code = normalize_locale_code(part)
            if code and code not in seen:
                seen.add(code)
                codes.append(code)
    return codes


def list_installed_spell_languages(*, refresh: bool = False) -> list[tuple[str, str]]:
    """Return installed spell-check locales as ``(code, label)`` pairs."""
    global _INSTALLED_CACHE
    if _INSTALLED_CACHE is not None and not refresh:
        return list(_INSTALLED_CACHE)

    codes = _discover_locale_codes()
    if not codes:
        codes = ["en_US"]

    _INSTALLED_CACHE = [
        (code, spell_language_label(code)) for code in sorted(set(codes))
    ]
    return list(_INSTALLED_CACHE)


def installed_spell_language_codes(*, refresh: bool = False) -> set[str]:
    return {code for code, _ in list_installed_spell_languages(refresh=refresh)}


def default_spell_languages() -> list[str]:
    """Pick a sensible default active language from the environment."""
    installed = installed_spell_language_codes()
    for env_name in ("LANG", "LC_MESSAGES", "LC_ALL"):
        normalized = normalize_locale_code(os.environ.get(env_name, ""))
        if normalized and normalized in installed:
            return [normalized]
        if normalized and "_" in normalized:
            language = normalized.split("_", 1)[0]
            for code in sorted(installed):
                if code == language or code.startswith(f"{language}_"):
                    return [code]
    if "en_US" in installed:
        return ["en_US"]
    if installed:
        return [sorted(installed)[0]]
    return ["en_US"]


def filter_installed_languages(languages: list[str]) -> list[str]:
    """Keep only installed locale codes, preserving order."""
    installed = installed_spell_language_codes()
    result: list[str] = []
    for language in languages:
        code = normalize_locale_code(language)
        if code and code in installed and code not in result:
            result.append(code)
    return result


def apply_spell_check(languages: list[str] | None = None) -> list[str]:
    """Apply spell-check languages to the process WebKit context."""
    if languages is None:
        languages = get_spell_check_languages()
    if not languages:
        languages = default_spell_languages()
    active = filter_installed_languages(languages)
    context = WebKit.WebContext.get_default()
    context.set_spell_checking_enabled(bool(active))
    if active:
        context.set_spell_checking_languages(active)
    return active


def ensure_spell_check_initialized() -> list[str]:
    """Load prefs defaults and enable spell checking once at startup."""
    stored = get_spell_check_languages()
    if not stored:
        defaults = default_spell_languages()
        set_spell_check_languages(defaults)
        stored = defaults
    return apply_spell_check(stored)


def get_active_spell_languages() -> list[str]:
    """Return the active spell-check languages from preferences."""
    stored = get_spell_check_languages()
    if stored:
        active = filter_installed_languages(stored)
        if active:
            return active
    return filter_installed_languages(default_spell_languages())


def set_spell_language_active(code: str, active: bool) -> list[str]:
    """Enable or disable one spell-check language in preferences."""
    normalized = normalize_locale_code(code)
    if not normalized:
        return get_active_spell_languages()

    current = get_active_spell_languages()
    if active:
        if normalized not in current:
            current.append(normalized)
    else:
        current = [item for item in current if item != normalized]

    set_spell_check_languages(current)
    return apply_spell_check(current)


def action_name_for_language(code: str) -> str:
    """Return a GAction-safe name for *code*."""
    return "lang-" + code.replace("_", "-")


def language_code_from_action_name(name: str) -> str | None:
    if not name.startswith("lang-"):
        return None
    return normalize_locale_code(name.removeprefix("lang-").replace("-", "_"))


def clear_installed_spell_languages_cache() -> None:
    global _INSTALLED_CACHE
    _INSTALLED_CACHE = None
