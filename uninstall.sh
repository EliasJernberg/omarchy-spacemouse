#!/bin/bash
# Remove what install.sh added. Your profiles.json is kept unless you pass
# --purge; the udev rule is left alone because it may belong to something else.
#
#   ./uninstall.sh
#   ./uninstall.sh --purge   also delete ~/.config/omarchy-spacemouse

set -euo pipefail

UNIT_NAME="omarchy-spacemouse.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy-spacemouse"
BIN_DIR="$HOME/.local/bin"
PLUGIN_ID="jernberg.spacemouse"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/omarchy-spacemouse"

PURGE=0
for arg in "$@"; do
  case "$arg" in
  --purge) PURGE=1 ;;
  -h | --help)
    sed -n '2,7p' "${BASH_SOURCE[0]}"
    exit 0
    ;;
  *)
    echo "uninstall.sh: unknown option '$arg'" >&2
    exit 2
    ;;
  esac
done

say() { printf '  %s\n' "$*"; }

printf '\n== Service\n'
if systemctl --user list-unit-files "$UNIT_NAME" >/dev/null 2>&1; then
  systemctl --user stop "$UNIT_NAME" 2>/dev/null || true
  systemctl --user disable "$UNIT_NAME" 2>/dev/null || true
fi
rm -f "$UNIT_DIR/$UNIT_NAME"
systemctl --user daemon-reload 2>/dev/null || true
say "stopped and removed $UNIT_NAME"

printf '\n== Command line\n'
if [[ -L $BIN_DIR/spacemouse-ctl ]]; then
  rm -f "$BIN_DIR/spacemouse-ctl"
  say "removed $BIN_DIR/spacemouse-ctl"
else
  say "no symlink at $BIN_DIR/spacemouse-ctl"
fi

printf '\n== Bar widget\n'
if ! command -v omarchy-plugin-list >/dev/null; then
  say "omarchy plugin commands not found, nothing to remove"
elif ! omarchy-plugin-list --json 2>/dev/null | grep -q "\"$PLUGIN_ID\""; then
  say "plugin '$PLUGIN_ID' was not installed"
elif omarchy plugin remove "$PLUGIN_ID" --yes >/dev/null 2>&1; then
  say "removed the '$PLUGIN_ID' plugin"
else
  say "could NOT remove '$PLUGIN_ID'. Remove it by hand:"
  say "  omarchy plugin remove $PLUGIN_ID --yes"
fi

printf '\n== Leftovers\n'
rm -rf "$RUNTIME_DIR"
say "cleared $RUNTIME_DIR"
if ((PURGE)); then
  rm -rf "$CONFIG_DIR"
  say "deleted $CONFIG_DIR"
else
  say "kept your profiles in $CONFIG_DIR (--purge deletes them)"
fi
say "the udev rule for /dev/uinput, if you added one, was left in place"
