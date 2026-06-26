# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Plain-text message composition helpers (headless where possible)."""

from __future__ import annotations

import html
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


def format_parsed_address(name: str | None, address: str) -> str:
    """Return a display string safe for headers and SMTP envelope parsing."""
    name = (name or "").strip()
    if not name:
        return address
    # Unquoted @ in display names break email.utils.getaddresses.
    if "@" in name or name.casefold() == address.casefold():
        return address
    return f"{name} <{address}>"


def envelope_recipient_addresses(
    to: list[str],
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
) -> list[str]:
    """Collect bare email addresses for SMTP RCPT TO."""
    import gi

    gi.require_version("Camel", "1.2")
    from gi.repository import Camel

    addresses: list[str] = []
    seen: set[str] = set()
    for group in (to, cc or [], bcc or []):
        for item in group:
            single = Camel.InternetAddress.new()
            single.unformat(item.strip())
            for index in range(single.length()):
                ok, _name, address = single.get(index)
                if not ok or not address:
                    continue
                key = address.casefold()
                if key in seen:
                    continue
                seen.add(key)
                addresses.append(address)
    return addresses


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
            addresses.append(format_parsed_address(name, address))
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
    return f"\n\n{FORWARD_QUOTE_MARKER}\n{header}\n\n{text}\n"


FORWARD_QUOTE_MARKER = "---------- Forwarded message ---------"


def body_html_for_quoting(message: dict[str, Any]) -> str | None:
    """Return original MIME HTML to embed when forwarding or replying."""
    content = (message.get("body_html") or "").strip()
    return content or None


def _strip_html_document_wrappers(body_html: str) -> str:
    """Return inner HTML, dropping outer document wrappers when present."""
    text = body_html.strip()
    body_match = re.search(r"<body\b[^>]*>(.*)</body>", text, re.IGNORECASE | re.DOTALL)
    if body_match:
        return body_match.group(1).strip()
    html_match = re.search(r"<html\b[^>]*>(.*)</html>", text, re.IGNORECASE | re.DOTALL)
    if html_match:
        return html_match.group(1).strip()
    return text


def quote_html_forward(original: dict[str, Any], body_html: str) -> str:
    from .helpers import format_message_header

    header_lines = format_message_header(original).splitlines()
    header_html = "<br>\n".join(html.escape(line) for line in header_lines)
    content = _strip_html_document_wrappers(body_html)
    return (
        f"<div>{FORWARD_QUOTE_MARKER}</div>"
        f"<div>{header_html}</div>"
        f'<blockquote class="post_quote">{content}</blockquote>'
    )


def quote_html_reply(original: dict[str, Any], body_html: str) -> str:
    date = original.get("date_received") or original.get("date_sent") or ""
    sender = html.escape(str(original.get("from") or ""))
    content = _strip_html_document_wrappers(body_html)
    return (
        f"<div>On {html.escape(str(date))}, {sender} wrote:</div>"
        f'<blockquote class="post_quote">{content}</blockquote>'
    )


def plain_to_simple_html(plain: str) -> str:
    """Convert plain text into a minimal HTML fragment."""
    text = plain.rstrip("\n")
    if not text:
        return ""
    return f'<div style="white-space:pre-wrap">{html.escape(text)}</div>'


def plain_quoted_as_html(quoted_plain: str) -> str:
    """Wrap edited plain-text quotes for the HTML body."""
    text = quoted_plain.strip()
    if not text:
        return ""
    return (
        f'<blockquote class="post_quote">'
        f'<div style="white-space:pre-wrap">{html.escape(text)}</div>'
        f"</blockquote>"
    )


_REPLY_QUOTE_MARKER_RE = re.compile(r"\n\nOn .+ wrote:\n", re.DOTALL)


def split_compose_body_at_quote(body_plain: str, mode: str) -> tuple[str, str]:
    """Split compose body into user content and the quoted section."""
    if mode == "forward":
        marker = f"\n\n{FORWARD_QUOTE_MARKER}"
        idx = body_plain.find(marker)
        if idx == -1:
            return body_plain, ""
        return body_plain[:idx], body_plain[idx:]
    if mode in ("reply", "reply-all"):
        match = _REPLY_QUOTE_MARKER_RE.search(body_plain)
        if not match:
            return body_plain, ""
        return body_plain[: match.start()], body_plain[match.start() :]
    return body_plain, ""


def build_outbound_html_body(
    *,
    user_plain: str,
    quoted_html: str | None,
) -> str | None:
    """Assemble the outbound text/html body from user text and quoted HTML."""
    parts: list[str] = []
    if user_plain.strip():
        parts.append(plain_to_simple_html(user_plain.rstrip()))
    if quoted_html:
        parts.append(quoted_html)
    if not parts:
        return None
    return "\n\n".join(parts)


