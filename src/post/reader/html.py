# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Build HTML documents for the WebKit reading pane."""

from __future__ import annotations

import base64
import html
import re
from html.parser import HTMLParser

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
span.post-bracketed {
  display: inline;
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
span.post-bracketed {
  display: inline;
}
"""

_ADAPT_TEXT_CSS = """
.message-body :where(
  p, div, span, li, td, th, font, blockquote, pre,
  h1, h2, h3, h4, h5, h6
):not(.post-painted):not(.post-keep-color) {
  color: inherit !important;
}
.message-body .post-painted :where(
  p, div, span, li, td, th, font, blockquote, pre,
  h1, h2, h3, h4, h5, h6
):not(.post-keep-color) {
  color: inherit !important;
}
.message-body a.post-adapt-text {
  color: inherit !important;
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
_CLASS_STYLE_RULE = re.compile(
    r"(?:^|[\s,>+~])(?:[a-zA-Z][\w-]*\.)*\.([a-zA-Z][\w-]*)\s*\{([^}]*)\}",
    re.MULTILINE,
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
_QUOTE_HISTORY_REGEXES = (
    re.compile(
        r'\bid\s*=\s*["\']mail-editor-reference-message-container["\']',
        re.IGNORECASE,
    ),
    re.compile(r'\bid\s*=\s*["\']geary-quote["\']', re.IGNORECASE),
    re.compile(
        r'\bclass\s*=\s*["\'][^"\']*\bgmail_quote\b[^"\']*["\']',
        re.IGNORECASE,
    ),
    re.compile(
        r"\bclass\s*=\s*['\"][^'\"]*\bgmail_quote\b[^'\"]*['\"]",
        re.IGNORECASE,
    ),
    re.compile(r'\bclass\s*=\s*["\'][^"\']*\bpost_quote\b[^"\']*["\']', re.IGNORECASE),
    re.compile(
        r"\bclass\s*=\s*['\"][^'\"]*\bpost_quote\b[^'\"]*['\"]",
        re.IGNORECASE,
    ),
    re.compile(r'\bid\s*=\s*["\']appendonsend["\']', re.IGNORECASE),
    re.compile(r"<blockquote\b", re.IGNORECASE),
)


def _quote_history_boundary_start(body_html: str, match: re.Match[str]) -> int:
    """Return the index where quoted history begins for a marker match."""
    if match.group(0).lstrip().lower().startswith("<blockquote"):
        return match.start()
    boundary = body_html.rfind("<", 0, match.start())
    if boundary == -1:
        return match.start()
    return boundary


def _split_html_at_quote_history(body_html: str) -> tuple[str, str | None]:
    """Split HTML into content before quoted history and the quoted suffix."""
    cut = len(body_html)
    for pattern in _QUOTE_HISTORY_REGEXES:
        match = pattern.search(body_html)
        if match is not None:
            cut = min(cut, _quote_history_boundary_start(body_html, match))
    if cut >= len(body_html):
        return body_html, None
    prefix = body_html[:cut]
    quoted = body_html[cut:]
    if not quoted.strip():
        return body_html, None
    return prefix, quoted


def _html_for_adaptation_detection(body_html: str) -> str:
    """Return the portion of HTML whose colors drive the adapt decision."""
    prefix, _quoted = _split_html_at_quote_history(body_html)
    return prefix


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


def _css_background_shorthand_is_meaningful(value: str) -> bool:
    """Return True when a background shorthand includes a visible color."""
    candidate = _normalize_css_declaration_value(value).lower()
    if not candidate:
        return False
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
    for part in style.split(";"):
        if ":" not in part:
            continue
        name, _, value = part.partition(":")
        name = name.strip().lower()
        value = value.strip()
        if name and value:
            declarations[name] = value
    return declarations


def _declarations_to_style(declarations: dict[str, str]) -> str:
    return ";".join(f"{name}:{value}" for name, value in declarations.items())


def _parse_class_styles(body_html: str) -> dict[str, dict[str, str]]:
    class_styles: dict[str, dict[str, str]] = {}
    for style_match in _STYLE_BLOCK.finditer(body_html):
        for rule_match in _CLASS_STYLE_RULE.finditer(style_match.group(1)):
            class_name = rule_match.group(1).lower()
            declarations = _parse_style_declarations(rule_match.group(2))
            if declarations:
                class_styles[class_name] = declarations
    return class_styles


def _merge_element_declarations(
    attrs: dict[str, str], class_styles: dict[str, dict[str, str]]
) -> dict[str, str]:
    merged = _parse_style_declarations(attrs.get("style", ""))
    for class_name in attrs.get("class", "").split():
        for name, value in class_styles.get(class_name.lower(), {}).items():
            merged.setdefault(name, value)
    return merged


def _declarations_have_text_color(declarations: dict[str, str]) -> bool:
    color = declarations.get("color", "")
    return bool(color and _normalize_css_declaration_value(color))


def _declarations_background_value(declarations: dict[str, str]) -> str | None:
    if "background-color" in declarations:
        value = declarations["background-color"]
        if _css_color_value_is_meaningful(value):
            return value
    if "background" in declarations:
        value = declarations["background"]
        if _css_background_shorthand_is_meaningful(value):
            return value
    return None


def _declarations_have_meaningful_background(declarations: dict[str, str]) -> bool:
    return _declarations_background_value(declarations) is not None


def _parse_css_color_rgb(value: str) -> tuple[int, int, int] | None:
    candidate = _normalize_css_declaration_value(value).lower()
    if not candidate:
        return None
    if candidate in _NAMED_CSS_COLORS:
        return _NAMED_CSS_COLORS[candidate]
    if candidate.startswith("#"):
        hex_value = candidate[1:]
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


def _bgcolor_attr_is_meaningful(bgcolor: str) -> bool:
    return _css_color_value_is_meaningful(bgcolor)


def _element_has_explicit_text_color(
    tag: str,
    attrs: dict[str, str],
    class_styles: dict[str, dict[str, str]] | None = None,
) -> bool:
    if class_styles is not None and _declarations_have_text_color(
        _merge_element_declarations(attrs, class_styles)
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
) -> bool:
    if _bgcolor_attr_is_meaningful(attrs.get("bgcolor", "")):
        return True
    if class_styles is not None and _declarations_have_meaningful_background(
        _merge_element_declarations(attrs, class_styles)
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
) -> str | None:
    if class_styles is not None:
        value = _declarations_text_color_value(
            _merge_element_declarations(attrs, class_styles)
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
    """Mark elements that should keep sender colors or receive adapted text."""

    def __init__(
        self,
        class_styles: dict[str, dict[str, str]],
        *,
        shell_background: str,
    ) -> None:
        super().__init__(convert_charrefs=False)
        self._class_styles = class_styles
        self._shell_background = shell_background
        self._inside_painted_depth = 0
        self._parts: list[str] = []

    def get_result(self) -> str:
        return "".join(self._parts)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        attrs_dict = _attrs_list_to_dict(attrs)
        merged = _merge_element_declarations(attrs_dict, self._class_styles)
        inside_painted = self._inside_painted_depth > 0
        self_painted = _element_has_meaningful_background(attrs_dict, self._class_styles)
        has_sender_color = _element_has_explicit_text_color(
            tag_lower, attrs_dict, self._class_styles
        )
        text_color_value = _element_text_color_value(
            tag_lower, attrs_dict, self._class_styles
        )
        extra_classes: list[str] = []
        if inside_painted or self_painted:
            extra_classes.append("post-painted")
            if has_sender_color:
                extra_classes.append("post-keep-color")
        elif has_sender_color:
            background = _declarations_background_value(merged) or self._shell_background
            if text_color_value and _colors_have_adequate_contrast(
                text_color_value, background
            ):
                extra_classes.append("post-keep-color")
            else:
                extra_classes.append("post-adapt-text")
        updated_attrs = attrs
        if extra_classes:
            updated_attrs = _add_classes_to_attrs(updated_attrs, extra_classes)
        if self_painted and not inside_painted and not _declarations_have_text_color(merged):
            background = _declarations_background_value(merged)
            if background is not None:
                updated_attrs = _append_style_declaration(
                    updated_attrs,
                    "color",
                    _contrasting_text_color(background),
                )
        self._parts.append(_format_start_tag(tag, updated_attrs))
        if tag_lower in _VOID_HTML_ELEMENTS:
            return
        if inside_painted or self_painted:
            self._inside_painted_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in _VOID_HTML_ELEMENTS:
            return
        if self._inside_painted_depth > 0:
            self._inside_painted_depth -= 1
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


def mark_adaptation_classes(
    body_html: str,
    *,
    shell_background: str = "#ffffff",
) -> str:
    """Add classes that drive per-element adapt-text overrides."""
    body_html = _normalize_bracketed_literals(body_html)
    class_styles = _parse_class_styles(body_html)
    marker = _AdaptationClassMarker(class_styles, shell_background=shell_background)
    try:
        marker.feed(body_html)
        marker.close()
    except Exception:
        return body_html
    return marker.get_result()


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


def _style_blocks_have_text_without_background(body_html: str) -> bool:
    for match in _STYLE_BLOCK.finditer(body_html):
        source = match.group(1)
        if _style_source_has_text_color(source) and not _style_source_has_meaningful_background(
            source
        ):
            return True
    return False


def html_message_needs_adaptation(body_html: str) -> bool:
    """Return True when any element needs adapt-text or adapt-background treatment."""
    content = _html_for_adaptation_detection(body_html)
    if not html_has_explicit_text_color(content):
        return False
    if _style_blocks_have_text_without_background(content):
        return True
    for shell_background in ("#1e1e1e", "#ffffff"):
        if "post-adapt-text" in mark_adaptation_classes(
            content, shell_background=shell_background
        ):
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
    prefix, quoted = _split_html_at_quote_history(body_html)
    if quoted is not None:
        if not prefix.strip():
            return appearance
        if html_sender_defines_complete_colors(prefix):
            return MESSAGE_APPEARANCE_ACCEPT_SENDER
        return appearance
    if not html_message_needs_adaptation(body_html):
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
