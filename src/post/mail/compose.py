# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Plain-text message composition helpers (headless where possible)."""

from __future__ import annotations

import re
from typing import Any

_ADDRESS_SPLIT = re.compile(r",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)")


def parse_address_list(text: str) -> list[str]:
    """Parse a comma-separated To/Cc/Bcc field into address strings."""
    import gi

    gi.require_version("Camel", "1.2")
    from gi.repository import Camel

    text = text.strip()
    if not text:
        return []

    addresses: list[str] = []
    for part in _ADDRESS_SPLIT.split(text):
        part = part.strip()
        if not part:
            continue
        container = Camel.InternetAddress.new()
        container.unformat(part)
        if container.length() == 0:
            raise ValueError(f"Invalid address: {part}")
        for index in range(container.length()):
            ok, name, address = container.get(index)
            if not ok or not address:
                continue
            if "@" not in address:
                raise ValueError(f"Invalid address: {part}")
            if name:
                addresses.append(f"{name} <{address}>")
            else:
                addresses.append(address)
    if not addresses:
        raise ValueError("No valid addresses found")
    return addresses


def addresses_to_internet_address(addresses: list[str]) -> Any | None:
    import gi

    gi.require_version("Camel", "1.2")
    from gi.repository import Camel

    if not addresses:
        return None

    container = Camel.InternetAddress.new()
    for item in addresses:
        single = Camel.InternetAddress.new()
        single.unformat(item.strip())
        for index in range(single.length()):
            ok, name, address = single.get(index)
            if ok and address:
                container.add(name or "", address)
    return container if container.length() > 0 else None


def build_reply_subject(subject: str) -> str:
    subject = (subject or "").strip() or "(no subject)"
    if subject.lower().startswith("re:"):
        return subject
    return f"Re: {subject}"


def extract_reply_address(from_header: str) -> str:
    """Return a single address suitable for the To field when replying."""
    import gi

    gi.require_version("Camel", "1.2")
    from gi.repository import Camel

    from_header = (from_header or "").strip()
    if not from_header:
        raise ValueError("Original message has no From address")

    container = Camel.InternetAddress.new()
    container.unformat(from_header)
    if container.length() == 0:
        return from_header
    ok, name, address = container.get(0)
    if not ok or not address:
        return from_header
    if name:
        return f"{name} <{address}>"
    return address


def quote_plain_reply(original: dict[str, Any], body_plain: str | None) -> str:
    date = original.get("date_received") or original.get("date_sent") or ""
    sender = original.get("from") or ""
    text = (body_plain or "").strip()
    if not text:
        text = "(no message body)"
    quoted = "\n".join(f"> {line}" if line else ">" for line in text.splitlines())
    return f"\n\nOn {date}, {sender} wrote:\n{quoted}\n"


def build_reply_references(message_id: str | None, references: str | None = None) -> str | None:
    message_id = (message_id or "").strip()
    if not message_id:
        return references.strip() if references else None
    if references and message_id in references:
        return references.strip()
    if references:
        return f"{references.strip()} {message_id}"
    return message_id


def build_plain_mime_message(
    *,
    from_name: str | None,
    from_address: str,
    to: list[str],
    cc: list[str] | None,
    bcc: list[str] | None,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> Any:
    import gi

    gi.require_version("Camel", "1.2")
    from gi.repository import Camel

    if not to:
        raise ValueError("At least one To address is required")

    message = Camel.MimeMessage.new()
    message.set_subject(subject or "")

    sender = Camel.InternetAddress.new()
    sender.add(from_name or "", from_address)
    message.set_from(sender)

    to_addrs = addresses_to_internet_address(to)
    if to_addrs is None:
        raise ValueError("At least one valid To address is required")
    message.set_recipients("To", to_addrs)

    cc_addrs = addresses_to_internet_address(cc or [])
    if cc_addrs is not None:
        message.set_recipients("Cc", cc_addrs)

    bcc_addrs = addresses_to_internet_address(bcc or [])
    if bcc_addrs is not None:
        message.set_recipients("Bcc", bcc_addrs)

    if in_reply_to:
        message.set_header("In-Reply-To", in_reply_to)
    if references:
        message.set_header("References", references)

    encoded_body = (body or "").encode("utf-8")
    message.set_content(encoded_body, "text/plain; charset=utf-8")
    return message
