# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Plain-text message composition helpers (headless where possible)."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Sequence

import gi

gi.require_version("Gio", "2.0")

from gi.repository import Gio

_ADDRESS_SPLIT = re.compile(r",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)")


def bare_address_is_valid(address: str) -> bool:
    """Return True when address has non-empty local and domain parts."""
    address = (address or "").strip()
    if "@" not in address:
        return False
    local, domain = address.rsplit("@", 1)
    return bool(local.strip() and domain.strip())


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
            if not bare_address_is_valid(address):
                raise ValueError(f'The address "{part}" is not valid.')
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


def build_forward_subject(subject: str) -> str:
    subject = (subject or "").strip() or "(no subject)"
    lowered = subject.lower()
    if lowered.startswith("fwd:") or lowered.startswith("fw:"):
        return subject
    return f"Fwd: {subject}"


def quote_plain_forward(original: dict[str, Any], body_plain: str | None) -> str:
    from .helpers import format_message_header

    header = format_message_header(original)
    text = (body_plain or "").strip() or "(no message body)"
    return f"\n\n---------- Forwarded message ---------\n{header}\n\n{text}\n"


def normalize_email(address: str) -> str:
    """Return a lowercase bare email address for comparison."""
    import gi

    gi.require_version("Camel", "1.2")
    from gi.repository import Camel

    address = (address or "").strip()
    if not address:
        return ""

    container = Camel.InternetAddress.new()
    container.unformat(address)
    if container.length() > 0:
        ok, _name, bare = container.get(0)
        if ok and bare:
            return bare.lower()

    match = re.search(r"<([^>]+)>", address)
    if match:
        return match.group(1).strip().lower()
    return address.lower()


def parse_address_header(text: str) -> list[str]:
    """Parse a To/Cc header into formatted address strings (empty if blank)."""
    import gi

    gi.require_version("Camel", "1.2")
    from gi.repository import Camel

    text = (text or "").strip()
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
            if "@" in part:
                addresses.append(part)
            continue
        for index in range(container.length()):
            ok, name, bare = container.get(index)
            if not ok or not bare or "@" not in bare:
                continue
            if name:
                addresses.append(f"{name} <{bare}>")
            else:
                addresses.append(bare)
    return addresses


def format_address_list(addresses: list[str]) -> str:
    return ", ".join(addresses)


def build_reply_all_recipients(
    original: dict[str, Any],
    *,
    own_addresses: set[str],
) -> tuple[list[str], list[str]]:
    """Return To and Cc lists for reply-all.

    To contains all Reply-To addresses (or From when Reply-To is absent), then
    every original To recipient, excluding own addresses.
    Cc contains the original Cc recipients not already in To or own addresses.
    When every To participant is filtered out (e.g. a message sent only to yourself),
    fall back to the reply target so reply-all matches plain reply.
    """
    own = {normalize_email(item) for item in own_addresses if item}

    def is_own(address: str) -> bool:
        return normalize_email(address) in own

    def add_to_unique(
        target: list[str], seen: set[str], address: str
    ) -> None:
        email = normalize_email(address)
        if not email or email in seen or is_own(address):
            return
        seen.add(email)
        target.append(address)

    def add_cc_unique(
        target: list[str], seen: set[str], address: str
    ) -> None:
        """Add a Cc recipient unless already in To or own addresses."""
        email = normalize_email(address)
        if not email or email in seen or is_own(address):
            return
        seen.add(email)
        target.append(address)

    seen: set[str] = set()
    to_addrs: list[str] = []

    try:
        reply_targets = extract_reply_target_addresses(original)
    except ValueError:
        reply_targets = []

    for address in reply_targets:
        add_to_unique(to_addrs, seen, address)

    for address in parse_address_header(original.get("to", "")):
        add_to_unique(to_addrs, seen, address)

    cc_addrs: list[str] = []
    for address in parse_address_header(original.get("cc", "")):
        add_cc_unique(cc_addrs, seen, address)

    if not to_addrs:
        if reply_targets:
            to_addrs = list(reply_targets)
        else:
            raise ValueError("No recipients for reply-all")

    return to_addrs, cc_addrs


