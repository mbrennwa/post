# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Build HTML documents for the WebKit reading pane."""

from __future__ import annotations

import base64
import html
import re

from post.preferences import (
    MESSAGE_APPEARANCE_ACCEPT_SENDER,
    MESSAGE_APPEARANCE_ADAPT_TEXT,
    MessageAppearance,
)

# Block http(s) images and trackers when remote content is disabled.
_EXTERNAL_IMG = re.compile(
    r'(<img\b[^>]*\ssrc=)(["\'])https?://[^"\']*\2',
    re.IGNORECASE,
)
_EXTERNAL_BG = re.compile(
    r"url\(\s*['\"]?https?://[^)'\"]+['\"]?\s*\)",
    re.IGNORECASE,
)
_REMOTE_IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_REMOTE_IMG_SRC = re.compile(
    r"""\ssrc\s*=\s*(?:("https?://[^"]*"|'https?://[^']*'|https?://[^\s>"']+))""",
    re.IGNORECASE,
)


def _remote_img_tag_is_notifiable(tag: str) -> bool:
    """Ignore tracking pixels and other intentionally hidden images."""
    lower = tag.lower()
    if "display:none" in lower or "display: none" in lower:
        return False
    if re.search(r'\bhidden(?:=(?:["\']?true["\']?)?|\b)', lower):
        return False
    width = re.search(r"""\bwidth=(["\']?)(\d+)\1""", tag, re.IGNORECASE)
    height = re.search(r"""\bheight=(["\']?)(\d+)\1""", tag, re.IGNORECASE)
    if width and height and int(width.group(2)) <= 1 and int(height.group(2)) <= 1:
        return False
    return True


_CID_IMG_SRC_QUOTED = re.compile(
    r'(\ssrc\s*=\s*)(["\'])cid:(.*?)\2',
    re.IGNORECASE,
)
_CID_IMG_SRC_UNQUOTED = re.compile(
    r'(\ssrc\s*=\s*)cid:(<[^>]+>|[^"\'>\s]+)',
    re.IGNORECASE,
)
_CID_CSS_URL = re.compile(
    r"url\(\s*['\"]?cid:(<[^>]+>|[^)'\"\s]+)['\"]?\s*\)",
    re.IGNORECASE,
)


def _normalize_cid_reference(cid_ref: str) -> str:
    ref = cid_ref.strip()
    if ref.startswith("<") and ref.endswith(">"):
        ref = ref[1:-1]
    return ref


def _lookup_inline_image(
    inline_images: dict[str, tuple[str, bytes]], cid_ref: str
) -> tuple[str, bytes] | None:
    ref = _normalize_cid_reference(cid_ref)
    if ref in inline_images:
        return inline_images[ref]
    lower_ref = ref.lower()
    for key, value in inline_images.items():
        if key.lower() == lower_ref:
            return value
    return None


def _inline_image_data_url(mime_type: str, data: bytes) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def resolve_cid_images(
    body_html: str, inline_images: dict[str, tuple[str, bytes]]
) -> str:
    """Replace cid: image references with embedded data URLs for WebKit."""
    if not inline_images:
        return body_html

    def replace_img_src_quoted(match: re.Match[str]) -> str:
        prefix, quote, cid_ref = match.groups()
        found = _lookup_inline_image(inline_images, cid_ref)
        if found is None:
            return match.group(0)
        data_url = _inline_image_data_url(found[0], found[1])
        return f"{prefix}{quote}{data_url}{quote}"

    def replace_img_src_unquoted(match: re.Match[str]) -> str:
        prefix, cid_ref = match.groups()
        found = _lookup_inline_image(inline_images, cid_ref)
        if found is None:
            return match.group(0)
        data_url = _inline_image_data_url(found[0], found[1])
        return f'{prefix}"{data_url}"'

    def replace_css_url(match: re.Match[str]) -> str:
        cid_ref = match.group(1)
        found = _lookup_inline_image(inline_images, cid_ref)
        if found is None:
            return match.group(0)
        data_url = _inline_image_data_url(found[0], found[1])
        return f"url({data_url})"

    body_html = _CID_IMG_SRC_QUOTED.sub(replace_img_src_quoted, body_html)
    body_html = _CID_IMG_SRC_UNQUOTED.sub(replace_img_src_unquoted, body_html)
    return _CID_CSS_URL.sub(replace_css_url, body_html)


def html_has_blocked_remote_content(body_html: str) -> bool:
    """Return True if HTML contains remote <img> tags the user may want to load."""
    for tag in _REMOTE_IMG_TAG.findall(body_html):
        if not _REMOTE_IMG_SRC.search(tag):
            continue
        if _remote_img_tag_is_notifiable(tag):
            return True
    return False


