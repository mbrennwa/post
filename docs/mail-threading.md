# Mail I/O threading

Post keeps the GTK main loop free of blocking Camel / EDS work. Phase 3 (#437 / #422) runs **one Camel helper OS process per account** so a wedged mailbox can be killed without taking the window or other accounts.

## Architecture

```
┌─────────────────────┐  MailService jobs         ┌──────────────────────────┐
│  GTK main thread    │  submit_job / *_async     │  helper acct A (Camel)   │
│  (UI only)          │ ─────────────────────────►│  one MailSession / acct  │
│                     │                           ├──────────────────────────┤
│                     │ ─────────────────────────►│  helper acct B (Camel)   │
│                     │  M365 bg STATUS (HTTP)    ├──────────────────────────┤
│                     │ ─────────────────────────►│  post-graph-http (Soup)  │
│                     │ ◄── GLib.idle_add ─────── │                         │
└─────────────────────┘                           └──────────────────────────┘
```

- **`post.mail.camel_helper`** — per-account helper process (`python3 -m post.mail.camel_helper --account-uid …` / `post-camel-helper`). Owns **one** `MailSession` and a private mail I/O thread in that process. UI talks length-prefixed JSON on stdin/stdout (`camel_ipc.py`).
- **`post.mail.camel_runtime`** — UI supervisor: spawn / call / watchdog timeout → **kill** / respawn; `CamelRuntimePool` keyed by account. Enable with `POST_MAIL_CAMEL_HELPERS=1` (default in the UI process); helpers set `POST_MAIL_CAMEL_HELPER_PROCESS=1` so they do not spawn nested helpers. Unit tests force helpers off via `tests/conftest.py`.
- **`post.mail.io_thread`** — still used **inside** each helper (and in the UI when helpers are disabled). The UI process must not pin a shared `post-mail-io` on helper IPC when helpers are on (`submit_*` runs account work off that shared thread).
- **`MailService`** (`post.mail.eds`) — job facade (#425 / #435 / #437). Prefer `submit_job` with `account_uid`. Camel-backed methods (`read_message`, `list_folders`, `move_messages`, …) proxy to the account helper when helpers are enabled. M365 Graph STATUS stays in-process on `post-graph-http` (#435); OAuth tokens for Graph may be fetched via the helper.
- **Epic lanes (#422 / #435)** — `foreground` = folder you’re in, open, send; `background` = maintenance. Same `(account, folder)` stays one-at-a-time (`FolderJobLock`). Per-helper Camel stays serial; **cross-account** isolation is the Phase 3 win.
- **M365 background STATUS** — Graph folder counts on **`post-graph-http`** (Soup), not a second Camel `*_sync`.
- **Kill / respawn** — `kill_account_camel_helper` / job watchdog timeout terminates that account’s helper; other accounts keep working. `invalidate_account_connection` stops the helper and clears UI caches. `shutdown_sync` shuts down all helpers.
- **UI / mail modules** — must not call `get_mail_io_thread()`. Queue work through `MailService`. `app.py` may start the UI mail thread at startup (compat / helpers-off).

## Rules for contributors

1. **Never call `MailIoThread.run_sync()` from the GTK thread** — it blocks the UI. From GTK use `MailService.submit_interactive` / `submit_front` / `submit_background` / `submit_job` or `*_async` (not `get_mail_io_thread()`).
2. **Never call `Camel.*_sync` directly from UI or ad-hoc worker threads** — go through `MailService` or `run_on_mail_thread()`.
3. **One `MailSession` per process** — in a helper, that session is for one account; do not add a second in-process session in the UI process. Do not reintroduce legacy per-thread worker sessions in the UI.
4. **Password / OAuth prompts** — use `GLib.idle_add` to show dialogs on the GTK thread; mail thread waits on the result. Do **not** call GOA `EnsureCredentials` synchronously from the GTK thread (compose must not preflight on the UI thread; see #156).
5. **Outbound send** — compose persists to outbox first, then delivers via Camel `transport.send_to_sync` on the account helper (or UI mail I/O when helpers are off). No `smtplib` send path. Send and draft save use a **finite** cancellable timeout; draft failures/timeouts fall back to the local draft queue.
6. **Offline body download** — `OfflineBodySyncCoordinator` runs `downsync_sync` on Camel I/O (helper when enabled). Each folder downsync is bound by a **30s** per-folder `Gio.Cancellable` timeout so one slow folder cannot pin that account’s Camel for minutes (#197); a folder timeout does **not** cancel the rest of the account pass (#208). Folders are ordered ordinary → Archive → Trash → Junk. Interactive work preempts offline sync via `cancel_all()` and between-folder yield. **Arrival prefetch (#372)** is a separate per-UID queue (`synchronize_message_sync`, 15s timeout, 20-UID burst cap). It does **not** use full-account downsync and is **not** paused by the Archive hold; interactive I/O still cancels the in-flight FETCH and keeps queued UIDs. See [offline-body-cache.md](offline-body-cache.md).
7. **Sync watcher setup** — `MailSyncWatcher` store/folder signal wiring runs as **background** mail-I/O work so folder search can preempt it. Preempt also cancels in-flight sidebar folder lists.
8. **Search** — interactive mail-I/O work: filter the folder-index (`filter_messages_by_query`). Folder-index rows are **headers only**; `body:` / bare-word terms load cached MIME. Cancellable; preempts offline downsync. See [offline-body-cache.md](offline-body-cache.md).
9. **Correspondents / autocomplete** — build from cached folder tree + folder indexes only; never connect a store just for compose autocomplete (#156).
10. **GOA EnsureCredentials** — D-Bus call uses a finite timeout (not `-1`) so a wedged Online Accounts account cannot pin Camel forever.
11. **Per-account Take offline** — first connect / `set_online_sync` must honor `get_account_user_online`, not only global network availability. From GTK use `submit_front` (never `run_sync`).
12. **Network monitor reconnect (#400)** — `Gio.NetworkMonitor` callbacks run on GTK. `MailService.set_network_available` / `go_online_sync` must **`submit_front`** (never `run_sync` from GTK). Per-store `set_online_sync` uses a **15s** cancellable; one failed/expired OAuth account must not block the rest. Prefer a single offline→online pass (flag + clear folder indexes + timed sync); do not call `go_online_sync` after `set_network_available` from the window.
13. **Folder transfer / Archive (#189/#404)** — `transfer_messages_to_sync` uses a finite `Gio.Cancellable` timeout; soft-succeed when source UIDs are already gone. After move (including soft-succeed), prune Camel `FolderSummary` UIDs locally (Evolution-style) and update Post’s folder-index cache with **destination RestIds only**. Do **not** park Inbox RestIds as openable Archive rows. For a small interactive move, a **bounded** dest `refresh_info` (8s) plus per-UID `synchronize_message_sync` learns dest ids; do **not** block UI completion on unbounded Graph `refresh_info_sync` of a 29k Archive. For `microsoft365` / `ews` bulk, skip post-transfer full-folder `synchronize_sync` / `refresh_info_sync` (#189). Account transfer-busy / not-responding badges escalate on timeout; refuse new moves for that account while busy. **Quit waits** for in-flight Archive/move/trash (same pattern as outbound send) so a mid-move exit does not drop work. **If Graph ignores cancel:** terminate that account’s **helper process** (#437); do not add a second in-process Camel session in the UI.
14. **Folder stats / count refresh (#197 / #435)** — Non-M365: Camel STATUS via the account helper (or UI mail I/O when helpers are off). **M365:** sidebar / STATUS-style counts prefer Graph HTTP on `post-graph-http`. Grow-only `folder-status` cache rules still apply.
15. **Heavy-folder header index (#208)** — Archive / All Mail / Trash / Junk open with disk/memory cache first, then a **chunked** background `continue_heavy_folder_index` (UID batches). Indexes are grow-only on disk (never replace a larger cache with a smaller partial summary). **Do not** call this indexer with server refresh from offline body sync. Opening a heavy folder: (1) index local summary immediately, (2) async `refresh_info` with `pump_until` so interactive mail I/O can run during Graph fetch (**without** sidebar `cancel_folder_refresh` aborting it — heavy index uses a separate cancellable), (3) index any new UIDs. M365 often stalls at a partial local summary; Post calls `prepare_content_refresh` and retries while the folder stays open until headers grow or several no-growth stalls. Folder::changed during that refresh must not invalidate the in-progress index. Cancel/timeout indexes the local summary but **does not** set `refresh_done` — the indexer keeps retrying while the folder stays open (full-account offline body sync stays held until that indexer finishes, #407). Interactive work pending before refresh yields with `yield_for_interactive` and leaves `refresh_done=False` (never permanently skips server refresh). Offline body sync may re-index local summary only (`allow_refresh=False`) after downsync. UI initially binds at most 500 rows (`MESSAGE_LIST_UI_BIND_CAP`) and appends more when the user scrolls near the end; status may show `Showing N of M`. Search uses the full in-memory index. **Sidebar STATUS** for heavy folders: M365 prefers Graph `totalItemCount` (#435); otherwise store FolderInfo REFRESH, persisted as a **grow-only** high-water mark (`folder-status` cache). Camel local-summary sizes must never be labeled “on server.”

## Debugging

Post always writes a rotating application log to
``$XDG_STATE_HOME/post/post.log`` (default ``~/.local/state/post/post.log``),
2 MiB × 3 backups. Default levels: **INFO+** to the file, **WARNING+** to
stderr/journal. Settings → About → **Open Log File** opens it for bug reports.

Set `POST_LOG_LEVEL=DEBUG` when launching Post to enable mail I/O task tracing
(`post.mail.io_thread`) and send-phase logs (`post.mail.eds`), and to raise both
file and stderr verbosity:

```bash
POST_LOG_LEVEL=DEBUG PYTHONPATH=src python3 -m post.main
# or
POST_LOG_LEVEL=DEBUG ./run.sh
```

Mail I/O tasks that run longer than **10s** also emit **WARNING** lines (`still running` / `slow finish`) with `func=` and elapsed time — useful for soft hangs where the UI sits on “Loading …” behind a Camel call (#197).

For folder search diagnostics (#120), also set `POST_DEBUG_SEARCH=1` (or use `POST_LOG_LEVEL=DEBUG`):

```bash
POST_DEBUG_SEARCH=1 PYTHONPATH=src python3 -m post.main
```

Search trace lines use the `post.search` logger and show load scheduling, mail-thread work, filter progress, and UI callback drops.

Without `POST_LOG_LEVEL`, stderr stays at WARNING+; the on-disk log still records INFO+.

## Unit tests

Mail-thread dispatcher behaviour is covered in `tests/test_io_thread.py`.  
`MailService` job facade and dispatch are covered in `tests/test_mail_job_api.py`, `tests/test_eds_*.py`, and `tests/test_send_background.py`.  
Epic lanes / folder lock / M365 Graph HTTP routing: `tests/test_lane_dispatch.py` (#435).  
Per-account Camel helpers / kill isolation: `tests/test_camel_helper_runtime.py` (#437).  
`tests/test_mail_threading_contract.py` forbids UI/mail modules from calling `get_mail_io_thread`.

Run the suite:

```bash
PYTHONPATH=src python3 -m pytest
```

## Manual regression matrix

Run after changes to mail threading, send, or shutdown. Check boxes when verified.

### Phase 3 helpers (#437)

| Scenario | Pass |
|----------|------|
| Two accounts: open/send on A while B does heavy Camel work | ☐ |
| Watchdog/kill helper for A; B still open/send | ☐ |
| M365 STATUS still via Graph HTTP (no Camel FIFO for that poll) | ☐ |
| Quit with multiple helpers running | ☐ |
| `POST_MAIL_CAMEL_HELPERS=0` falls back to in-process Camel | ☐ |

### Phase 2 spike (#435)

| Scenario | Account | Pass |
|----------|---------|------|
| Open Inbox + send while Archive STATUS / count poll runs | M365 | ☐ |
| Sidebar counts stay grow-only-correct after Graph HTTP STATUS | M365 | ☐ |
| Open Inbox while background sync runs (still serial Camel) | Gmail | ☐ |
| Quit during background STATUS / sync | Either | ☐ |

### Send path

| Scenario | Account | Pass |
|----------|---------|------|
| SSL :465 SMTP send | Hoststar | ☐ |
| OAuth send | Gmail | ☐ |
| Send with attachment (HTML + file) | Hoststar | ☐ |
| Close compose during send → toast, deferred close | Any | ☐ |
| Sent folder copy after send | Any | ☐ |
| Offline → outbox queue → reconnect → flush | Any | ☐ |

### Read / UI responsiveness

| Scenario | Pass |
|----------|------|
| Folder switch + scroll while sync runs | ☐ |
| Search | ☐ |
| Reply / forward (loads source message) | ☐ |
| Open attachment | ☐ |
| Move / archive + undo | ☐ |
| Sidebar folder refresh / account reload | ☐ |
| Compose address autocomplete (correspondents) | ☐ |
| Edit draft with attachments → send | ☐ |

### Lifecycle

| Scenario | Pass |
|----------|------|
| Launch → sync → send | ☐ |
| Quit during active send → relaunch (no hang) | ☐ |
| Network off → on → folders reload | ☐ |

Plain unencrypted SMTP: N/A if no test account (Hoststar covers SSL :465 + PLAIN auth).
