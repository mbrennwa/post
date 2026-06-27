# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Plain-text message composition helpers (headless where possible)."""

from __future__ import annotations

import html
import re
import uuid
from dataclasses import dataclass
from typing import Any, Sequence

_OUTBOUND_SMTP_POLICY: Any | None = None

import gi

gi.require_version("Gio", "2.0")

from gi.repository import Gio

_ADDRESS_SPLIT = re.compile(r",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)")
_HEADER_NEWLINES = re.compile(r"[\r\n]+")


def _sanitize_header_field(value: str, *, field: str) -> str:
    """Reject header values that would inject extra MIME header lines."""
    if _HEADER_NEWLINES.search(value):
        raise ValueError(f"{field} must not contain line breaks.")
    return value


def _sanitize_optional_header_field(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    return _sanitize_header_field(value, field=field)


def bare_address_is_valid(address: str) -> bool:
    """Return True when address has non-empty local and domain parts."""
    address = (address or "").strip()
    if "@" not in address:
        return False
    local, domain = address.rsplit("@", 1)
    return bool(local.strip() and domain.strip())


def format_parsed_address(name: str | None, address: str) -> str:
    """Return a display string safe for headers and SMTP envelope parsing."""
    address = _sanitize_header_field(address.strip(), field="Recipient address")
    name = _sanitize_header_field((name or "").strip(), field="Display name")
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


def parse_draft_address_list(text: str) -> list[str]:
    """Parse a To/Cc/Bcc field for draft saving without validating addresses."""
    text = text.strip()
    if not text:
        return []

    addresses: list[str] = []
    for part in _ADDRESS_SPLIT.split(text):
        part = part.strip()
        if not part:
            continue
        addresses.append(_sanitize_header_field(part, field="Recipient address"))
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
                safe_name = _sanitize_optional_header_field(name, field="Display name")
                safe_address = _sanitize_header_field(address, field="Recipient address")
                container.add(safe_name or "", safe_address)
    return container if container.length() > 0 else None


def draft_addresses_to_internet_address(addresses: list[str]) -> Any | None:
    """Build Camel recipients for drafts, preserving unparseable addresses."""
    import gi

    gi.require_version("Camel", "1.2")
    from gi.repository import Camel

    if not addresses:
        return None

    container = Camel.InternetAddress.new()
    for item in addresses:
        single = Camel.InternetAddress.new()
        single.unformat(item.strip())
        if single.length() > 0:
            for index in range(single.length()):
                ok, name, address = single.get(index)
                if ok and address:
                    safe_name = _sanitize_optional_header_field(name, field="Display name")
                    safe_address = _sanitize_header_field(address, field="Recipient address")
                    container.add(safe_name or "", safe_address)
        else:
            container.add(
                "",
                _sanitize_header_field(item.strip(), field="Recipient address"),
            )
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
    from .helpers import format_forward_quote_header

    header = format_forward_quote_header(original)
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
    from .helpers import format_forward_quote_header

    header_lines = format_forward_quote_header(original).splitlines()
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


def _camel_text_transfer_encoding(payload: bytes) -> Any:
    """Pick RFC 2045 CTE for UTF-8 text parts (7bit only when US-ASCII)."""
    import gi

    gi.require_version("Camel", "1.2")
    from gi.repository import Camel

    if payload and all(byte < 0x80 for byte in payload):
        return Camel.TransferEncoding.ENCODING_7BIT
    return Camel.TransferEncoding.ENCODING_8BIT


@dataclass(frozen=True)
class ComposeAttachment:
    filename: str
    mime_type: str
    data: bytes


def validate_compose_mime_fields(
    *,
    from_name: str | None,
    subject: str,
    to: list[str] | None = None,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    attachments: Sequence[ComposeAttachment] | None = None,
) -> None:
    """Reject user-controlled header fields before outbox queue or MIME build."""
    _sanitize_optional_header_field(from_name, field="From name")
    _sanitize_header_field(subject or "", field="Subject")
    _sanitize_optional_header_field(in_reply_to, field="In-Reply-To")
    _sanitize_optional_header_field(references, field="References")
    for field, group in (
        ("To", to),
        ("Cc", cc),
        ("Bcc", bcc),
    ):
        for item in group or []:
            _sanitize_header_field(item.strip(), field=field)
    for group in (to or [], cc or [], bcc or []):
        addresses_to_internet_address(group)
    for attachment in attachments or ():
        _sanitize_header_field(attachment.filename, field="Attachment filename")


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
    message_id: str | None = None,
    date: str | None = None,
    allow_unparseable_recipients: bool = False,
) -> None:
    import gi

    gi.require_version("Camel", "1.2")
    from gi.repository import Camel

    message.set_subject(
        _sanitize_header_field(subject or "", field="Subject")
    )

    sender = Camel.InternetAddress.new()
    safe_from_name = _sanitize_optional_header_field(from_name, field="From name")
    sender.add(safe_from_name or "", from_address)
    message.set_from(sender)

    if allow_unparseable_recipients:
        to_container = draft_addresses_to_internet_address(to or [])
        cc_container = draft_addresses_to_internet_address(cc or [])
        bcc_container = draft_addresses_to_internet_address(bcc or [])
    else:
        to_container = addresses_to_internet_address(to or [])
        cc_container = addresses_to_internet_address(cc or [])
        bcc_container = addresses_to_internet_address(bcc or [])

    if require_to:
        if to_container is None:
            raise ValueError("At least one valid To address is required")
        message.set_recipients("To", to_container)
    elif to_container is not None:
        message.set_recipients("To", to_container)

    if cc_container is not None:
        message.set_recipients("Cc", cc_container)

    if include_bcc_header and bcc_container is not None:
        message.set_recipients("Bcc", bcc_container)

    safe_in_reply_to = _sanitize_optional_header_field(
        in_reply_to, field="In-Reply-To"
    )
    if safe_in_reply_to:
        message.set_header("In-Reply-To", safe_in_reply_to)
    safe_references = _sanitize_optional_header_field(references, field="References")
    if safe_references:
        message.set_header("References", safe_references)

    if message_id:
        message.set_message_id(message_id.strip().strip("<>"))
    if date:
        message.set_header("Date", date)