_READER_CSS_LIGHT = """
body {
  font-family: system-ui, sans-serif;
  font-size: 14px;
  line-height: 1.5;
  margin: 1rem;
  overflow-wrap: anywhere;
  color: #1e1e1e;
  background: #ffffff;
}
pre.plain-body {
  white-space: pre-wrap;
  font-family: inherit;
}
img[src=""] {
  display: none;
}
.remote-blocked-notice {
  color: #888;
  font-size: 12px;
  margin-bottom: 1rem;
  padding: 0.5rem 0.75rem;
  border-left: 3px solid #888;
}
a { color: #3584e4; }
blockquote {
  margin: 0.5rem 0;
  padding-left: 1rem;
  border-left: 3px solid #ccc;
}
"""

_READER_CSS_DARK = """
body {
  font-family: system-ui, sans-serif;
  font-size: 14px;
  line-height: 1.5;
  margin: 1rem;
  overflow-wrap: anywhere;
  color: #eeeeee;
  background: #1e1e1e;
}
pre.plain-body {
  white-space: pre-wrap;
  font-family: inherit;
}
img[src=""] {
  display: none;
}
.remote-blocked-notice {
  color: #aaaaaa;
  font-size: 12px;
  margin-bottom: 1rem;
  padding: 0.5rem 0.75rem;
  border-left: 3px solid #666666;
}
a { color: #62a0ea; }
blockquote {
  margin: 0.5rem 0;
  padding-left: 1rem;
  border-left: 3px solid #555555;
}
"""

_ADAPT_TEXT_CSS = """
.message-body :where(
  p, div, span, li, td, th, font, blockquote, pre, a,
  h1, h2, h3, h4, h5, h6
) {
  color: inherit !important;
}
"""

