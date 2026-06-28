# Offline body cache and search

Post downloads message **bodies** into Camel's local cache (`~/.cache/evolution/`) so mail can be read and searched offline. Header metadata alone is not enough for body text or attachments.

Epic: [#99](https://github.com/mbrennwa/post/issues/99)

## Settings

**Settings → Offline Mail** configures per-account policy:

| Mode | Behavior |
|------|----------|
| Off | Headers only (default for existing users until changed) |
| Last month | `stay_synchronized` + age limit 1 month |
| Last year | `stay_synchronized` + age limit 1 year |
| Everything | `stay_synchronized`, no age limit |

Applies to **all folders** in remote accounts (IMAP, Exchange, POP).

**Everything** can use significant disk space and bandwidth on large mailboxes. The first-run prompt and Settings both explain this trade-off.

On first launch, Post offers a one-time prompt to enable offline download.

## Architecture

- **`post.preferences`** — per-account `offline_body_sync` in `~/.config/post/preferences.json`
- **`post.mail.offline_settings`** — maps preferences to `Camel.OfflineSettings` and folder `offline_sync`
- **`post.mail.offline_sync`** — background `OfflineFolder.downsync_sync()` on the mail I/O thread
- **`post.mail.search`** — `query_to_sexp()` compiles the search bar DSL to Camel S-expressions; all search runs via `camel_folder_search_by_expression()` (libcamel, not PyGObject `FolderSearch.search()`, which corrupts Camel's UID string pool on repeated searches)

## Search

All search (headers, flags, body) goes through Camel `search_by_expression`. Bare words match headers **and** body.

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

## Shared cache with Evolution

Post configures the same EDS/Camel store as Evolution. Offline settings affect Evolution's local cache for that account.
