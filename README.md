# Post

**Post** is a mail app for GNOME — read and send email with a simple, native desktop experience. It is free and open source, built for Linux.

- **Your accounts, one place** — work across multiple mail accounts without clutter
- **Read mail comfortably** — folders, message list, and a clean reading pane
- **Native and lightweight** — a focused mail client, not a full personal information manager
- **Uses what you already have** — picks up online accounts from GNOME Online Accounts; optional local spool or Maildir in Settings

## Today

- Browse folders and read messages (paginated)
- Multiple accounts with a unified Inbox view
- HTML messages with remote images off by default
- Unread indicators, dates, and attachment hints in the message list
- Mark messages as read when opened; folder counts update
- Open attachments from the reading pane (right-click to save or open with…)
- Clear empty-folder and error states when loading fails
- Context menu: mark read/unread, flag, archive, move to trash (with undo)
- Compose plain-text **New Message** and **Reply** (Ctrl+N); send via account SMTP
- **Settings** (gear icon) — configure local mail from a system spool file or Maildir folder

## Planned

- Save draft, HTML compose, attachments, reply-all, forward
- Conversation threading

## Run

```bash
./run.sh
```

You need at least one mail account: add one in **Settings → Online Accounts**, or enable **local mail** in Post’s Settings (gear icon in the header).

## License

GPL-3.0-or-later — see [LICENSE](LICENSE).
