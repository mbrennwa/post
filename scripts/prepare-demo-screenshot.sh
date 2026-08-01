#!/usr/bin/env bash
# Prepare an isolated Post session with fake local mail for landing-page screenshots.
#
# Plain HOME=/tmp/post-demo is not enough: a private D-Bus session still shares
# XDG_RUNTIME_DIR (/run/user/UID), so GOA/gnome-keyring reinject your real
# accounts into the demo Evolution sources. This script uses a private runtime
# dir (with Wayland sockets symlinked) so GOA stays empty.
#
# Usage:
#   ./scripts/prepare-demo-screenshot.sh          # set up + launch Post
#   ./scripts/prepare-demo-screenshot.sh --setup  # set up only
#   ./scripts/prepare-demo-screenshot.sh --probe  # set up + list accounts via probe
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEMO_HOME="${POST_DEMO_HOME:-/tmp/post-demo}"
SOURCES="$DEMO_HOME/.config/evolution/sources"
MODE="${1:-}"
REAL_RUNTIME="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

write_msg() {
  local maildir="$1"
  local unique="$2"
  local subject="$3"
  local from_name="$4"
  local from_addr="$5"
  local to_name="$6"
  local to_addr="$7"
  local body="$8"
  mkdir -p "$maildir/tmp" "$maildir/new" "$maildir/cur"
  local path="$maildir/new/$unique"
  cat >"$path" <<EOF
Return-Path: <$from_addr>
Delivered-To: $to_addr
From: $from_name <$from_addr>
To: $to_name <$to_addr>
Subject: $subject
Date: Sat, 1 Aug 2026 10:00:00 +0200
Message-ID: <$unique@example.com>
MIME-Version: 1.0
Content-Type: text/plain; charset=utf-8

$body
EOF
}

write_account() {
  local uid="$1"
  local display="$2"
  local mail_path="$3"
  local from_name="$4"
  local from_addr="$5"
  local identity_uid="${uid}-identity"
  local transport_uid="${uid}-sendmail"
  # Match Post's built-in local-mail UIDs so repair/ensure paths work.
  if [[ "$uid" == "post-local-mail" ]]; then
    identity_uid="post-local-mail-identity"
    transport_uid="post-local-sendmail"
  fi
  mkdir -p "$mail_path/tmp" "$mail_path/new" "$mail_path/cur"

  cat >"$SOURCES/${uid}.source" <<EOF
[Data Source]
DisplayName=$display
Enabled=true
Parent=

[Mail Account]
BackendName=maildir
IdentityUid=$identity_uid
ArchiveFolder=
NeedsInitialSetup=false
MarkSeen=inconsistent
MarkSeenTimeout=1500
Builtin=false

[Maildir Backend]
Path=$mail_path
FilterInbox=true
StoreChangesInterval=3
FilterAll=false
FilterJunk=true

[Refresh]
Enabled=false
EOF

  cat >"$SOURCES/${identity_uid}.source" <<EOF
[Data Source]
DisplayName=$from_name
Enabled=true
Parent=$uid

[Mail Identity]
Address=$from_addr
Aliases=
Name=$from_name
Organization=
ReplyTo=
SignatureUid=none

[Mail Submission]
TransportUid=$transport_uid
EOF

  cat >"$SOURCES/${transport_uid}.source" <<EOF
[Data Source]
DisplayName=Local SMTP
Enabled=true
Parent=$uid

[Mail Transport]
BackendName=smtp

[Authentication]
Host=127.0.0.1
Port=25
User=
Method=
RememberPassword=false
ProxyUid=system-proxy

[Security]
Method=none
EOF
}