_STYLE_BLOCK = re.compile(r"<style\b[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)
_INLINE_STYLE = re.compile(
    r"""style\s*=\s*(["'])(.*?)\1""",
    re.IGNORECASE | re.DOTALL,
)
_DECL_TEXT_COLOR = re.compile(r"(?:^|[;{\s])color\s*:", re.IGNORECASE | re.MULTILINE)
_DECL_BG_COLOR_VALUE = re.compile(
    r"\bbackground-color\s*:\s*([^;}{]+)",
    re.IGNORECASE,
)
_DECL_BACKGROUND_VALUE = re.compile(
    r"(?:^|[;{\s])background\s*:\s*([^;}{]+)",
    re.IGNORECASE | re.MULTILINE,
)
_BGCOLOR_VALUE = re.compile(
    r"""\bbgcolor\s*=\s*(["']?)([^"'\s>]+)\1""",
    re.IGNORECASE,
)
_TRANSPARENT_COLOR_VALUES = frozenset(
    {
        "transparent",
        "none",
        "inherit",
        "initial",
        "unset",
        "revert",
        "revert-layer",
    }
)
_RGBA_COLOR = re.compile(
    r"rgba?\(\s*[\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+\s*(?:,\s*([\d.]+)\s*)?\)",
    re.IGNORECASE,
)
_HSLA_COLOR = re.compile(
    r"hsla?\(\s*[\d.]+\s*,\s*[\d.]+%\s*,\s*[\d.]+%\s*(?:,\s*([\d.]+)\s*)?\)",
    re.IGNORECASE,
)
_TEXT_ATTR = re.compile(r"""\btext\s*=\s*["']?[^"'\s>]+""", re.IGNORECASE)
_FONT_COLOR_ATTR = re.compile(r"<font\b[^>]*\bcolor\s*=", re.IGNORECASE)
_QUOTE_HISTORY_MARKERS = (
    'id="mail-editor-reference-message-container"',
    "id='mail-editor-reference-message-container'",
    'id="geary-quote"',
    "id='geary-quote'",
    'class="gmail_quote"',
    "class='gmail_quote'",
    'id="appendonsend"',
    "<blockquote",
)


def _html_for_adaptation_detection(body_html: str) -> str:
    """Return the portion of HTML whose colors drive the adapt decision."""
    lower = body_html.lower()
    cut = len(body_html)
    for marker in _QUOTE_HISTORY_MARKERS:
        idx = lower.find(marker.lower())
        if idx != -1:
            cut = min(cut, idx)
    return body_html[:cut]


def _iter_style_sources(body_html: str):
    for match in _STYLE_BLOCK.finditer(body_html):
        yield match.group(1)
    for match in _INLINE_STYLE.finditer(body_html):
        yield match.group(2)


def _normalize_css_declaration_value(value: str) -> str:
    candidate = value.strip()
    for delimiter in ('"', "'", ">"):
        idx = candidate.find(delimiter)
        if idx != -1:
            candidate = candidate[:idx]
    return candidate.strip()


def _css_color_value_is_meaningful(value: str) -> bool:
    """Return True when a CSS color value paints a visible background."""
    candidate = _normalize_css_declaration_value(value).lower()
    if not candidate:
        return False
    first_token = candidate.split()[0].rstrip(",")
    if first_token in _TRANSPARENT_COLOR_VALUES:
        return False
    if first_token.startswith("url("):
        return False
    rgba_match = _RGBA_COLOR.search(candidate)
    if rgba_match is not None:
        alpha = rgba_match.group(1)
        if alpha is not None:
            try:
                return float(alpha) > 0
            except ValueError:
                return True
        return True
    hsla_match = _HSLA_COLOR.search(candidate)
    if hsla_match is not None:
        alpha = hsla_match.group(1)
        if alpha is not None:
            try:
                return float(alpha) > 0
            except ValueError:
                return True
        return True
    return True


def _style_source_has_meaningful_background(source: str) -> bool:
    for match in _DECL_BG_COLOR_VALUE.finditer(source):
        if _css_color_value_is_meaningful(match.group(1)):
            return True
    for match in _DECL_BACKGROUND_VALUE.finditer(source):
        if _css_color_value_is_meaningful(match.group(1)):
            return True
    return False


def html_has_explicit_text_color(body_html: str) -> bool:
    """Return True when HTML sets an explicit foreground/text color."""
    if _FONT_COLOR_ATTR.search(body_html) or _TEXT_ATTR.search(body_html):
        return True
    return any(_DECL_TEXT_COLOR.search(source) for source in _iter_style_sources(body_html))


def html_has_explicit_background_color(body_html: str) -> bool:
    """Return True when HTML sets a visible/opaque background color."""
    for match in _BGCOLOR_VALUE.finditer(body_html):
        if _css_color_value_is_meaningful(match.group(2)):
            return True
    return any(
        _style_source_has_meaningful_background(source)
        for source in _iter_style_sources(body_html)
    )


def html_sender_defines_complete_colors(body_html: str) -> bool:
    """Return True when the sender set both text and background colors."""
    return (
        html_has_explicit_text_color(body_html)
        and html_has_explicit_background_color(body_html)
    )


def html_should_apply_adaptation(body_html: str) -> bool:
    """Return True when adapt modes should adjust reader colors for this HTML."""
    content = _html_for_adaptation_detection(body_html)
    return (
        html_has_explicit_text_color(content)
        and not html_has_explicit_background_color(content)
    )


def _effective_message_appearance(
    body_html: str | None,
    appearance: MessageAppearance,
) -> MessageAppearance:
    if (
        appearance != MESSAGE_APPEARANCE_ACCEPT_SENDER
        and body_html is not None
        and not html_should_apply_adaptation(body_html)
    ):
        return MESSAGE_APPEARANCE_ACCEPT_SENDER
    return appearance


def _effective_reader_dark(app_dark: bool, appearance: MessageAppearance) -> bool:
    if appearance == "adapt_background":
        return not app_dark
    return app_dark


def build_reader_document(
    *,
    body_html: str | None,
    body_plain: str | None,
    allow_remote: bool,
    dark: bool = False,
    message_appearance: MessageAppearance = MESSAGE_APPEARANCE_ADAPT_TEXT,
    inline_images: dict[str, tuple[str, bytes]] | None = None,
) -> str:
    """Wrap message content in a safe HTML shell for WebKit."""
    blocked_notice = ""
    html_body = body_html is not None
    effective_appearance = _effective_message_appearance(body_html, message_appearance)
    if body_html:
        content = body_html
        if inline_images:
            content = resolve_cid_images(content, inline_images)
        if not allow_remote:
            has_remote = html_has_blocked_remote_content(content)
            content = _EXTERNAL_IMG.sub(r'\1""', content)
            content = _EXTERNAL_BG.sub("url(none)", content)
            if has_remote:
                blocked_notice = (
                    '<p class="remote-blocked-notice">'
                    "Remote images are hidden. Enable “Load remote content” in Settings to show them."
                    "</p>"
                )
        if effective_appearance == "adapt_text":
            content = f'<div class="message-body">{content}</div>'
    elif body_plain:
        content = f'<pre class="plain-body">{html.escape(body_plain)}</pre>'
    else:
        content = "<p><em>(No message body)</em></p>"

    csp = (
        "default-src 'none'; "
        "img-src cid: data: blob: https: http:; "
        "style-src 'unsafe-inline'; "
        "font-src data:;"
    )
    if not allow_remote:
        csp = (
            "default-src 'none'; "
            "img-src cid: data: blob:; "
            "style-src 'unsafe-inline'; "
            "font-src data:;"
        )

    reader_dark = _effective_reader_dark(dark, effective_appearance)
    color_scheme = "dark" if reader_dark else "light"
    reader_css = _READER_CSS_DARK if reader_dark else _READER_CSS_LIGHT
    if effective_appearance == "adapt_text" and html_body:
        reader_css = f"{reader_css}\n{_ADAPT_TEXT_CSS}"

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="color-scheme" content="{color_scheme}">
<meta http-equiv="Content-Security-Policy" content="{csp}">
<style>{reader_css}</style>
</head>
<body>
{blocked_notice}
{content}
</body>
</html>"""
