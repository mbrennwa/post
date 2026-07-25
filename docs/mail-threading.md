# Mail I/O threading

Post runs blocking Camel / Evolution Data Server (EDS) work on a **single dedicated mail I/O thread** (`post-mail-io`) so the GTK main loop never executes synchronous mail I/O.

## Architecture

```
┌─────────────────────┐    submit / run_sync     ┌──────────────────────────┐
│  GTK main thread    │ ───────────────────────► │  post-mail-io thread     │
│  (UI only)          │ ◄── GLib.idle_add ────── │  (Camel Session owner) │
└─────────────────────┘                          └──────────────────────────┘
```

- **`post.mail.io_thread`** — serial queue + private `GMainContext`; bootstraps `Camel.init` once.
- **`MailService`** (`post.mail.eds`) — facade; public methods dispatch blocking work via `run_on_mail_thread()` or `get_mail_io_thread().run_sync()` / `submit()`.
- **UI modules** (`window.py`, `sidebar.py`, `compose_window.py`, `sync_watcher.py`) — call `get_mail_io_thread().submit(worker)` for background mail work; update GTK via `GLib.idle_add` only.

## Rules for contributors

1. **Never call `MailIoThread.run_sync()` from the GTK thread** — it blocks the UI. Use `submit()` + `idle_add` from UI code.
2. **Never call `Camel.*_sync` directly from UI or ad-hoc worker threads** — go through `MailService` or `run_on_mail_thread()`.
3. **One `MailSession` per process** — owned on the mail I/O thread (`MailService._session`, `_stores`, `_transports`). Do not reintroduce per-thread worker sessions.
4. **Password / OAuth prompts** — use `GLib.idle_add` to show dialogs on the GTK thread; mail thread waits on the result. Do **not** call GOA `EnsureCredentials` synchronously from the GTK thread (compose must not preflight on the UI thread; see #156).
5. **Outbound send** — compose persists to outbox first, then delivers via Camel `transport.send_to_sync` on the mail I/O thread. No `smtplib` send path. Send and draft save use a **finite** cancellable timeout; draft failures/timeouts fall back to the local draft queue.
6. **Offline body download** — `OfflineBodySyncCoordinator` runs `downsync_sync` on the mail I/O thread only. See [offline-body-cache.md](offline-body-cache.md).
7. **Sync watcher setup** — `MailSyncWatcher` store/folder signal wiring runs as **background** mail-I/O work so folder search can preempt it. Preempt also cancels in-flight sidebar folder lists.
8. **Search** — interactive mail-I/O work: filter the in-memory folder index (`filter_messages_by_query`), loading cached MIME for body terms. Cancellable; preempts offline downsync. See [offline-body-cache.md](offline-body-cache.md).
9. **Correspondents / autocomplete** — build from cached folder tree + folder indexes only; never connect a store just for compose autocomplete (#156).
10. **GOA EnsureCredentials** — D-Bus call uses a finite timeout (not `-1`) so a wedged Online Accounts account cannot pin `post-mail-io` forever.
11. **Per-account Take offline** — first connect / `set_online_sync` must honor `get_account_user_online`, not only global network availability.
12. **Folder transfer / Archive (#189)** — `transfer_messages_to_sync` uses a finite `Gio.Cancellable` timeout; soft-succeed when source UIDs are already gone. After move (including soft-succeed), prune Camel `FolderSummary` UIDs locally (Evolution-style) and update Post’s folder-index cache — do **not** block UI completion on Graph `refresh_info_sync`. For `microsoft365` / `ews`, skip post-transfer `synchronize_sync` / `refresh_info_sync`. Account transfer-busy / not-responding badges escalate on timeout; refuse new moves for that account while busy. **Quit waits** for in-flight Archive/move/trash (same pattern as outbound send) so a mid-move exit does not drop work. **Residual:** if the Graph provider ignores cancel, `post-mail-io` stays pinned until the native call returns or the wait times out; true kill-isolation needs a helper process (follow-up), not a second in-process Camel session.

## Debugging

Set `POST_LOG_LEVEL=DEBUG` when launching Post to enable mail I/O task tracing (`post.mail.io_thread`) and send-phase logs (`post.mail.eds`):

```bash
POST_LOG_LEVEL=DEBUG PYTHONPATH=src python3 -m post.main
# or
POST_LOG_LEVEL=DEBUG ./run.sh
```

For folder search diagnostics (#120), also set `POST_DEBUG_SEARCH=1` (or use `POST_LOG_LEVEL=DEBUG`):

```bash
POST_DEBUG_SEARCH=1 PYTHONPATH=src python3 -m post.main
```

Search trace lines use the `post.search` logger and show load scheduling, mail-thread work, filter progress, and UI callback drops.

Default log level is quiet (no console handler unless `POST_LOG_LEVEL` is set).

## Unit tests

Mail-thread dispatcher behaviour is covered in `tests/test_io_thread.py`.  
`MailService` dispatch to the mail thread is covered in `tests/test_eds_*.py` and `tests/test_send_background.py`.

Run the suite:

```bash
PYTHONPATH=src python3 -m pytest
```

## Manual regression matrix

Run after changes to mail threading, send, or shutdown. Check boxes when verified.

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