setup_demo() {
  rm -rf "$DEMO_HOME"
  mkdir -p "$SOURCES" "$DEMO_HOME/.local/share" "$DEMO_HOME/.cache"
  # Empty GOA config so even a leaky daemon has nothing to load from $HOME.
  mkdir -p "$DEMO_HOME/.config/goa-1.0"
  : >"$DEMO_HOME/.config/goa-1.0/accounts.conf"

  local personal="$DEMO_HOME/maildir-personal"
  local work="$DEMO_HOME/maildir-work"
  write_account post-local-mail "demo@example.com" "$personal" "Demo User" "demo@example.com"
  write_account post-demo-work "work@example.org" "$work" "Demo Work" "work@example.org"

  write_msg "$personal" 1754035200000001.1.demo "Welcome to Post" "Alex Rivera" "alex@example.com" \
    "Demo User" "demo@example.com" \
    "Thanks for trying Post — a focused mail app for GNOME."
  write_msg "$personal" 1754035260000002.2.demo "Project status update" "Sam Chen" "sam@example.org" \
    "Demo User" "demo@example.com" \
    "The release checklist is ready. Screenshot copy uses example.com only."
  write_msg "$personal" 1754035320000003.3.demo "Free for lunch tomorrow?" "Jordan Lee" "jordan@example.com" \
    "Demo User" "demo@example.com" \
    "Hi!

Are you free for lunch tomorrow around noon? I was thinking the usual place near the office — happy to do 12:00 or 12:30 if that works better for you.

Let me know either way.

Thanks,
Jordan"
  write_msg "$personal" 1754035380000004.4.demo "Re: packaging the .deb" "Pat Okonkwo" "pat@example.org" \
    "Demo User" "demo@example.com" \
    "Looks good on Debian 12 and Ubuntu 24.04. Ship it."

  write_msg "$work" 1754035400000005.5.demo "Quarterly report draft" "Riley Nguyen" "riley@example.org" \
    "Demo Work" "work@example.org" \
    "Attached is the first draft of the quarterly report. Comments welcome before Friday."
  write_msg "$work" 1754035460000006.6.demo "Server maintenance window" "Ops Team" "ops@example.com" \
    "Demo Work" "work@example.org" \
    "We will apply security updates this Sunday from 02:00 to 04:00 UTC. Expect brief downtime."

  chmod 600 "$SOURCES"/*.source
  local count
  count="$(find "$personal/new" "$work/new" -type f | wc -l)"
  echo "Demo home ready: $DEMO_HOME"
  echo "Maildirs: personal + work ($count messages)"
}

prepare_runtime() {
  DEMO_RUNTIME="$(mktemp -d /tmp/post-demo-runtime-XXXXXX)"
  # Wayland (and optional PipeWire) from the real session; no keyring/bus/goa.
  for name in wayland-0 wayland-0.lock; do
    if [[ -e "$REAL_RUNTIME/$name" ]]; then
      ln -s "$REAL_RUNTIME/$name" "$DEMO_RUNTIME/$name"
    fi
  done
  if [[ -S "$REAL_RUNTIME/pipewire-0" ]]; then
    ln -s "$REAL_RUNTIME/pipewire-0" "$DEMO_RUNTIME/pipewire-0"
  fi
  echo "$DEMO_RUNTIME"
}

run_in_demo_session() {
  local -a cmd=("$@")
  local cmd_str=""
  local part
  for part in "${cmd[@]}"; do
    cmd_str+="$(printf '%q' "$part") "
  done

  local demo_runtime
  demo_runtime="$(prepare_runtime)"

  # Mask GOA before dbus-daemon starts — it reads XDG_DATA_HOME at startup.
  mkdir -p "$DEMO_HOME/.local/share/dbus-1/services"
  for svc in org.gnome.OnlineAccounts org.gnome.Identity; do
    cat >"$DEMO_HOME/.local/share/dbus-1/services/${svc}.service" <<EOF
[D-BUS Service]
Name=$svc
Exec=/bin/false
EOF
  done

  # Env must be set for dbus-run-session itself (not only the child), or the
  # session dbus-daemon still activates /usr GOA and reinjects real accounts.
  # bash -c (not -lc): avoid login profiles. Unique app-id on launch avoids
  # attaching to a real Post instance if bus isolation ever fails.
  env \
    HOME="$DEMO_HOME" \
    XDG_CONFIG_HOME="$DEMO_HOME/.config" \
    XDG_DATA_HOME="$DEMO_HOME/.local/share" \
    XDG_CACHE_HOME="$DEMO_HOME/.cache" \
    XDG_STATE_HOME="$DEMO_HOME/.local/state" \
    XDG_RUNTIME_DIR="$demo_runtime" \
    ADW_DEBUG_COLOR_SCHEME=prefer-dark \
    POST_DEMO=1 \
    GNOME_KEYRING_CONTROL= \
    SSH_AUTH_SOCK= \
    GIO_LAUNCHED_DESKTOP_FILE= \
    GIO_LAUNCHED_DESKTOP_FILE_PID= \
    dbus-run-session -- bash -c "
      set -euo pipefail
      cleanup() {
        kill \$registry_pid 2>/dev/null || true
        rm -rf $(printf '%q' "$demo_runtime") 2>/dev/null || true
      }
      trap cleanup EXIT
      /usr/libexec/evolution-source-registry &
      registry_pid=\$!
      for _ in \$(seq 1 50); do
        if busctl --user list 2>/dev/null | grep -q 'org.gnome.evolution.dataserver.Sources'; then
          break
        fi
        sleep 0.1
      done
      find \"\$HOME/.config/evolution/sources\" -maxdepth 1 -type f -name '*.source' \
        ! -name 'post-local-*' ! -name 'post-demo-*' -delete
      rm -rf \"\$HOME/.cache/evolution/sources\"
      mkdir -p \"\$HOME/.cache/evolution/sources\"
      if grep -El 'gmail\\.com|brennwald|gasometrix' \"\$HOME/.config/evolution/sources/\"*.source >/dev/null 2>&1; then
        echo \"ERROR: real account sources leaked into demo home; aborting.\" >&2
        ls \"\$HOME/.config/evolution/sources/\" >&2
        exit 1
      fi
      if busctl --user status org.gnome.OnlineAccounts >/dev/null 2>&1; then
        echo \"ERROR: GOA is running in the demo session; aborting.\" >&2
        exit 1
      fi
      echo \"Demo sources: \$(ls \"\$HOME/.config/evolution/sources\")\"
      cd $(printf '%q' "$ROOT")
      echo \"Demo bus=\$DBUS_SESSION_BUS_ADDRESS\"
      echo \"Demo HOME=\$HOME\"
      echo \"Demo XDG_RUNTIME_DIR=\$XDG_RUNTIME_DIR\"
      $cmd_str
    "
}


setup_demo

case "$MODE" in
  --setup)
    exit 0
    ;;
  --probe)
    run_in_demo_session .venv/bin/python -m post.probe
    ;;
  --launch|"")
    echo "Launching Post in isolated demo session…"
    echo "Take a screenshot, then close the window."
    # Skip run.sh (desktop-file install). Unique app id avoids attaching to
    # a real Post instance if the bus isolation ever fails.
    run_in_demo_session .venv/bin/post --gapplication-app-id=io.github.mbrennwa.Post.demo
    ;;
  *)
    echo "Unknown option: $MODE" >&2
    echo "Usage: $0 [--setup|--probe|--launch]" >&2
    exit 2
    ;;
esac