def extract_reply_target_addresses(message: dict[str, Any]) -> list[str]:
    """Return all addresses replies should go to (Reply-To if set, else From)."""
    reply_to = (message.get("reply_to") or "").strip()
    if reply_to:
        addresses = parse_address_header(reply_to)
        if addresses:
            return addresses

    from_header = (message.get("from") or "").strip()
    if not from_header:
        raise ValueError("Original message has no From address")
    return [extract_reply_address(from_header)]


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


_QUOTE_LINE_RE = re.compile(r"^(>+)( ?)(.*)$")


def _prefix_reply_quote_line(line: str) -> str:
    """Increase quote depth by one per RFC 3676 (``>`` → ``>>``, not ``> >``)."""
    if not line:
        return ">"
    match = _QUOTE_LINE_RE.match(line)
    if match:
        markers, space, rest = match.groups()
        if not rest:
            return f"{markers}>"
        sep = space if space else " "
        return f"{markers}>{sep}{rest}"
    return f"> {line}"


def body_text_for_quoting(message: dict[str, Any]) -> str | None:
    """Return the best plain-text body to quote when replying or forwarding."""
    from .helpers import html_to_quotable_plain, plain_body_looks_truncated

    plain = (message.get("body_plain") or "").strip() or None
    html = (message.get("body_html") or "").strip() or None
    if plain and not plain_body_looks_truncated(plain, html):
        return plain
    if html:
        converted = html_to_quotable_plain(html).strip()
        if converted:
            return converted
    return plain


def quote_plain_reply(original: dict[str, Any], body_plain: str | None) -> str:
    date = original.get("date_received") or original.get("date_sent") or ""
    sender = original.get("from") or ""
    text = (body_plain or "").strip()
    if not text:
        text = "(no message body)"
    quoted = "\n".join(_prefix_reply_quote_line(line) for line in text.splitlines())
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


def format_signature_block(signature: str) -> str:
    """Return a plain-text signature block with the conventional delimiter."""
    text = signature.strip("\n")
    if not text:
        return ""
    return f"-- \n{text}"


def compose_body_with_signature(
    *,
    mode: str,
    quoted_body: str,
    signature: str | None,
) -> str:
    """Build the initial compose body for a mode, inserting a signature when set."""
    block = format_signature_block(signature or "")
    if not block:
        return quoted_body if mode != "new" else ""

    if mode == "new":
        return f"\n\n{block}"
    if mode in ("reply", "reply-all"):
        quoted = quoted_body.lstrip("\n")
        return f"\n\n{block}\n\n{quoted}"
    return quoted_body


def body_is_unedited_signature_template(body: str, signatures: list[str]) -> bool:
    """True when the body is empty or still matches an auto-inserted signature."""
    if not body.strip():
        return True
    for signature in signatures:
        if body == compose_body_with_signature(
            mode="new",
            quoted_body="",
            signature=signature,
        ):
            return True
    return False


def _encode_plain_body(body: str) -> bytes:
    """Encode a plain-text body for Camel; zero-length payloads break serialization."""
    encoded = (body or "").encode("utf-8")
    return encoded if encoded else b"\n"


@dataclass(frozen=True)
class ComposeAttachment:
    filename: str
    mime_type: str
    data: bytes


def guess_attachment_mime_type(filename: str, data: bytes) -> str:
    """Guess a MIME type for a compose attachment."""
    guessed, _certain = Gio.content_type_guess(filename, data)
    if guessed:
        return guessed
    return "application/octet-stream"


def read_compose_attachments_from_message(mime_msg: Any) -> list[ComposeAttachment]:
    """Extract attachment payloads from a Camel MIME message."""
    from .helpers import extract_attachments, get_attachment_data

    attachments: list[ComposeAttachment] = []
    for meta in extract_attachments(mime_msg):
        index = meta.get("index")
        if index is None:
            continue
        filename, data = get_attachment_data(mime_msg, int(index))
        attachments.append(
            ComposeAttachment(
                filename=filename,
                mime_type=str(meta.get("mime_type") or "application/octet-stream"),
                data=data,
            )
        )
    return attachments


