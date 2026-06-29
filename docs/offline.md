# Offline mail in Post

Post keeps mail usable when the network is down by relying on Camel/EDS local storage (`~/.cache/evolution/`) plus Post-side caches and queues.

Related: [#6](https://github.com/mbrennwa/post/issues/6), [offline body cache](offline-body-cache.md).

## What works offline

- **Folder lists on cold start** — when the server is unreachable, Post reads folder names from Camel's on-disk store (`dup_downsync_folders` and local folder info), not a separate Post folder list file.
- **Message lists** — in-memory indexes and `~/.cache/post/folder-index/` disk cache; see status *Offline · showing cached list*.
- **Read & search** — cached bodies and headers via Camel; see [offline-body-cache.md](offline-body-cache.md).
- **Compose & send** — outbound mail is queued in `~/.config/post/outbox/` and sent on reconnect.
- **Move, archive, flag** — changes apply locally in the UI; server sync is queued in `~/.config/post/operations/` and flushed on reconnect.
- **Drafts** — queued in `~/.config/post/draft-queue/` when offline (IMAP cannot append to Drafts until reconnect); flushed to the server Drafts folder when back online.

## What is disabled offline

- **Refresh** (sidebar context menu) — greyed out while offline; no server sync is attempted.

## Reconnect

When the network returns:

1. Camel stores go back online (`go_online_sync`)
2. Outbound send queue is flushed
3. Queued move/archive/flag operations are flushed
4. Queued drafts are appended to Drafts on the server
5. Optional body downsync resumes
6. Open folder reloads from server when the account is **online** (user has not taken it offline)

Status bar examples:

- `Offline`
- `Offline · 2 messages queued`
- `Offline · 1 action queued`
- `Offline · 1 draft queued`
- `Offline · 1 message queued · 2 actions queued`

## Per-account offline

Right-click an account header in the sidebar to **Take Offline** or **Take Online**. Offline accounts do not background-sync; use **Refresh** to reload folders and messages from the local Camel cache. A network-offline icon marks offline accounts in the sidebar.

## Manual checks (before closing #6)

| Scenario | Pass |
|----------|------|
| Cold start offline with synced mail — folders visible | |
| Move + flag offline → reconnect → verified on server | |
| Refresh greyed out in sidebar while offline | |
| Save draft offline → toast says will sync; appears in Drafts after reconnect | |
