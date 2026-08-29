# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Build HTML documents for the WebKit reading pane."""

from __future__ import annotations

import base64
import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser

from post.mail.quote_history import split_html_at_quote_history
from post.preferences import (
    MESSAGE_APPEARANCE_ACCEPT_SENDER,
    MESSAGE_APPEARANCE_ADAPT_BACKGROUND,
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
html {
  background: #ffffff;
}
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
span.post-bracketed {
  display: inline;
}
"""

_READER_CSS_DARK = """
html {
  background: #1e1e1e;
}
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
span.post-bracketed {
  display: inline;
}
"""

_ADAPT_TEXT_CSS = """
.message-body {
  min-height: 100%;
  background: inherit;
}
.message-body .post-adapt-text {
  color: inherit !important;
}
.message-body .post-on-shell:not(.post-keep-color):not(.post-adapt-text):not(.post-forced-contrast) {
  color: inherit !important;
}
.message-body .post-painted {
  color-scheme: inherit;
}
.message-body .post-image-canvas {
  color-scheme: light;
}
"""

_STYLE_BLOCK = re.compile(r"<style\b[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)
_INLINE_STYLE = re.compile(
    r"""style\s*=\s*(["'])(.*?)\1""",
    re.IGNORECASE | re.DOTALL,
)
_DECL_TEXT_COLOR = re.compile(r"(?:^|[;{\s])color\s*:", re.IGNORECASE | re.MULTILINE)
_DECL_TEXT_COLOR_VALUE = re.compile(
    r"(?:^|[;{\s])color\s*:\s*([^;}{]+)",
    re.IGNORECASE | re.MULTILINE,
)
_DECL_BG_COLOR_VALUE = re.compile(
    r"\bbackground-color\s*:\s*([^;}{]+)",
    re.IGNORECASE,
)
_DECL_BACKGROUND_VALUE = re.compile(
    r"(?:^|[;{\s])background\s*:\s*([^;}{]+)",
    re.IGNORECASE | re.MULTILINE,
)
_DECL_BG_IMAGE = re.compile(r"\bbackground-image\s*:", re.IGNORECASE)
_BGCOLOR_VALUE = re.compile(
    r"""\bbgcolor\s*=\s*(["']?)([^"'\s>]+)\1""",
    re.IGNORECASE,
)
_HTML_BACKGROUND_ATTR = re.compile(
    r"""<[a-z][^>]*\sbackground\s*=\s*(["']?)([^"'\s>]+)\1""",
    re.IGNORECASE,
)
_CSS_URL_TOKEN = re.compile(r"url\s*\(", re.IGNORECASE)
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
# Simple `.class`, `tag.class`, or `tag` selectors (comma lists OK).
# Complex selectors still contribute class tokens via harvest (see #320).
_STYLE_RULE_BLOCK = re.compile(r"([^{}@]+)\{([^{}]*)\}", re.MULTILINE)
_SIMPLE_CSS_SELECTOR = re.compile(
    r"^\s*(?:"
    r"([A-Za-z][\w]*)?\.([A-Za-z_][\w-]*)"
    r"|#([A-Za-z_][\w-]*)"
    r"|([A-Za-z][\w]*)"
    r")\s*$"
)
_CSS_CLASS_TOKEN = re.compile(r"\.([A-Za-z_][\w-]*)")
_CSS_ID_TOKEN = re.compile(r"#([A-Za-z_][\w-]*)")
_CSS_CLASS_ATTR = re.compile(
    r"""\[\s*class\s*(?:\*=|\^=|\$=|~=|\|=|=)\s*(["'])([^"']+)\1\s*\]""",
    re.IGNORECASE,
)
_NAMED_CSS_COLORS: dict[str, tuple[int, int, int]] = {
    "aliceblue": (240, 248, 255),
    "antiquewhite": (250, 235, 215),
    "aqua": (0, 255, 255),
    "aquamarine": (127, 255, 212),
    "azure": (240, 255, 255),
    "beige": (245, 245, 220),
    "bisque": (255, 228, 196),
    "black": (0, 0, 0),
    "blanchedalmond": (255, 235, 205),
    "blue": (0, 0, 255),
    "brown": (165, 42, 42),
    "burlywood": (222, 184, 135),
    "cadetblue": (95, 158, 160),
    "chartreuse": (127, 255, 0),
    "chocolate": (210, 105, 30),
    "coral": (255, 127, 80),
    "cornsilk": (255, 248, 220),
    "crimson": (220, 20, 60),
    "cyan": (0, 255, 255),
    "darkblue": (0, 0, 139),
    "darkcyan": (0, 139, 139),
    "darkgoldenrod": (184, 134, 11),
    "darkgray": (169, 169, 169),
    "darkgreen": (0, 100, 0),
    "darkgrey": (169, 169, 169),
    "darkkhaki": (189, 183, 107),
    "darkmagenta": (139, 0, 139),
    "darkolivegreen": (85, 107, 47),
    "darkorange": (255, 140, 0),
    "darkorchid": (153, 50, 204),
    "darkred": (139, 0, 0),
    "darksalmon": (233, 150, 122),
    "darkseagreen": (143, 188, 143),
    "darkslateblue": (72, 61, 139),
    "darkslategray": (47, 79, 79),
    "darkslategrey": (47, 79, 79),
    "darkturquoise": (0, 206, 209),
    "darkviolet": (148, 0, 211),
    "deeppink": (255, 20, 147),
    "deepskyblue": (0, 191, 255),
    "dimgray": (105, 105, 105),
    "dimgrey": (105, 105, 105),
    "dodgerblue": (30, 144, 255),
    "firebrick": (178, 34, 34),
    "floralwhite": (255, 250, 240),
    "forestgreen": (34, 139, 34),
    "fuchsia": (255, 0, 255),
    "gainsboro": (220, 220, 220),
    "ghostwhite": (248, 248, 255),
    "gold": (255, 215, 0),
    "goldenrod": (218, 165, 32),
    "gray": (128, 128, 128),
    "green": (0, 128, 0),
    "greenyellow": (173, 255, 47),
    "grey": (128, 128, 128),
    "honeydew": (240, 255, 240),
    "hotpink": (255, 105, 180),
    "indianred": (205, 92, 92),
    "indigo": (75, 0, 130),
    "ivory": (255, 255, 240),
    "khaki": (240, 230, 140),
    "lavender": (230, 230, 250),
    "lavenderblush": (255, 240, 245),
    "lawngreen": (124, 252, 0),
    "lemonchiffon": (255, 250, 205),
    "lightblue": (173, 216, 230),
    "lightcoral": (240, 128, 128),
    "lightcyan": (224, 255, 255),
    "lightgoldenrodyellow": (250, 250, 210),
    "lightgray": (211, 211, 211),
    "lightgreen": (144, 238, 144),
    "lightgrey": (211, 211, 211),
    "lightpink": (255, 182, 193),
    "lightsalmon": (255, 160, 122),
    "lightseagreen": (32, 178, 170),
    "lightskyblue": (135, 206, 250),
    "lightslategray": (119, 136, 153),
    "lightslategrey": (119, 136, 153),
    "lightsteelblue": (176, 196, 222),
    "lightyellow": (255, 255, 224),
    "lime": (0, 255, 0),
    "limegreen": (50, 205, 50),
    "linen": (250, 240, 230),
    "magenta": (255, 0, 255),
    "maroon": (128, 0, 0),
    "mediumaquamarine": (102, 205, 170),
    "mediumblue": (0, 0, 205),
    "mediumorchid": (186, 85, 211),
    "mediumpurple": (147, 112, 219),
    "mediumseagreen": (60, 179, 113),
    "mediumslateblue": (123, 104, 238),
    "mediumspringgreen": (0, 250, 154),
    "mediumturquoise": (72, 209, 204),
    "mediumvioletred": (199, 21, 133),
    "midnightblue": (25, 25, 112),
    "mintcream": (245, 255, 250),
    "mistyrose": (255, 228, 225),
    "moccasin": (255, 228, 181),
    "navajowhite": (255, 222, 173),
    "navy": (0, 0, 128),
    "oldlace": (253, 245, 230),
    "olive": (128, 128, 0),
    "olivedrab": (107, 142, 35),
    "orange": (255, 165, 0),
    "orangered": (255, 69, 0),
    "orchid": (218, 112, 214),
    "palegoldenrod": (238, 232, 170),
    "palegreen": (152, 251, 152),
    "paleturquoise": (175, 238, 238),
    "palevioletred": (219, 112, 147),
    "papayawhip": (255, 239, 213),
    "peachpuff": (255, 218, 185),
    "peru": (205, 133, 63),
    "pink": (255, 192, 203),
    "plum": (221, 160, 221),
    "powderblue": (176, 224, 230),
    "purple": (128, 0, 128),
    "rebeccapurple": (102, 51, 153),
    "red": (255, 0, 0),
    "rosybrown": (188, 143, 143),
    "royalblue": (65, 105, 225),
    "saddlebrown": (139, 69, 19),
    "salmon": (250, 128, 114),
    "sandybrown": (244, 164, 96),
    "seagreen": (46, 139, 87),
    "seashell": (255, 245, 238),
    "sienna": (160, 82, 45),
    "silver": (192, 192, 192),
    "skyblue": (135, 206, 235),
    "slateblue": (106, 90, 205),
    "slategray": (112, 128, 144),
    "slategrey": (112, 128, 144),
    "snow": (255, 250, 250),
    "springgreen": (0, 255, 127),
    "steelblue": (70, 130, 180),
    "tan": (210, 180, 140),
    "teal": (0, 128, 128),
    "thistle": (216, 191, 216),
    "tomato": (255, 99, 71),
    "turquoise": (64, 224, 208),
    "violet": (238, 130, 238),
    "wheat": (245, 222, 179),
    "white": (255, 255, 255),
    "whitesmoke": (245, 245, 245),
    "yellow": (255, 255, 0),
    "yellowgreen": (154, 205, 50),
}


def _html_for_adaptation_detection(body_html: str) -> str:
    """Return the portion of HTML whose colors drive the adapt decision."""
    prefix, _quoted = split_html_at_quote_history(body_html)
    return prefix


def _iter_style_sources(body_html: str):
    for match in _STYLE_BLOCK.finditer(body_html):
        yield _unwrap_at_rules(match.group(1))
    for match in _INLINE_STYLE.finditer(body_html):
        yield match.group(2)


def _unwrap_at_rules(source: str) -> str:
    """Expose declarations nested in ``@media`` / ``@supports`` for parsing."""
    result: list[str] = []
    index = 0
    length = len(source)
    lower = source.lower()
    while index < length:
        at_index = lower.find("@", index)
        if at_index == -1:
            result.append(source[index:])
            break
        result.append(source[index:at_index])
        brace = source.find("{", at_index)
        if brace == -1:
            result.append(source[at_index:])
            break
        depth = 0
        end = None
        for cursor in range(brace, length):
            char = source[cursor]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = cursor
                    break
        if end is None:
            result.append(source[at_index:])
            break
        header = lower[at_index:brace].strip()
        inner = source[brace + 1 : end]
        if header.startswith("@media") or header.startswith("@supports"):
            result.append(_unwrap_at_rules(inner))
        index = end + 1
    return "".join(result)


def _normalize_css_declaration_value(value: str) -> str:
    candidate = value.strip()
    for delimiter in ('"', "'", ">"):
        idx = candidate.find(delimiter)
        if idx != -1:
            candidate = candidate[:idx]
    candidate = candidate.strip()
    if candidate.lower().endswith("!important"):
        candidate = candidate[: -len("!important")].rstrip()
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


def _css_background_shorthand_color(value: str) -> str | None:
    """Extract a visible color token from a CSS background shorthand value."""
    candidate = _normalize_css_declaration_value(value).lower()
    if not candidate:
        return None
    skip_tokens = frozenset(
        {
            "no-repeat",
            "repeat",
            "repeat-x",
            "repeat-y",
            "fixed",
            "scroll",
            "local",
            "cover",
            "contain",
            "top",
            "bottom",
            "left",
            "right",
            "center",
        }
    )
    for token in candidate.split():
        token = token.rstrip(",").strip()
        if not token or token in skip_tokens:
            continue
        if token.startswith("url("):
            continue
        if _css_color_value_is_meaningful(token):
            return _normalize_css_color_token(token)
    return None


def _css_background_shorthand_is_meaningful(value: str) -> bool:
    """Return True when a background shorthand includes a visible color."""
    return _css_background_shorthand_color(value) is not None


def _css_value_has_url(value: str) -> bool:
    """Return True when a CSS value includes a real ``url(...)`` image."""
    if not _CSS_URL_TOKEN.search(value or ""):
        return False
    compact = (value or "").lower().replace(" ", "")
    without_placeholder = compact.replace("url(none)", "")
    return "url(" in without_placeholder


def _html_background_attr_is_image(value: str) -> bool:
    """Return True when an HTML ``background`` attribute is an image, not a color."""
    candidate = value.strip()
    if not candidate:
        return False
    lower = candidate.lower()
    if lower.startswith(("http://", "https://", "cid:", "data:", "/", "./", "url(")):
        return True
    if _parse_css_color_rgb(candidate) is not None:
        return False
    if lower in _TRANSPARENT_COLOR_VALUES:
        return False
    if "." in candidate:
        return True
    return False


def _html_background_attr_color(value: str) -> str | None:
    """Return a color token from an HTML ``background`` attribute, if any."""
    if not value or _html_background_attr_is_image(value):
        return None
    if _css_color_value_is_meaningful(value):
        return _normalize_css_color_token(value)
    return None


def _style_source_has_background_image(source: str) -> bool:
    if _DECL_BG_IMAGE.search(source) and _css_value_has_url(source):
        return True
    for match in _DECL_BACKGROUND_VALUE.finditer(source):
        if _css_value_has_url(match.group(1)):
            return True
    return False


def _style_source_has_meaningful_background(source: str) -> bool:
    for match in _DECL_BG_COLOR_VALUE.finditer(source):
        if _css_color_value_is_meaningful(match.group(1)):
            return True
    for match in _DECL_BACKGROUND_VALUE.finditer(source):
        if _css_background_shorthand_is_meaningful(match.group(1)):
            return True
    return False


def _style_source_has_text_color(source: str) -> bool:
    for match in _DECL_TEXT_COLOR_VALUE.finditer(source):
        if _normalize_css_declaration_value(match.group(1)):
            return True
    return False


def _parse_style_declarations(style: str) -> dict[str, str]:
    declarations: dict[str, str] = {}
    depth = 0
    current: list[str] = []
    for char in style:
        if char == "(":
            depth += 1
            current.append(char)
        elif char == ")" and depth:
            depth -= 1
            current.append(char)
        elif char == ";" and depth == 0:
            _store_style_declaration(declarations, "".join(current))
            current = []
        else:
            current.append(char)
    if current:
        _store_style_declaration(declarations, "".join(current))
    return declarations


def _store_style_declaration(declarations: dict[str, str], part: str) -> None:
    if ":" not in part:
        return
    name, _, value = part.partition(":")
    name = name.strip().lower()
    value = value.strip()
    if name and value:
        declarations[name] = value


def _declarations_to_style(declarations: dict[str, str]) -> str:
    return ";".join(f"{name}:{value}" for name, value in declarations.items())


def _class_names_from_complex_selector(selector: str) -> set[str]:
    """Harvest class names from otherwise-complex CSS selectors (#320)."""
    names: set[str] = {match.group(1).lower() for match in _CSS_CLASS_TOKEN.finditer(selector)}
    for match in _CSS_CLASS_ATTR.finditer(selector):
        for part in match.group(2).split():
            cleaned = part.strip().lower()
            if cleaned:
                names.add(cleaned)
    return names


def _id_names_from_complex_selector(selector: str) -> set[str]:
    """Harvest id tokens from otherwise-complex CSS selectors."""
    return {match.group(1).lower() for match in _CSS_ID_TOKEN.finditer(selector)}


def _merge_class_declarations(
    class_styles: dict[str, dict[str, str]],
    class_name: str,
    declarations: dict[str, str],
) -> None:
    key = class_name.lower()
    class_styles[key] = {**class_styles.get(key, {}), **declarations}


def _merge_id_declarations(
    id_styles: dict[str, dict[str, str]],
    id_name: str,
    declarations: dict[str, str],
) -> None:
    key = id_name.lower()
    id_styles[key] = {**id_styles.get(key, {}), **declarations}


def _parse_class_styles(body_html: str) -> dict[str, dict[str, str]]:
    class_styles, _tag_styles, _id_styles = _parse_stylesheet_maps(body_html)
    return class_styles


def _parse_stylesheet_maps(
    body_html: str,
) -> tuple[
    dict[str, dict[str, str]],
    dict[str, dict[str, str]],
    dict[str, dict[str, str]],
]:
    """Parse ``.class`` / ``#id`` / tag selectors; harvest tokens from complex ones."""
    class_styles: dict[str, dict[str, str]] = {}
    tag_styles: dict[str, dict[str, str]] = {}
    id_styles: dict[str, dict[str, str]] = {}
    for style_match in _STYLE_BLOCK.finditer(body_html):
        style_source = _unwrap_at_rules(style_match.group(1))
        for rule_match in _STYLE_RULE_BLOCK.finditer(style_source):
            selector_group = rule_match.group(1).strip()
            if not selector_group or selector_group.startswith("@"):
                continue
            declarations = _parse_style_declarations(rule_match.group(2))
            if not declarations:
                continue
            for raw_selector in selector_group.split(","):
                match = _SIMPLE_CSS_SELECTOR.match(raw_selector)
                if match is not None:
                    _tag_prefix, class_name, id_name, tag_name = match.groups()
                    if class_name is not None:
                        _merge_class_declarations(
                            class_styles, class_name, declarations
                        )
                    elif id_name is not None:
                        _merge_id_declarations(id_styles, id_name, declarations)
                    elif tag_name is not None:
                        key = tag_name.lower()
                        tag_styles[key] = {
                            **tag_styles.get(key, {}),
                            **declarations,
                        }
                    continue
                for class_name in _class_names_from_complex_selector(raw_selector):
                    _merge_class_declarations(class_styles, class_name, declarations)
                for id_name in _id_names_from_complex_selector(raw_selector):
                    _merge_id_declarations(id_styles, id_name, declarations)
    return class_styles, tag_styles, id_styles


def _inherited_root_styles(
    tag_styles: dict[str, dict[str, str]],
) -> dict[str, str]:
    """Color defaults from ``html`` / ``body`` rules (background is not inherited)."""
    inherited: dict[str, str] = {}
    for key in ("html", "body"):
        color = tag_styles.get(key, {}).get("color")
        if color and _normalize_css_declaration_value(color):
            inherited["color"] = color
    return inherited


def _merge_element_declarations(
    attrs: dict[str, str],
    class_styles: dict[str, dict[str, str]],
    *,
    tag: str = "",
    tag_styles: dict[str, dict[str, str]] | None = None,
    id_styles: dict[str, dict[str, str]] | None = None,
    inherited_styles: dict[str, str] | None = None,
) -> dict[str, str]:
    merged: dict[str, str] = {}
    if inherited_styles:
        merged.update(inherited_styles)
    if tag_styles and tag:
        merged.update(tag_styles.get(tag.lower(), {}))
    if id_styles:
        element_id = attrs.get("id", "")
        if element_id:
            merged.update(id_styles.get(element_id.lower(), {}))
    for class_name in attrs.get("class", "").split():
        for name, value in class_styles.get(class_name.lower(), {}).items():
            merged[name] = value
    merged.update(_parse_style_declarations(attrs.get("style", "")))
    return merged


def _declarations_have_text_color(declarations: dict[str, str]) -> bool:
    color = declarations.get("color", "")
    return bool(color and _normalize_css_declaration_value(color))


def _declarations_background_value(declarations: dict[str, str]) -> str | None:
    if "background-color" in declarations:
        value = declarations["background-color"]
        if _css_color_value_is_meaningful(value):
            return _normalize_css_color_token(value)
    if "background" in declarations:
        value = declarations["background"]
        color = _css_background_shorthand_color(value)
        if color is not None:
            return _normalize_css_color_token(color)
    return None


def _declarations_have_meaningful_background(declarations: dict[str, str]) -> bool:
    return _declarations_background_value(declarations) is not None


def _element_painted_background_value(
    attrs: dict[str, str], merged: dict[str, str]
) -> str | None:
    value = _declarations_background_value(merged)
    if value is not None:
        return value
    bgcolor = attrs.get("bgcolor", "")
    if _bgcolor_attr_is_meaningful(bgcolor):
        return _normalize_css_color_token(bgcolor)
    return _html_background_attr_color(attrs.get("background", ""))


def _declarations_have_background_image(declarations: dict[str, str]) -> bool:
    return _css_value_has_url(declarations.get("background-image", "")) or _css_value_has_url(
        declarations.get("background", "")
    )


def _element_has_background_image(
    attrs: dict[str, str],
    class_styles: dict[str, dict[str, str]],
    *,
    tag: str = "",
    tag_styles: dict[str, dict[str, str]] | None = None,
    id_styles: dict[str, dict[str, str]] | None = None,
) -> bool:
    html_bg = attrs.get("background", "")
    if html_bg and _html_background_attr_is_image(html_bg):
        return True
    merged = _merge_element_declarations(
        attrs,
        class_styles,
        tag=tag,
        tag_styles=tag_styles,
        id_styles=id_styles,
        inherited_styles=None,
    )
    return _declarations_have_background_image(merged)


def _parse_css_color_rgb(value: str) -> tuple[int, int, int] | None:
    candidate = _normalize_css_declaration_value(value).lower()
    if not candidate:
        return None
    if candidate in _NAMED_CSS_COLORS:
        return _NAMED_CSS_COLORS[candidate]
    if candidate.startswith("#"):
        hex_value = candidate[1:]
    elif len(candidate) in (3, 6) and all(
        ch in "0123456789abcdef" for ch in candidate
    ):
        # Microsoft mail often omits '#' (e.g. background-color: E5E5E5).
        hex_value = candidate
    else:
        hex_value = ""
    if hex_value:
        if len(hex_value) == 3:
            hex_value = "".join(ch * 2 for ch in hex_value)
        if len(hex_value) == 6:
            try:
                return (
                    int(hex_value[0:2], 16),
                    int(hex_value[2:4], 16),
                    int(hex_value[4:6], 16),
                )
            except ValueError:
                return None
    rgb_match = _RGBA_COLOR.fullmatch(candidate)
    if rgb_match:
        parts = [p.strip() for p in candidate[candidate.find("(") + 1 : candidate.rfind(")")].split(",")]
        if len(parts) >= 3:
            try:
                return int(float(parts[0])), int(float(parts[1])), int(float(parts[2]))
            except ValueError:
                return None
    return None


def _normalize_css_color_token(value: str) -> str:
    """Normalize a color token; prefix bare 3/6-digit hex with ``#``."""
    candidate = _normalize_css_declaration_value(value)
    if not candidate:
        return candidate
    lower = candidate.lower()
    if lower.startswith("#") or lower in _NAMED_CSS_COLORS or "(" in lower:
        return candidate
    if len(lower) in (3, 6) and all(ch in "0123456789abcdef" for ch in lower):
        return f"#{candidate}"
    return candidate


def _relative_luminance(red: int, green: int, blue: int) -> float:
    def channel(value: int) -> float:
        scaled = value / 255
        if scaled <= 0.03928:
            return scaled / 12.92
        return ((scaled + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def _contrasting_text_color(background: str) -> str:
    rgb = _parse_css_color_rgb(background)
    if rgb is None:
        return "#1e1e1e"
    if _relative_luminance(*rgb) > 0.6:
        return "#1e1e1e"
    return "#eeeeee"


def _sender_canvas_opposes_shell(paint_background: str, shell_background: str) -> bool:
    """True when a light sender canvas sits on a dark shell (or the reverse)."""
    paint = _parse_css_color_rgb(paint_background)
    shell = _parse_css_color_rgb(shell_background)
    if paint is None or shell is None:
        return False
    paint_lum = _relative_luminance(*paint)
    shell_lum = _relative_luminance(*shell)
    return (paint_lum > 0.6 and shell_lum <= 0.4) or (
        paint_lum < 0.2 and shell_lum >= 0.6
    )


def _bgcolor_attr_is_meaningful(bgcolor: str) -> bool:
    return _css_color_value_is_meaningful(bgcolor)


def _element_has_explicit_text_color(
    tag: str,
    attrs: dict[str, str],
    class_styles: dict[str, dict[str, str]] | None = None,
    *,
    tag_styles: dict[str, dict[str, str]] | None = None,
    id_styles: dict[str, dict[str, str]] | None = None,
    inherited_styles: dict[str, str] | None = None,
) -> bool:
    if class_styles is not None and _declarations_have_text_color(
        _merge_element_declarations(
            attrs,
            class_styles,
            tag=tag,
            tag_styles=tag_styles,
            id_styles=id_styles,
            inherited_styles=inherited_styles,
        )
    ):
        return True
    if _style_source_has_text_color(attrs.get("style", "")):
        return True
    tag_lower = tag.lower()
    if tag_lower == "font" and "color" in attrs:
        return True
    if tag_lower == "body" and "text" in attrs:
        return True
    return False


def _element_has_meaningful_background(
    attrs: dict[str, str],
    class_styles: dict[str, dict[str, str]] | None = None,
    *,
    tag: str = "",
    tag_styles: dict[str, dict[str, str]] | None = None,
    id_styles: dict[str, dict[str, str]] | None = None,
    inherited_styles: dict[str, str] | None = None,
) -> bool:
    if _bgcolor_attr_is_meaningful(attrs.get("bgcolor", "")):
        return True
    if _html_background_attr_color(attrs.get("background", "")):
        return True
    if class_styles is not None and _declarations_have_meaningful_background(
        _merge_element_declarations(
            attrs,
            class_styles,
            tag=tag,
            tag_styles=tag_styles,
            id_styles=id_styles,
            inherited_styles=inherited_styles,
        )
    ):
        return True
    return _style_source_has_meaningful_background(attrs.get("style", ""))


def _declarations_text_color_value(declarations: dict[str, str]) -> str | None:
    color = declarations.get("color", "")
    if color and _normalize_css_declaration_value(color):
        return _normalize_css_declaration_value(color)
    return None


def _element_text_color_value(
    tag: str,
    attrs: dict[str, str],
    class_styles: dict[str, dict[str, str]] | None = None,
    *,
    tag_styles: dict[str, dict[str, str]] | None = None,
    id_styles: dict[str, dict[str, str]] | None = None,
    inherited_styles: dict[str, str] | None = None,
) -> str | None:
    if class_styles is not None:
        value = _declarations_text_color_value(
            _merge_element_declarations(
                attrs,
                class_styles,
                tag=tag,
                tag_styles=tag_styles,
                id_styles=id_styles,
                inherited_styles=inherited_styles,
            )
        )
        if value:
            return value
    for match in _DECL_TEXT_COLOR_VALUE.finditer(attrs.get("style", "")):
        value = _normalize_css_declaration_value(match.group(1))
        if value:
            return value
    tag_lower = tag.lower()
    if tag_lower == "font" and "color" in attrs:
        return _normalize_css_declaration_value(attrs["color"])
    if tag_lower == "body" and "text" in attrs:
        return _normalize_css_declaration_value(attrs["text"])
    return None


def _colors_have_adequate_contrast(foreground: str, background: str) -> bool:
    """Return True when foreground/background meet a relaxed readability ratio."""
    fg = _parse_css_color_rgb(foreground)
    bg = _parse_css_color_rgb(background)
    if fg is None or bg is None:
        return True
    l1 = _relative_luminance(*fg)
    l2 = _relative_luminance(*bg)
    lighter = max(l1, l2)
    darker = min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05) >= 3.0


def _remove_attr_from_attrs(
    attrs: list[tuple[str, str | None]], name: str
) -> list[tuple[str, str | None]]:
    lower = name.lower()
    return [(attr_name, attr_value) for attr_name, attr_value in attrs if attr_name.lower() != lower]


def _append_style_declaration(
    attrs: list[tuple[str, str | None]], name: str, value: str
) -> list[tuple[str, str | None]]:
    found = False
    updated: list[tuple[str, str | None]] = []
    declarations: dict[str, str] = {}
    for attr_name, attr_value in attrs:
        if attr_name.lower() == "style":
            found = True
            declarations = _parse_style_declarations(attr_value or "")
            declarations[name] = value
            updated.append(("style", _declarations_to_style(declarations)))
        else:
            updated.append((attr_name, attr_value))
    if not found:
        updated.append(("style", f"{name}:{value}"))
    return updated


_VOID_HTML_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_ON_SHELL_TEXT_TAGS = frozenset(
    {
        "p",
        "div",
        "span",
        "li",
        "td",
        "th",
        "font",
        "blockquote",
        "pre",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }
)
_CANVAS_SHELL = "shell"
_CANVAS_COLOR = "color"
_CANVAS_IMAGE = "image"
_ROOT_CANVAS_TAGS = frozenset({"html", "body"})


@dataclass(frozen=True)
class _CanvasFrame:
    """Effective background for one open element in the adaptation walk."""

    tag: str
    background: str
    kind: str


_ADAPTATION_SKIP_TAGS = frozenset(
    {
        "style",
        "script",
        "head",
        "meta",
        "link",
        "title",
        "noscript",
    }
)
_BRACKETED_LITERAL = re.compile(
    r"(?:&lt;|<)"
    r"([A-Za-z][\w.-]*\.[A-Za-z0-9]{2,8}|[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})"
    r"(?:&gt;|>)"
)
_BRACKETED_MAILTO_LINK = re.compile(
    r"(?:&lt;|<)\s*"
    r"(<a\b[^>]*\bhref=[\"']mailto:[^\"']+[\"'][^>]*>.*?</a>)"
    r"\s*(?:&gt;|>)",
    re.IGNORECASE | re.DOTALL,
)
_BRACKETED_SPAN = re.compile(r'<span class="post-bracketed">(.*?)</span>', re.DOTALL)


def _normalize_bracketed_literals(body_html: str) -> str:
    """Preserve ``<file.jpg>`` and ``<email@host>`` through HTML adaptation."""
    body_html = _BRACKETED_MAILTO_LINK.sub(
        lambda match: f'<span class="post-bracketed">{match.group(1)}</span>',
        body_html,
    )
    return _BRACKETED_LITERAL.sub(
        lambda match: f'<span class="post-bracketed">{match.group(1)}</span>',
        body_html,
    )


def _embed_bracketed_span_literals(body_html: str) -> str:
    """Insert visible ``<``/``>`` around normalized bracket spans for WebKit."""
    return _BRACKETED_SPAN.sub(
        lambda match: (
            f'<span class="post-bracketed">&#x3C;{match.group(1)}&#x3E;</span>'
        ),
        body_html,
    )


def _attrs_list_to_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    return {name: value for name, value in attrs if value is not None}


def _add_class_to_attrs(
    attrs: list[tuple[str, str | None]], class_name: str
) -> list[tuple[str, str | None]]:
    found = False
    updated: list[tuple[str, str | None]] = []
    for name, value in attrs:
        if name.lower() == "class":
            found = True
            existing = value or ""
            classes = existing.split()
            if class_name not in classes:
                value = f"{existing} {class_name}".strip()
        updated.append((name, value))
    if not found:
        updated.append(("class", class_name))
    return updated


def _add_classes_to_attrs(
    attrs: list[tuple[str, str | None]], class_names: list[str]
) -> list[tuple[str, str | None]]:
    updated = attrs
    for class_name in class_names:
        updated = _add_class_to_attrs(updated, class_name)
    return updated


def _format_start_tag(tag: str, attrs: list[tuple[str, str | None]]) -> str:
    if not attrs:
        return f"<{tag}>"
    attr_parts: list[str] = []
    for name, value in attrs:
        if value is None:
            attr_parts.append(f" {name}")
        else:
            attr_parts.append(f' {name}="{value}"')
    return f"<{tag}{''.join(attr_parts)}>"


class _AdaptationClassMarker(HTMLParser):
    """Mark elements using contrast against the effective painted background."""

    def __init__(
        self,
        class_styles: dict[str, dict[str, str]],
        tag_styles: dict[str, dict[str, str]],
        id_styles: dict[str, dict[str, str]],
        *,
        shell_background: str,
        inherited_styles: dict[str, str] | None = None,
        prefer_reader_shell: bool = True,
    ) -> None:
        super().__init__(convert_charrefs=False)
        self._class_styles = class_styles
        self._tag_styles = tag_styles
        self._id_styles = id_styles
        self._inherited_styles = inherited_styles or {}
        self._shell_background = shell_background
        self._prefer_reader_shell = prefer_reader_shell
        self._stack: list[_CanvasFrame] = []
        self._parts: list[str] = []

    def get_result(self) -> str:
        return "".join(self._parts)

    def _effective(self) -> tuple[str, str]:
        if self._stack:
            frame = self._stack[-1]
            return frame.background, frame.kind
        return self._shell_background, _CANVAS_SHELL

    def _push(self, tag: str, background: str, kind: str) -> None:
        self._stack.append(_CanvasFrame(tag, background, kind))

    def _pop_until(self, tag: str) -> None:
        tag_lower = tag.lower()
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index].tag == tag_lower:
                del self._stack[index:]
                return

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        if tag_lower in _ADAPTATION_SKIP_TAGS:
            self._parts.append(_format_start_tag(tag, attrs))
            if tag_lower not in _VOID_HTML_ELEMENTS:
                background, kind = self._effective()
                self._push(tag_lower, background, kind)
            return
        attrs_dict = _attrs_list_to_dict(attrs)
        paint_merged = _merge_element_declarations(
            attrs_dict,
            self._class_styles,
            tag=tag_lower,
            tag_styles=self._tag_styles,
            id_styles=self._id_styles,
            inherited_styles=None,
        )
        self_painted = _element_has_meaningful_background(
            attrs_dict,
            self._class_styles,
            tag=tag_lower,
            tag_styles=self._tag_styles,
            id_styles=self._id_styles,
            inherited_styles=None,
        )
        paint_background = (
            _element_painted_background_value(attrs_dict, paint_merged)
            if self_painted
            else None
        )
        own_image = _element_has_background_image(
            attrs_dict,
            self._class_styles,
            tag=tag_lower,
            tag_styles=self._tag_styles,
            id_styles=self._id_styles,
        )
        has_sender_color = _element_has_explicit_text_color(
            tag_lower,
            attrs_dict,
            self._class_styles,
            tag_styles=self._tag_styles,
            id_styles=self._id_styles,
            inherited_styles=self._inherited_styles,
        )
        text_color_value = _element_text_color_value(
            tag_lower,
            attrs_dict,
            self._class_styles,
            tag_styles=self._tag_styles,
            id_styles=self._id_styles,
            inherited_styles=self._inherited_styles,
        )
        if "post-bracketed" in attrs_dict.get("class", "").split():
            self._parts.append(_format_start_tag(tag, attrs))
            if tag_lower not in _VOID_HTML_ELEMENTS:
                background, kind = self._effective()
                self._push(tag_lower, background, kind)
            return
        ancestor_background, ancestor_kind = self._effective()
        extra_classes: list[str] = []
        adapt_color: str | None = None
        contrast_color: str | None = None
        keep_color_value: str | None = None
        neutralize_background = False
        neutralize_to_shell = False
        island_paint: str | None = None
        image_root = False
        opposes_shell = bool(
            self._prefer_reader_shell
            and paint_background is not None
            and _sender_canvas_opposes_shell(paint_background, self._shell_background)
        )
        # Shell-first: drop canvases that oppose the reader (white cards on a
        # dark shell, or the reverse). Descendants then contrast against the
        # background that remains — that is Adapt text (#317/#347/#348).
        # Keeping those cards as light islands made newsletters readable but
        # left a white page, and it undid PayPal.
        if own_image and paint_background is None:
            effective_background = ancestor_background
            kind = _CANVAS_IMAGE
            image_root = True
        elif opposes_shell:
            neutralize_background = True
            extra_classes.append("post-neutralized")
            if tag_lower in _ROOT_CANVAS_TAGS:
                neutralize_to_shell = True
                effective_background = self._shell_background
                kind = _CANVAS_SHELL
            else:
                effective_background = ancestor_background
                kind = ancestor_kind
        elif paint_background is not None:
            effective_background = paint_background
            kind = _CANVAS_COLOR
            island_paint = paint_background
        else:
            effective_background = ancestor_background
            kind = ancestor_kind

        if kind == _CANVAS_IMAGE:
            extra_classes.append("post-painted")
            if image_root:
                extra_classes.append("post-image-canvas")
            if has_sender_color and text_color_value:
                extra_classes.append("post-keep-color")
                keep_color_value = text_color_value
        elif has_sender_color:
            if kind == _CANVAS_COLOR:
                extra_classes.append("post-painted")
            if text_color_value and _colors_have_adequate_contrast(
                text_color_value, effective_background
            ):
                extra_classes.append("post-keep-color")
                keep_color_value = text_color_value
            else:
                extra_classes.append("post-adapt-text")
                adapt_color = _contrasting_text_color(effective_background)
        elif kind == _CANVAS_COLOR:
            extra_classes.append("post-painted")
            if paint_background is not None:
                extra_classes.append("post-forced-contrast")
                contrast_color = _contrasting_text_color(effective_background)
        elif kind == _CANVAS_SHELL and tag_lower in _ON_SHELL_TEXT_TAGS:
            extra_classes.append("post-on-shell")

        updated_attrs = attrs
        if extra_classes:
            updated_attrs = _add_classes_to_attrs(updated_attrs, extra_classes)
        if neutralize_background:
            if neutralize_to_shell:
                updated_attrs = _append_style_declaration(
                    updated_attrs,
                    "background-color",
                    f"{self._shell_background} !important",
                )
                updated_attrs = _append_style_declaration(
                    updated_attrs,
                    "background",
                    f"{self._shell_background} !important",
                )
            else:
                updated_attrs = _append_style_declaration(
                    updated_attrs,
                    "background-color",
                    "transparent !important",
                )
                updated_attrs = _append_style_declaration(
                    updated_attrs,
                    "background",
                    "transparent !important",
                )
            updated_attrs = _remove_attr_from_attrs(updated_attrs, "bgcolor")
            if _html_background_attr_color(attrs_dict.get("background", "")):
                updated_attrs = _remove_attr_from_attrs(updated_attrs, "background")
        elif island_paint is not None:
            updated_attrs = _append_style_declaration(
                updated_attrs,
                "background-color",
                f"{island_paint} !important",
            )
        if contrast_color is not None:
            updated_attrs = _append_style_declaration(
                updated_attrs,
                "color",
                f"{contrast_color} !important",
            )
        if adapt_color is not None:
            updated_attrs = _append_style_declaration(
                updated_attrs,
                "color",
                f"{adapt_color} !important",
            )
        if keep_color_value is not None and not _style_source_has_text_color(
            attrs_dict.get("style", "")
        ):
            updated_attrs = _append_style_declaration(
                updated_attrs,
                "color",
                f"{keep_color_value} !important",
            )
        self._parts.append(_format_start_tag(tag, updated_attrs))
        if tag_lower in _VOID_HTML_ELEMENTS:
            return
        self._push(tag_lower, effective_background, kind)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in _VOID_HTML_ELEMENTS:
            return
        self._pop_until(tag)
        self._parts.append(f"</{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def handle_comment(self, data: str) -> None:
        self._parts.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self._parts.append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        self._parts.append(f"<?{data}>")


def _strip_sender_style_blocks(body_html: str) -> str:
    """Remove sender ``<style>`` blocks after styles are consumed for adaptation."""
    return _STYLE_BLOCK.sub("", body_html)


_COMPOSE_SHELL_DARK = "#1e1e1e"
_COMPOSE_SHELL_LIGHT = "#ffffff"


def _text_color_is_theme_locked(foreground: str) -> bool:
    """True when a color fails contrast on a light or dark unpainted page."""
    return not (
        _colors_have_adequate_contrast(foreground, _COMPOSE_SHELL_DARK)
        and _colors_have_adequate_contrast(foreground, _COMPOSE_SHELL_LIGHT)
    )


def _remove_style_declaration(
    attrs: list[tuple[str, str | None]], name: str
) -> list[tuple[str, str | None]]:
    """Drop one CSS property from a ``style`` attribute, or the attr if empty."""
    lower = name.lower()
    updated: list[tuple[str, str | None]] = []
    for attr_name, attr_value in attrs:
        if attr_name.lower() != "style":
            updated.append((attr_name, attr_value))
            continue
        declarations = _parse_style_declarations(attr_value or "")
        declarations.pop(lower, None)
        if declarations:
            updated.append(("style", _declarations_to_style(declarations)))
    return updated


class _ThemeLockedTextColorStripper(HTMLParser):
    """Remove orphan page-color text styles that break light/dark compose."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        # True when this open element or an ancestor has a painted background.
        self._painted_stack: list[bool] = []
        self._tag_stack: list[str] = []
        self._parts: list[str] = []

    def get_result(self) -> str:
        return "".join(self._parts)

    def _on_painted(self) -> bool:
        return bool(self._painted_stack and self._painted_stack[-1])

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        if tag_lower in _ADAPTATION_SKIP_TAGS:
            self._parts.append(_format_start_tag(tag, attrs))
            if tag_lower not in _VOID_HTML_ELEMENTS:
                self._painted_stack.append(self._on_painted())
                self._tag_stack.append(tag_lower)
            return
        attrs_dict = _attrs_list_to_dict(attrs)
        self_painted = _element_has_meaningful_background(attrs_dict)
        on_painted = self_painted or self._on_painted()
        updated_attrs = attrs
        if not on_painted:
            text_color = _element_text_color_value(tag_lower, attrs_dict)
            if text_color and _text_color_is_theme_locked(text_color):
                if _style_source_has_text_color(attrs_dict.get("style", "")):
                    updated_attrs = _remove_style_declaration(updated_attrs, "color")
                if tag_lower == "font" and "color" in attrs_dict:
                    updated_attrs = _remove_attr_from_attrs(updated_attrs, "color")
                if tag_lower == "body" and "text" in attrs_dict:
                    updated_attrs = _remove_attr_from_attrs(updated_attrs, "text")
        self._parts.append(_format_start_tag(tag, updated_attrs))
        if tag_lower in _VOID_HTML_ELEMENTS:
            return
        self._painted_stack.append(on_painted)
        self._tag_stack.append(tag_lower)

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower in _VOID_HTML_ELEMENTS:
            return
        for index in range(len(self._tag_stack) - 1, -1, -1):
            if self._tag_stack[index] == tag_lower:
                del self._tag_stack[index:]
                del self._painted_stack[index:]
                break
        self._parts.append(f"</{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def handle_comment(self, data: str) -> None:
        self._parts.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self._parts.append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        self._parts.append(f"<?{data}>")


def strip_theme_locked_text_colors(body_html: str) -> str:
    """Strip orphan near-black/near-white text colors from HTML for compose quotes.

    Colors that sit on a painted sender background are kept. Colors without a
    local canvas that fail contrast against a light *or* dark page shell are
    removed so text inherits (readable in dark compose, safe for light MIME
    recipients). Does not neutralize backgrounds or bake a shell foreground.
    """
    if not body_html:
        return body_html
    stripper = _ThemeLockedTextColorStripper()
    try:
        stripper.feed(body_html)
        stripper.close()
    except Exception:
        return body_html
    return stripper.get_result()


def mark_adaptation_classes(
    body_html: str,
    *,
    shell_background: str = "#ffffff",
    prefer_reader_shell: bool = True,
) -> str:
    """Add classes that drive per-element adapt-text overrides."""
    body_html = _normalize_bracketed_literals(body_html)
    class_styles, tag_styles, id_styles = _parse_stylesheet_maps(body_html)
    inherited = _inherited_root_styles(tag_styles) if prefer_reader_shell else {}
    marker = _AdaptationClassMarker(
        class_styles,
        tag_styles,
        id_styles,
        shell_background=shell_background,
        inherited_styles=inherited,
        prefer_reader_shell=prefer_reader_shell,
    )
    try:
        marker.feed(body_html)
        marker.close()
    except Exception:
        return body_html
    return _strip_sender_style_blocks(marker.get_result())


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
    for match in _HTML_BACKGROUND_ATTR.finditer(body_html):
        if _html_background_attr_color(match.group(2)):
            return True
    return any(
        _style_source_has_meaningful_background(source)
        for source in _iter_style_sources(body_html)
    )


def html_has_background_image(body_html: str) -> bool:
    """Return True when HTML paints with a background image and no measured color."""
    for match in _HTML_BACKGROUND_ATTR.finditer(body_html):
        if _html_background_attr_is_image(match.group(2)):
            return True
    return any(
        _style_source_has_background_image(source) for source in _iter_style_sources(body_html)
    )


def html_sender_defines_complete_colors(body_html: str) -> bool:
    """Return True when the sender set both text and background colors."""
    return (
        html_has_explicit_text_color(body_html)
        and html_has_explicit_background_color(body_html)
    )


def _style_blocks_have_text_without_background(body_html: str) -> bool:
    for match in _STYLE_BLOCK.finditer(body_html):
        source = match.group(1)
        if _style_source_has_text_color(source) and not _style_source_has_meaningful_background(
            source
        ):
            return True
    return False


def _marked_html_needs_adaptation(marked: str) -> bool:
    """Return True when marked HTML needs adapt-text treatment."""
    if "post-adapt-text" in marked or "post-forced-contrast" in marked:
        return True
    if "post-image-canvas" in marked or "post-neutralized" in marked:
        return True
    compact = marked.replace(" ", "").lower()
    return "background-color:transparent!important" in compact


def html_message_needs_adaptation(
    body_html: str,
    *,
    prefer_reader_shell: bool = True,
) -> bool:
    """Return True when any element needs adapt-text or adapt-background treatment."""
    content = _html_for_adaptation_detection(body_html)
    if (
        not html_has_explicit_text_color(content)
        and not html_has_explicit_background_color(content)
        and not html_has_background_image(content)
    ):
        return False
    if _style_blocks_have_text_without_background(content):
        return True
    for shell_background in ("#1e1e1e", "#ffffff"):
        marked = mark_adaptation_classes(
            content,
            shell_background=shell_background,
            prefer_reader_shell=prefer_reader_shell,
        )
        if _marked_html_needs_adaptation(marked):
            return True
    return False


def html_should_apply_adaptation(body_html: str) -> bool:
    """Return True when adapt modes should adjust reader colors for this HTML."""
    return html_message_needs_adaptation(body_html)


def _effective_message_appearance(
    body_html: str | None,
    appearance: MessageAppearance,
) -> MessageAppearance:
    if appearance == MESSAGE_APPEARANCE_ACCEPT_SENDER or body_html is None:
        return appearance
    prefer_shell = appearance == MESSAGE_APPEARANCE_ADAPT_TEXT
    prefix, quoted = split_html_at_quote_history(body_html)
    if quoted is not None:
        if not prefix.strip():
            return appearance
        check_html = prefix
    else:
        check_html = body_html
    # Adapt background: self-contained sender canvases keep accept_sender (no shell flip).
    if appearance == MESSAGE_APPEARANCE_ADAPT_BACKGROUND and html_sender_defines_complete_colors(
        check_html
    ):
        if not html_message_needs_adaptation(
            check_html, prefer_reader_shell=False
        ):
            return MESSAGE_APPEARANCE_ACCEPT_SENDER
    if quoted is not None:
        if (
            html_sender_defines_complete_colors(check_html)
            and not html_message_needs_adaptation(
                check_html, prefer_reader_shell=prefer_shell
            )
        ):
            return MESSAGE_APPEARANCE_ACCEPT_SENDER
        return appearance
    if not html_message_needs_adaptation(
        body_html, prefer_reader_shell=prefer_shell
    ):
        return MESSAGE_APPEARANCE_ACCEPT_SENDER
    return appearance


def _effective_reader_dark(app_dark: bool, appearance: MessageAppearance) -> bool:
    if appearance == "adapt_background":
        return not app_dark
    return app_dark


# Plain-text URL shapes for reader linkification (#193). Same character class idea
# as post.mail.helpers._SEARCH_URL_PATTERN.
_PLAIN_URL_PATTERN = re.compile(
    r"https?://[^\s<>'\"\)]+|mailto:[^\s<>'\"\)]+|www\.[^\s<>'\"\)]+",
    re.IGNORECASE,
)


def linkify_plain_text(text: str) -> str:
    """Escape *text* and wrap detected URLs in ``<a href>`` anchors."""
    parts: list[str] = []
    pos = 0
    for match in _PLAIN_URL_PATTERN.finditer(text):
        parts.append(html.escape(text[pos : match.start()]))
        raw = match.group(0)
        display = html.escape(raw)
        if raw.lower().startswith("www."):
            href = html.escape("https://" + raw, quote=True)
        else:
            href = html.escape(raw, quote=True)
        parts.append(f'<a href="{href}">{display}</a>')
        pos = match.end()
    parts.append(html.escape(text[pos:]))
    return "".join(parts)


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
    reader_dark = _effective_reader_dark(dark, effective_appearance)
    shell_background = "#1e1e1e" if reader_dark else "#ffffff"
    if body_html:
        content = _normalize_bracketed_literals(body_html)
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
            content = mark_adaptation_classes(
                content, shell_background=shell_background
            )
            content = f'<div class="message-body">{content}</div>'
        content = _embed_bracketed_span_literals(content)
    elif body_plain:
        content = f'<pre class="plain-body">{linkify_plain_text(body_plain)}</pre>'
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

    reader_css = _READER_CSS_DARK if reader_dark else _READER_CSS_LIGHT
    if effective_appearance == "adapt_text" and html_body:
        reader_css = f"{reader_css}\n{_ADAPT_TEXT_CSS}"

    color_scheme = "dark" if reader_dark else "light"
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