def _apply_compose_headers(
    message: Any,
    *,
    from_name: str | None,
    from_address: str,
    to: list[str] | None,
    cc: list[str] | None,
    bcc: list[str] | None,
    subject: str,
    in_reply_to: str | None,
    references: str | None,
    require_to: bool,
) -> None:
    import gi

    gi.require_version("Camel", "1.2")
    from gi.repository import Camel

    message.set_subject(subject or "")

    sender = Camel.InternetAddress.new()
    sender.add(from_name or "", from_address)
    message.set_from(sender)

    to_addrs = addresses_to_internet_address(to or [])
    if require_to:
        if to_addrs is None:
            raise ValueError("At least one valid To address is required")
        message.set_recipients("To", to_addrs)
    elif to_addrs is not None:
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


def _set_message_body(
    message: Any,
    body: str,
    attachments: Sequence[ComposeAttachment] | None,
) -> None:
    import gi

    gi.require_version("Camel", "1.2")
    from gi.repository import Camel

    encoded_body = _encode_plain_body(body)
    if not attachments:
        message.set_content(encoded_body, "text/plain; charset=utf-8")
        return

    multipart = Camel.Multipart.new()
    multipart.set_boundary(f"----post-{uuid.uuid4().hex}")

    body_part = Camel.MimePart.new()
    body_part.set_content(encoded_body, "text/plain; charset=utf-8")
    body_part.set_encoding(Camel.TransferEncoding.ENCODING_7BIT)
    multipart.add_part(body_part)

    for attachment in attachments:
        part = Camel.MimePart.new()
        part.set_content(attachment.data, attachment.mime_type)
        part.set_disposition("attachment")
        part.set_filename(attachment.filename)
        part.set_encoding(Camel.TransferEncoding.ENCODING_BASE64)
        multipart.add_part(part)

    message.props.content = multipart
    message.set_mime_type("multipart/mixed")


def build_outbound_email_bytes(
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
    attachments: Sequence[ComposeAttachment] | None = None,
) -> bytes:
    """Build a MIME message for SMTP without Camel/GObject."""
    from email.message import EmailMessage
    from email.utils import formataddr

    if not to:
        raise ValueError("At least one To address is required")

    message = EmailMessage()
    message["From"] = (
        formataddr((from_name, from_address)) if from_name else from_address
    )
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    if bcc:
        message["Bcc"] = ", ".join(bcc)
    message["Subject"] = subject or ""
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references

    text = body if body else "\n"
    if not attachments:
        message.set_content(text, charset="utf-8")
        return message.as_bytes()

    message.set_content(text, charset="utf-8")
    for attachment in attachments:
        maintype, _, subtype = attachment.mime_type.partition("/")
        if not subtype:
            maintype, subtype = "application", "octet-stream"
        message.add_attachment(
            attachment.data,
            maintype=maintype,
            subtype=subtype,
            filename=attachment.filename,
        )
    return message.as_bytes()


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
    attachments: Sequence[ComposeAttachment] | None = None,
) -> Any:
    import gi

    gi.require_version("Camel", "1.2")
    from gi.repository import Camel

    if not to:
        raise ValueError("At least one To address is required")

    message = Camel.MimeMessage.new()
    _apply_compose_headers(
        message,
        from_name=from_name,
        from_address=from_address,
        to=to,
        cc=cc,
        bcc=bcc,
        subject=subject,
        in_reply_to=in_reply_to,
        references=references,
        require_to=True,
    )
    _set_message_body(message, body, attachments)
    return message


def build_draft_mime_message(
    *,
    from_name: str | None,
    from_address: str,
    to: list[str] | None,
    cc: list[str] | None,
    bcc: list[str] | None,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    references: str | None = None,
    attachments: Sequence[ComposeAttachment] | None = None,
) -> Any:
    """Build a MIME message for saving to the Drafts folder (recipients optional)."""
    import gi

    gi.require_version("Camel", "1.2")
    from gi.repository import Camel

    message = Camel.MimeMessage.new()
    _apply_compose_headers(
        message,
        from_name=from_name,
        from_address=from_address,
        to=to,
        cc=cc,
        bcc=bcc,
        subject=subject,
        in_reply_to=in_reply_to,
        references=references,
        require_to=False,
    )
    _set_message_body(message, body, attachments)
    return message
