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
4. **Password / OAuth prompts** — use `GLib.idle_add` to show dialogs on the GTK thread; mail thread waits on the result.
5. **Outbound send** — compose persists to outbox first, then delivers via Camel `transport.send_to_sync` on the mail I/O thread. No `smtplib` send path.
6. **Offline body download** — `OfflineBodySyncCoordinator` runs `downsync_sync` on the mail I/O thread only. See [offline-body-cache.md](offline-body-cache.md).
7. **Search** — all folder search runs on the mail I/O thread via `query_to_sexp()` and `camel_folder_search_by_expression()` (libcamel). See [offline-body-cache.md](offline-body-cache.md).

## Debugging

Set `POST_LOG_LEVEL=DEBUG` when launching Post to enable mail I/O task tracing (`post.mail.io_thread`) and send-phase logs (`post.mail.eds`):

```bash
POST_LOG_LEVEL=DEBUG PYTHONPATH=src python3 -m post.main
# or
POST_LOG_LEVEL=DEBUG ./run.sh
```

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