def build_outbound_html_for_compose(
    *,
    body_plain: str,
    mode: str,
    reply_to: dict[str, Any] | None,
    quoted_html_source: str | None,
    quoted_plain_expected: str,
) -> str | None:
    """Build text/html for reply/forward using original MIME HTML when unchanged."""
    if mode not in ("reply", "reply-all", "forward"):
        return None
    user_plain, quoted_plain = split_compose_body_at_quote(body_plain, mode)
    quoted_html: str | None = None
    if quoted_plain.strip():
        if (
            quoted_plain == quoted_plain_expected
            and quoted_html_source
            and reply_to is not None
        ):
            if mode == "forward":
                quoted_html = quote_html_forward(reply_to, quoted_html_source)
            else:
                quoted_html = quote_html_reply(reply_to, quoted_html_source)
        else:
            quoted_html = plain_quoted_as_html(quoted_plain)
    return build_outbound_html_body(user_plain=user_plain, quoted_html=quoted_html)


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
            addresses.append(format_parsed_address(name, bare))
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
    return format_parsed_address(name, address)


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
    include_bcc_header: bool = True,
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

    if include_bcc_header:
        bcc_addrs = addresses_to_internet_address(bcc or [])
        if bcc_addrs is not None:
            message.set_recipients("Bcc", bcc_addrs)

    if in_reply_to:
        message.set_header("In-Reply-To", in_reply_to)
    if references:
        message.set_header("References", references)


def _encode_html_body(body_html: str) -> bytes:
    encoded = (body_html or "").encode("utf-8")
    return encoded if encoded else b"\n"


def _build_alternative_multipart(
    encoded_plain: bytes,
    encoded_html: bytes,
) -> Any:
    import gi

    gi.require_version("Camel", "1.2")
    from gi.repository import Camel

    alternative = Camel.Multipart.new()
    alternative.set_boundary(f"----post-alt-{uuid.uuid4().hex}")

    plain_part = Camel.MimePart.new()
    plain_part.set_content(encoded_plain, "text/plain; charset=utf-8")
    plain_part.set_encoding(Camel.TransferEncoding.ENCODING_7BIT)
    alternative.add_part(plain_part)

    html_part = Camel.MimePart.new()
    html_part.set_content(encoded_html, "text/html; charset=utf-8")
    html_part.set_encoding(Camel.TransferEncoding.ENCODING_7BIT)
    alternative.add_part(html_part)

    return alternative


def _set_message_body(
    message: Any,
    body: str,
    attachments: Sequence[ComposeAttachment] | None,
    body_html: str | None = None,
) -> None:
    import gi

    gi.require_version("Camel", "1.2")
    from gi.repository import Camel

    encoded_plain = _encode_plain_body(body)
    encoded_html = _encode_html_body(body_html) if body_html else None

    if not encoded_html and not attachments:
        message.set_content(encoded_plain, "text/plain; charset=utf-8")
        return

    if encoded_html and not attachments:
        alternative = _build_alternative_multipart(encoded_plain, encoded_html)
        message.props.content = alternative
        message.set_mime_type("multipart/alternative")
        return

    multipart = Camel.Multipart.new()
    multipart.set_boundary(f"----post-{uuid.uuid4().hex}")

    if encoded_html:
        multipart.add_part(_build_alternative_multipart(encoded_plain, encoded_html))
    else:
        body_part = Camel.MimePart.new()
        body_part.set_content(encoded_plain, "text/plain; charset=utf-8")
        body_part.set_encoding(Camel.TransferEncoding.ENCODING_7BIT)
        multipart.add_part(body_part)

    for attachment in attachments or ():
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
    body_html: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    attachments: Sequence[ComposeAttachment] | None = None,
) -> bytes:
    """Build a MIME message for SMTP/local delivery without Camel/GObject.

    Bcc addresses are omitted from MIME headers; callers must still pass them
    for SMTP RCPT TO / local recipient resolution.
    """
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
    message["Subject"] = subject or ""
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references

    text = body if body else "\n"
    message.set_content(text, charset="utf-8")
    if body_html:
        message.add_alternative(body_html, subtype="html", charset="utf-8")
    if attachments:
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
    body_html: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    attachments: Sequence[ComposeAttachment] | None = None,
    include_bcc_header: bool = True,
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
        include_bcc_header=include_bcc_header,
    )
    _set_message_body(message, body, attachments, body_html=body_html)
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
    body_html: str | None = None,
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
    _set_message_body(message, body, attachments, body_html=body_html)
    return message
