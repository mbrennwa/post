# Offline body cache and search

Post downloads message **bodies** into Camel's local cache (`~/.cache/evolution/`) so mail can be read and searched offline. Header metadata alone is not enough for body text or attachments.

Epic: [#99](https://github.com/mbrennwa/post/issues/99)

## Settings

**Settings → Offline** configures per-account **body** download into Camel
(`~/.cache/evolution/`). It does **not** store bodies in the folder-index
(`~/.cache/post/folder-index/`). Header search (`from:`, `subject:`, …) uses
the folder-index. `body:` and bare-word search need cached MIME
([#137](https://github.com/mbrennwa/post/issues/137),
[#365](https://github.com/mbrennwa/post/issues/365)).

Policy:

| Mode | Behavior |
|------|----------|
| *(unset)* | No choice yet — Post prompts when the account appears in the sidebar; treated like Off for downloads until set ([#427](https://github.com/mbrennwa/post/issues/427)) |
| Off | Headers only (explicit choice; stored as `"off"`) |
| Last month | `stay_synchronized` + age limit 1 month |
| Last year | `stay_synchronized` + age limit 1 year |
| Everything | `stay_synchronized`, no age limit |

Applies to **all folders** in remote accounts (IMAP, Exchange, POP), including
**Archive, Trash, and Junk**. Valuable mail can land in Trash/Junk by accident;
those folders are indexed and body-cached under the same offline policy
([#208](https://github.com/mbrennwa/post/issues/208)).

Background body backfill order (same policy; priority only):

1. Ordinary folders (Inbox, etc.)
2. Archive / All Mail
3. Trash
4. Junk (lowest)

**Everything** can use significant disk space and bandwidth on large mailboxes. The first-choice prompt and Settings both explain this trade-off.

When a remote account is still **unset**, Post shows a required per-account dialog (Off / Last month / Last year / Everything) as soon as that account is listed in the sidebar. There is no Not Now; Escape chooses Off. Settings → Offline lists only accounts that already have an explicit mode ([#427](https://github.com/mbrennwa/post/issues/427)).

## Architecture

- **`post.preferences`** — per-account `offline_body_sync` in `~/.config/post/preferences.json`
- **`post.mail.offline_settings`** — maps preferences to `Camel.OfflineSettings` and folder `offline_sync`
- **`post.mail.offline_sync`** — background body crawl driven by `OfflineBodySyncCoordinator` in the UI process; Camel `downsync_sync` / arrival `synchronize_message_sync` run via `MailService.list_offline_downsync_folders` / `offline_downsync_folder` / `synchronize_folder_message` on the per-account helper ([#451](https://github.com/mbrennwa/post/issues/451)). Per-folder 30s timeout does not abort the whole account pass; after heavy-folder downsync, re-indexes **local** Camel summary only (`allow_refresh=False`). Body cache does not grow the list past headers Camel already knows. **Arrival prefetch (#372):** when Camel learns a new UID (`Folder::changed` added UIDs, or `refresh_info` UID diff), Post queues a short per-UID fetch on the same background path. That fetch ignores the Archive/heavy-folder **hold** that pauses full-account downsync. Skip when offline policy is Off, the account is taken offline, the network is down, or a **non-empty** RFC822 is already cached (0-byte header stubs are fetched). Bursts are capped at 20 UIDs per folder. Interactive mail I/O preempts between UIDs; mid-FETCH cancel is bounded by the 15s timeout (serial helper IPC).
- **`post.mail.eds` / folder-index** — message list + search candidates live in Post’s folder-index (`~/.cache/post/folder-index/`). Heavy folders (Archive/Trash/Junk/All Mail) use a **chunked, grow-only** background indexer when opened: local summary first, then async `refresh_info` with interactive pump so Graph can enlarge the Camel summary without freezing the UI. Opening a heavy folder holds **that account’s** full-account offline body sync while that folder’s header indexer is running (so Archive Graph catch-up is not abandoned for the same account’s Inbox crawl). Other accounts keep crawling ([#449](https://github.com/mbrennwa/post/issues/449); timing from [#407](https://github.com/mbrennwa/post/issues/407)). The hold lifts when indexing finishes or you leave the folder. Cancel/timeout retries server refresh while the folder stays open. Partial Camel counts must not shrink known STATUS totals (`folder-status` cache is grow-only; only trusted STATUS-sized observations lock in).
- **`post.mail.search`** — `parse_search_query()` and `filter_messages_by_query()` filter the in-memory folder index; bare-word and `body:` terms load cached MIME and match via `searchable_body_text()` (human-readable body text, not raw base64 — [#111](https://github.com/mbrennwa/post/issues/111)). `query_to_sexp()` remains for libcamel integration tests.

List and search require folder-index rows. Bodies in Camel’s cache alone are not enough for a message to appear in the list or search results.

## Search

Folder search filters the folder-index (headers only). Header and flag terms match index metadata directly. Bare-word and `body:` terms load cached MIME and match human-readable body text via `searchable_body_text()`. Offline body download does not put bodies into the folder-index.

Limits: per-folder only; attachment content not searched; offline body matches require cached MIME.

## Threading

Offline downsync and Camel search run on **`post-mail-io`** only. UI updates via `GLib.idle_add`. See [mail-threading.md](mail-threading.md).

## Manual regression matrix

| Scenario | Pass |
|----------|------|
| Enable **Last month** in Settings → status shows download activity | ☑ |
| Wait for download → airplane mode → open never-opened message (in range) | ☑ |
| Search for body-only phrase offline → match found | ☑ |
| Message not yet downloaded offline → clear “not available offline yet” message | ☑ |
| Relaunch app → download resumes without re-fetching completed messages | ☑ |
| Header search offline (`from:`) without server sync | ☑ |
| Open Archive with tiny cache → indexing status grows; list shows newest ≤500 with Showing N of M | ☐ |
| Search older Archive subject after index catch-up | ☐ |
| Trash/Junk accidentally moved mail findable after backfill | ☐ |
| Offline caching order: ordinary before Archive before Trash before Junk | ☐ |

## Shared cache with Evolution

Post configures the same EDS/Camel store as Evolution. Offline settings affect Evolution's local cache for that account.
