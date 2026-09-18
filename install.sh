#!/bin/bash
# Install omarchy-spacemouse for the current user. Safe to run again: every
# step either matches what is already there or replaces it.
#
#   ./install.sh              install and start
#   ./install.sh --no-plugin  skip the bar widget, install only the daemon
#   ./install.sh --no-start   install without starting the service

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON="$REPO/daemon/spacemoused.py"
CTL="$REPO/bin/spacemouse-ctl"
UNIT_NAME="omarchy-spacemouse.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy-spacemouse"
BIN_DIR="$HOME/.local/bin"
PLUGIN_ID="jernberg.spacemouse"
PLUGIN_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/$PLUGIN_ID"
UDEV_RULE="/etc/udev/rules.d/99-omarchy-spacemouse-uinput.rules"

WITH_PLUGIN=1
START=1
for arg in "$@"; do
  case "$arg" in
  --no-plugin) WITH_PLUGIN=0 ;;
  --no-start) START=0 ;;
  -h | --help)
    sed -n '2,9p' "${BASH_SOURCE[0]}"
    exit 0
    ;;
  *)
    echo "install.sh: unknown option '$arg'" >&2
    exit 2
    ;;
  esac
done

say() { printf '  %s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }

step "Checking what is here"
command -v python3 >/dev/null || {
  echo "python3 is required" >&2
  exit 1
}
say "python3            $(python3 --version 2>&1)"
if systemctl is-active --quiet spacenavd.service 2>/dev/null; then
  say "spacenavd          running"
else
  say "spacenavd          NOT running. Install it and enable the service:"
  say "                   omarchy pkg add spacenavd && sudo systemctl enable --now spacenavd"
fi
if [[ -w /dev/uinput ]]; then
  say "/dev/uinput        writable"
else
  say "/dev/uinput        NOT writable (mouse and keys profiles will stay idle)"
fi

step "Profiles"
mkdir -p "$CONFIG_DIR"
if [[ -f "$CONFIG_DIR/profiles.json" ]]; then
  say "kept your $CONFIG_DIR/profiles.json"
else
  cp "$REPO/profiles.default.json" "$CONFIG_DIR/profiles.json"
  say "wrote $CONFIG_DIR/profiles.json from the shipped defaults"
fi

step "spacemouse-ctl"
mkdir -p "$BIN_DIR"
ln -sf "$CTL" "$BIN_DIR/spacemouse-ctl"
say "linked $BIN_DIR/spacemouse-ctl -> $CTL"
case ":$PATH:" in
*":$BIN_DIR:"*) ;;
*) say "note: $BIN_DIR is not on your PATH" ;;
esac

step "systemd user service"
mkdir -p "$UNIT_DIR"
sed "s|@DAEMON@|$DAEMON|g" "$REPO/systemd/$UNIT_NAME" >"$UNIT_DIR/$UNIT_NAME"
systemctl --user daemon-reload
systemctl --user enable "$UNIT_NAME" >/dev/null
say "installed $UNIT_DIR/$UNIT_NAME"
if ((START)); then
  systemctl --user restart "$UNIT_NAME"
  sleep 1
  if systemctl --user is-active --quiet "$UNIT_NAME"; then
    say "service is running"
  else
    say "service did not start, see: journalctl --user -u $UNIT_NAME -n 40"
  fi
else
  say "not started (--no-start). Start it with: systemctl --user start $UNIT_NAME"
fi

if ((WITH_PLUGIN)); then
  step "Bar widget"
  if ! command -v omarchy-plugin-list >/dev/null; then
    say "omarchy plugin commands not found, skipping the bar widget"
  elif omarchy-plugin-list --json 2>/dev/null | grep -q "\"$PLUGIN_ID\""; then
    say "plugin '$PLUGIN_ID' is already installed in $PLUGIN_DIR"
    say "update it with: omarchy plugin update $PLUGIN_ID"
  else
    # `omarchy plugin add` clones a git repo, so a local checkout works as a
    # source as well as a URL. Prefer the published remote when there is one,
    # so `omarchy plugin update` has something to fast-forward from.
    source_url="$(git -C "$REPO" remote get-url origin 2>/dev/null || true)"
    [[ -n $source_url ]] || source_url="$REPO"
    if omarchy plugin add "$source_url" --enable --yes; then
      say "added and enabled '$PLUGIN_ID' from $source_url"
    else
      say "could not add the plugin from $source_url"
      say "add it by hand with: omarchy plugin add $source_url --enable --yes"
    fi
  fi
fi

if [[ ! -w /dev/uinput ]]; then
  step "One step is left, and it needs root"
  cat <<RULE
  Mouse and keyboard emulation writes to /dev/uinput, which is root-only until
  a udev rule opens it to your group. Run these three lines, then log out and
  back in (the group membership is picked up at login):

    echo 'KERNEL=="uinput", MODE="0660", GROUP="uucp", OPTIONS+="static_node=uinput"' | sudo tee $UDEV_RULE
    sudo udevadm control --reload-rules && sudo udevadm trigger /dev/uinput
    sudo usermod -aG uucp "$USER"     # only if 'id -nG' does not list uucp already

  Until then the daemon runs, follows focus and keeps the native profiles
  working; only the emulated profiles stay idle.

  Optional, and only for tests/live_check.py, which reads the device back:

    echo 'SUBSYSTEM=="input", ATTRS{name}=="Omarchy SpaceMouse", MODE="0660", GROUP="uucp"' | sudo tee -a $UDEV_RULE
    sudo udevadm control --reload-rules
RULE
fi

step "Done"
say "status:   spacemouse-ctl status"
say "profiles: $CONFIG_DIR/profiles.json"
say "logs:     journalctl --user -u $UNIT_NAME -f"