def _outbound_smtp_policy() -> Any:
    global _OUTBOUND_SMTP_POLICY
    if _OUTBOUND_SMTP_POLICY is None:
        from email import policy

        _OUTBOUND_SMTP_POLICY = policy.SMTP
    return _OUTBOUND_SMTP_POLICY


def _outbound_message_id_domain(from_address: str) -> str:
    _, _, domain = normalize_email(from_address).partition("@")
    return domain or "localhost"


@dataclass(frozen=True)
class OutboundMimeIdentifiers:
    message_id: str
    date: str


def new_outbound_mime_identifiers(from_address: str) -> OutboundMimeIdentifiers:
    """Return RFC 5322 Message-ID and Date for a new outbound message."""
    from email.utils import formatdate, make_msgid

    return OutboundMimeIdentifiers(
        message_id=make_msgid(domain=_outbound_message_id_domain(from_address)),
        date=formatdate(localtime=True),
    )


def build_outbound_email_message(
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
    include_bcc_header: bool = False,
    message_id: str | None = None,
    date: str | None = None,
) -> Any:
    """Build a MIME message for outbound delivery using the stdlib email package."""
    from email.message import EmailMessage
    from email.utils import formataddr

    if not to:
        raise ValueError("At least one To address is required")

    identifiers = new_outbound_mime_identifiers(from_address)
    resolved_message_id = message_id or identifiers.message_id
    resolved_date = date or identifiers.date

    safe_from_name = _sanitize_optional_header_field(from_name, field="From name")
    safe_subject = _sanitize_header_field(subject or "", field="Subject")
    safe_in_reply_to = _sanitize_optional_header_field(
        in_reply_to, field="In-Reply-To"
    )
    safe_references = _sanitize_optional_header_field(references, field="References")
    safe_to = [_sanitize_header_field(item.strip(), field="To") for item in to]
    safe_cc = (
        [_sanitize_header_field(item.strip(), field="Cc") for item in cc] if cc else None
    )
    safe_bcc = (
        [_sanitize_header_field(item.strip(), field="Bcc") for item in bcc]
        if bcc
        else None
    )

    message = EmailMessage(policy=_outbound_smtp_policy())
    message["From"] = (
        formataddr((safe_from_name, from_address)) if safe_from_name else from_address
    )
    message["To"] = ", ".join(safe_to)
    if safe_cc:
        message["Cc"] = ", ".join(safe_cc)
    if include_bcc_header and safe_bcc:
        message["Bcc"] = ", ".join(safe_bcc)
    message["Subject"] = safe_subject
    message["Message-ID"] = resolved_message_id
    message["Date"] = resolved_date
    if safe_in_reply_to:
        message["In-Reply-To"] = safe_in_reply_to
    if safe_references:
        message["References"] = safe_references

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
                filename=_sanitize_header_field(
                    attachment.filename, field="Attachment filename"
                ),
            )
    return message


