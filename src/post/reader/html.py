# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Build HTML documents for the WebKit reading pane."""

from __future__ import annotations

import html
import re

# Block http(s) images and trackers when remote content is disabled.
_EXTERNAL_IMG = re.compile(
    r'(<img\b[^>]*\ssrc=)(["\'])https?://[^"\']*\2',
    re.IGNORECASE,
)
_EXTERNAL_BG = re.compile(
    r"url\(\s*['\"]?https?://[^)'\"]+['\"]?\s*\)",
    re.IGNORECASE,
)

_READER_CSS = """
body {
  font-family: system-ui, sans-serif;
  font-size: 14px;
  line-height: 1.5;
  margin: 1rem;
  overflow-wrap: anywhere;
}
@media (prefers-color-scheme: light) {
  body { color: #1e1e1e; background: #ffffff; }
}
@media (prefers-color-scheme: dark) {
  body { color: #eeeeee; background: #1e1e1e; }
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


def build_reader_document(
    *,
    body_html: str | None,
    body_plain: str | None,
    allow_remote: bool,
) -> str:
    """Wrap message content in a safe HTML shell for WebKit."""
    blocked_notice = ""
    if body_html:
        content = body_html
        if not allow_remote:
            content = _EXTERNAL_IMG.sub(r'\1""', content)
            content = _EXTERNAL_BG.sub("url(none)", content)
            blocked_notice = (
                '<p class="remote-blocked-notice">'
                "Remote images are hidden. Turn on “Load remote content” to show them."
                "</p>"
            )
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

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="color-scheme" content="light dark">
<meta http-equiv="Content-Security-Policy" content="{csp}">
<style>{_READER_CSS}</style>
</head>
<body>
{blocked_notice}
{content}
</body>
</html>"""