@dataclass(frozen=True)
class OutboundMimePackage:
    message_id: str
    date: str
    wire_bytes: bytes
    sent_message: Any


def build_outbound_mime_package(
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
) -> OutboundMimePackage:
    """Build wire bytes and a Sent-folder Camel message with matching identifiers."""
    identifiers = new_outbound_mime_identifiers(from_address)
    wire_bytes = build_outbound_email_bytes(
        from_name=from_name,
        from_address=from_address,
        to=to,
        cc=cc,
        bcc=bcc,
        subject=subject,
        body=body,
        body_html=body_html,
        in_reply_to=in_reply_to,
        references=references,
        attachments=attachments,
        message_id=identifiers.message_id,
        date=identifiers.date,
    )
    sent_message = build_plain_mime_message(
        from_name=from_name,
        from_address=from_address,
        to=to,
        cc=cc,
        bcc=bcc,
        subject=subject,
        body=body,
        body_html=body_html,
        in_reply_to=in_reply_to,
        references=references,
        attachments=attachments,
        message_id=identifiers.message_id,
        date=identifiers.date,
    )
    return OutboundMimePackage(
        message_id=identifiers.message_id,
        date=identifiers.date,
        wire_bytes=wire_bytes,
        sent_message=sent_message,
    )


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
    plain_part.set_encoding(_camel_text_transfer_encoding(encoded_plain))
    alternative.add_part(plain_part)

    html_part = Camel.MimePart.new()
    html_part.set_content(encoded_html, "text/html; charset=utf-8")
    html_part.set_encoding(_camel_text_transfer_encoding(encoded_html))
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
        body_part.set_encoding(_camel_text_transfer_encoding(encoded_plain))
        multipart.add_part(body_part)

    for attachment in attachments or ():
        part = Camel.MimePart.new()
        part.set_content(attachment.data, attachment.mime_type)
        part.set_disposition("attachment")
        part.set_filename(
            _sanitize_header_field(attachment.filename, field="Attachment filename")
        )
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
    message_id: str | None = None,
    date: str | None = None,
) -> bytes:
    """Build a MIME message for SMTP/local delivery without Camel/GObject.

    Bcc addresses are omitted from MIME headers; callers must still pass them
    for SMTP RCPT TO / local recipient resolution.
    """
    message = build_outbound_email_message(
        from_name=from_name,
        from_address=from_address,
        to=to,
        cc=cc,
        bcc=bcc,
        subject=subject,
        body=body,
        body_html=body_html,
        in_reply_to=in_reply_to,
        references=references,
        attachments=attachments,
        include_bcc_header=False,
        message_id=message_id,
        date=date,
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
    message_id: str | None = None,
    date: str | None = None,
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
        message_id=message_id,
        date=date,
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
        allow_unparseable_recipients=True,
    )
    _set_message_body(message, body, attachments, body_html=body_html)
    return message
