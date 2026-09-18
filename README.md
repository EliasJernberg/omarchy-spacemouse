# omarchy-spacemouse

A 3Dconnexion SpaceMouse that works in **every** application on Omarchy, not
just the handful that ship spacenavd support.

On Windows, 3DxWare keeps a profile per application: it talks the native
protocol to the applications that understand it, and for everything else it
synthesizes mouse and keyboard input, so the puck still orbits, pans and zooms.
This is that model on Linux: a small daemon reads the puck from spacenavd,
watches which window Hyprland has focused, and picks the profile for that
window class.

```
  spacenavd  ──►  spacemoused  ──►  native   nothing is emitted, the app reads
  /run/spnav.sock      ▲                      spacenavd itself (KiCad, FreeCAD,
                       │                      Blender, Fusion through Bifrost)
  Hyprland  ───────────┘           ──►  off      the puck stays quiet here
  .socket2.sock                    ──►  mouse    a virtual uinput mouse and
  (focused window class)                         keyboard: drag gestures + wheel
                                   ──►  keys     axes bound to keys, with repeat
```

Everything is Python standard library. No build step, no virtualenv, no
dependency beyond `python3`, `spacenavd` and a writable `/dev/uinput`.

---

## Install

```bash
git clone https://github.com/EliasJernberg/omarchy-spacemouse.git
cd omarchy-spacemouse
./install.sh
```

The installer is idempotent, and it does five things:

1. copies `profiles.default.json` to `~/.config/omarchy-spacemouse/profiles.json`
   (an existing file is never overwritten),
2. links `bin/spacemouse-ctl` into `~/.local/bin`,
3. installs and starts the systemd user service `omarchy-spacemouse.service`,
4. registers the bar widget with `omarchy plugin add ... --enable`,
5. prints the udev rule below if `/dev/uinput` is not writable.

`./install.sh --no-plugin` skips the bar widget, `--no-start` installs without
starting. `./uninstall.sh` removes all of it (`--purge` also deletes your
profiles).

### The one step that needs root

The emulated profiles write to `/dev/uinput`, which is `0600 root:root` on a
stock Arch install. Open it to your own group once:

```bash
echo 'KERNEL=="uinput", MODE="0660", GROUP="uucp", OPTIONS+="static_node=uinput"' \
  | sudo tee /etc/udev/rules.d/99-omarchy-spacemouse-uinput.rules
sudo udevadm control --reload-rules && sudo udevadm trigger /dev/uinput
id -nG | grep -q uucp || sudo usermod -aG uucp "$USER"   # then log out and back in
```

`uucp` is the Arch group that owns serial and input style devices; on Debian or
Ubuntu the same rule is usually written with `GROUP="input"`. Until the rule is
in place the daemon still runs, still follows focus and still keeps the native
applications working, and `spacemouse-ctl status` reports `uinput denied`.

### Requirements

- `spacenavd` running (`omarchy pkg add spacenavd && sudo systemctl enable --now spacenavd`)
- Hyprland, for the focus following. Without it the daemon falls back to the
  `default` profile and keeps working.
- `python3`

---

## Profiles

`~/.config/omarchy-spacemouse/profiles.json` is read at startup and again
whenever the file's timestamp changes, so an edit takes effect within a second.
A file that fails to parse is refused and the previous set stays live, with the
reason in `spacemouse-ctl status`.

```json
{
  "version": 1,
  "settings": { "...": "see Tuning below" },
  "profiles": [
    { "name": "cad-native", "match": "^(kicad|freecad|blender)", "type": "native" },
    { "name": "default",    "match": null,                       "type": "mouse", "gestures": {} }
  ]
}
```

`match` is a regular expression, case insensitive, searched against the focused
window's class. The **first** profile whose pattern matches wins, so put the
specific ones first. `"match": null` (or `"*"`) makes a profile the fallback for
everything the list did not claim; there is always exactly one, and one is added
for you if the file forgets it.

To see a window's class: `hyprctl activewindow -j | jq -r .class`.

### The four profile types

| type     | what happens                                                                |
|----------|-----------------------------------------------------------------------------|
| `native` | nothing is emitted. The application is a spacenavd client and reads the puck itself. |
| `off`    | nothing is emitted, and nothing is expected to. The puck is idle here.       |
| `mouse`  | gesture groups: hold a button, move the pointer, turn the wheel.             |
| `keys`   | axis directions bound to keys, tapped at a repeat rate or held down.         |

### What ships by default

| profile           | matches                                        | type   |
|-------------------|------------------------------------------------|--------|
| `cad-native`      | KiCad, FreeCAD, Blender                        | native |
| `fusion-bifrost`  | `fusion360.exe`                                | native |
| `desktop-off`     | terminals, chat clients, Spotify, Obsidian     | off    |
| `browser-threejs` | Opera, Chromium, Chrome, Brave, Firefox, Zen   | mouse  |
| `default`         | everything else                                | mouse  |

`fusion360.exe` is native because Fusion under Wine is driven by
[Bifrost](https://github.com/EliasJernberg/bifrost), which speaks to spacenavd
directly and hands the axes to the Fusion add-in. Two daemons emitting into the
same window would fight.

`desktop-off` is a safety rail rather than a rule: the `default` profile holds
the **middle button** while orbiting, and in a terminal a middle click pastes
the primary selection. Delete that profile if you want the CAD convention
everywhere, or add to it whenever a new application surprises you.

---

## Gestures

A `mouse` profile is a set of named gesture groups. Each group reads one or
more axes and either drags (holding a button and moving the pointer) or turns
the wheel.

```json
"gestures": {
  "orbit": {
    "enabled": true,
    "hold": ["middle"],
    "speed": 1.0,
    "axes": { "rx": { "to": "dy", "gain": 1.0 }, "ry": { "to": "dx", "gain": -1.0 } }
  },
  "pan":  { "hold": ["shift", "middle"],
            "axes": { "x": { "to": "dx" }, "z": { "to": "dy", "gain": -1.0 } } },
  "zoom": { "mode": "wheel", "axes": { "y": { "to": "wheel" } } }
}
```

- `hold` is pressed while the gesture is active, in the order written
  (modifiers first). Names: `left`, `right`, `middle`, `side`, `extra`, plus
  `shift`, `ctrl`, `alt`, `super` and any `KEY_*` from the Linux keycode table.
- `axes` maps a puck axis to `dx`, `dy`, `wheel` or `hwheel`, through a `gain`
  that also carries the sign. Flip a `gain` to reverse a direction.
- `mode` is `drag` (default) or `wheel`.
- `speed` scales this group only; `pointer_speed` and `wheel_speed` in
  `settings` scale every group.

### How one gesture at a time is chosen

Each tick every group gets a magnitude from its own axes. The largest one wins
and nothing else is allowed to press a button, which is what stops orbit and
pan from ever holding their buttons at the same time. A gesture that is already
running keeps its buttons through a brief pause, and only gives way to a
competitor that is `dominance_ratio` (1.35 by default) stronger. When the puck
has been back in the deadzone for `idle_release_ms` (150 ms), the buttons are
released.

Every profile change, every `disable` and every shutdown releases whatever is
held, immediately. A stuck middle button would make the desktop unusable, so
that path is covered by its own tests.

### Axis names

Measured on a SpaceMouse Pro, which is what the shipped defaults assume:

| axis | motion                    | positive direction        |
|------|---------------------------|---------------------------|
| `x`  | slide sideways            | right                     |
| `y`  | lift or press             | lift                      |
| `z`  | slide forward or back     | away from you             |
| `rx` | tilt (pitch)              | front edge **up**         |
| `ry` | twist (yaw)               | counterclockwise from above |
| `rz` | roll (tilt sideways)      | not measured here, flip the gain if it feels backwards |

A full deflection reads about 350 counts. At rest the axes wander up to 20
counts, and a hard push on one axis bleeds up to 50 onto its neighbours, which
is what the deadzone and the dominant-group rule are sized for.

### Puck buttons

```json
"fit_key": "KEY_HOME",
"buttons": {
  "5": { "action": "fit" },
  "1": { "action": "key",  "key": "ctrl+shift+f" },
  "2": { "action": "exec", "command": "omarchy-notification-send 'puck says hello'" }
}
```

`fit` taps the profile's own `fit_key`, so the same physical button does the
right thing in every application. Button 5 is FIT on the SpaceMouse Pro; use
`spacemoused.py --dump` to find the number of any other button.

---

## Adding an application

1. Focus the window and read its class:
   `hyprctl activewindow -j | jq -r .class`
2. Add a profile **above** `default` in `~/.config/omarchy-spacemouse/profiles.json`:

```json
{
  "name": "meshlab",
  "description": "Trackball convention: rotate with the left button",
  "match": "^meshlab$",
  "type": "mouse",
  "fit_key": "KEY_HOME",
  "gestures": {
    "orbit": { "hold": ["left"],
               "axes": { "rx": { "to": "dy" }, "ry": { "to": "dx", "gain": -1.0 } } },
    "pan":   { "hold": ["ctrl", "left"],
               "axes": { "x": { "to": "dx" }, "z": { "to": "dy", "gain": -1.0 } } },
    "zoom":  { "mode": "wheel", "axes": { "y": { "to": "wheel" } } }
  },
  "buttons": { "5": { "action": "fit" } }
}
```

3. Save. The daemon reloads within a second; `spacemouse-ctl profiles` lists
   what it now knows, and `spacemouse-ctl status` shows which one the focused
   window is getting.

If the application is a spacenavd client, give it `"type": "native"` instead
and let it read the puck itself. That is always the better path when it exists:
no emulation, six degrees of freedom at once.

### A `keys` profile

For an application that has no 3D view but does have shortcuts, for example a
slide deck or an image viewer:

```json
{
  "name": "slides",
  "match": "^impress$",
  "type": "keys",
  "bindings": [
    { "axis": "z", "dir": "+", "key": "KEY_RIGHT", "threshold": 0.35, "repeat_hz": 4 },
    { "axis": "z", "dir": "-", "key": "KEY_LEFT",  "threshold": 0.35, "repeat_hz": 4 },
    { "axis": "x", "dir": "+", "key": "shift",     "hold": true }
  ]
}
```

`repeat_hz` taps the key while the axis is held past `threshold`;
`"hold": true` presses it once and releases it when the axis returns.

---

## Tuning

Everything in `settings` applies to every profile:

| key                  | default | what it does                                                     |
|----------------------|---------|------------------------------------------------------------------|
| `deadzone`           | 30      | counts ignored around centre. Raise it if the pointer drifts.     |
| `full_scale`         | 350     | counts that count as full deflection.                             |
| `curve`              | 1.5     | response exponent. Higher means gentler near centre.              |
| `sensitivity`        | 1.0     | overall multiplier.                                               |
| `axis_gain`          | all 1.0 | per-axis multiplier.                                              |
| `axis_invert`        | all false | per-axis direction flip.                                        |
| `pointer_speed`      | 900     | pixels per second at full deflection.                             |
| `wheel_speed`        | 3.0     | wheel detents per second at full deflection.                      |
| `wheel_hi_res`       | true    | emit `REL_WHEEL_HI_RES` for smooth scrolling.                     |
| `activate_threshold` | 0.12    | how hard you have to push to start a gesture.                     |
| `release_threshold`  | 0.05    | below this the gesture stops moving.                              |
| `idle_release_ms`    | 150     | how long it keeps the buttons after you let go.                   |
| `dominance_ratio`    | 1.35    | how much stronger a competing group must be to take over.         |
| `tick_hz`            | 60      | emit rate.                                                        |

A practical order to tune in:

1. **Drift at rest**: watch `spacemoused.py --dump` with your hand off the puck.
   Set `deadzone` just above the largest number you see.
2. **Too fast or too slow**: `pointer_speed` for the drags, `wheel_speed` for
   the zoom. Both are "per second at full deflection", so they are easy to
   reason about.
3. **Twitchy near centre**: raise `curve` to 1.8 or 2.0.
4. **A gesture keeps flipping to another**: raise `dominance_ratio`.
5. **The gesture drops out mid-move**: raise `idle_release_ms`.
6. **Wrong direction**: flip the `gain` sign on that axis in the gesture, or
   set `axis_invert` if you want it flipped everywhere.

---

## The bar widget

The Quickshell plugin (`jernberg.spacemouse`) shows the icon and the active
profile name.

- **left click** enable or disable
- **middle click** back to following the focused window
- **right click** the panel: window class, profile, gesture, spacenavd and
  uinput health, the profile list with a manual override, and buttons for
  enable/disable and reload

It reads `$XDG_RUNTIME_DIR/omarchy-spacemouse/status.json` and drives the
daemon through `spacemouse-ctl`, so the widget and the command line are the
same path into the daemon. The icon is dimmed whenever nothing is being
emitted, which includes the native profiles.

---

## spacemouse-ctl

```
spacemouse-ctl status [--json]     what the daemon is doing right now
spacemouse-ctl profiles [--json]   the loaded profiles, active one marked
spacemouse-ctl enable | disable | toggle
spacemouse-ctl profile <name>      pin a profile, ignoring focus
spacemouse-ctl auto                back to following the focused window
spacemouse-ctl reload              re-read profiles.json
```

It talks to `$XDG_RUNTIME_DIR/omarchy-spacemouse/control.sock`. With the daemon
down, `status` falls back to the last status file and says so.

---

## Troubleshooting

**Nothing happens in any application.**
`spacemouse-ctl status`. If `uinput` says `denied`, the udev rule above is
missing or you have not logged back in since joining the group. If `spacenavd`
says `disconnected`, check `systemctl status spacenavd`.

**Nothing happens in one application.**
Its profile is probably `native` or `off`. `spacemouse-ctl status` names the
profile the focused window is getting. If it is native, the application should
be reading spacenavd itself; check that it actually does.

**A button is stuck down.**
`spacemouse-ctl disable` releases everything immediately, and so does stopping
the service. Please open an issue if you find a way to reach that state, since
it is the one failure the design is built to prevent.

**The pointer drifts on its own.**
Deadzone too low. See Tuning, step 1. Recentre the puck (`spacenavd` does that
at startup) before measuring.

**The profile does not change when I switch windows.**
`spacemouse-ctl status` shows the window class the daemon sees. If it is empty,
the Hyprland event socket is not connected, and the daemon falls back to the
`default` profile. The daemon reconnects on its own every two seconds.

**Middle-clicking pastes in my terminal.**
The terminal is getting a `mouse` profile. Add its class to `desktop-off`.

**An empty workspace gets the `default` mouse profile.**
With no window focused there is no class to match, so the fallback profile
applies. Give the fallback `"type": "off"` if you would rather the puck were
quiet on an empty desktop.

**Logs**: `journalctl --user -u omarchy-spacemouse -f`

---

## Working on it

```bash
python3 tests/run.py            # 119 tests, standard library only
python3 tests/run.py -v
python3 tests/run.py gesture    # just tests/test_gestures.py
```

The daemon has three modes that make it testable without hardware and without
touching the kernel:

```bash
# decode a recorded capture, or watch the puck live
python3 daemon/spacemoused.py --dump tests/fixtures/hardware/calibration_capture.bin
python3 daemon/spacemoused.py --dump

# run the whole pipeline against a recording, printing what it would emit
python3 daemon/spacemoused.py --dry-run --no-focus --profile default \
  --replay tests/fixtures/hardware/calibration_capture.bin --replay-speed 8
```

`--dry-run` swaps the uinput device for a sink that writes every batch as
`syn <type>:<code>:<value> ...`, which is exactly what the tests parse.
`tests/fixtures/hardware/calibration_capture.bin` is a real recording from a
SpaceMouse Pro: 2004 frames of axis sweeps followed by three presses of the FIT
button, with the decoded values in the `.txt` next to it.

To watch the real device the daemon creates:

```bash
ls /sys/class/input/*/name | while read -r f; do
  grep -q "Omarchy SpaceMouse" "$f" && echo "${f%/name}"
done
sudo libinput debug-events --device /dev/input/eventN
```

---

## Backlog

- Per-profile `sensitivity`, so a browser can be gentler than a CAD viewer
  without touching the global setting.
- A calibration command that samples the puck at rest and proposes a deadzone.
- Optional lift-to-scroll for ordinary windows, so the puck is useful outside
  3D views too.
- Profiles keyed on window title as well as class, for applications that put
  several kinds of view in one window class.
- A `native` handshake that checks the application really did connect to
  spacenavd, and warns when it did not.

## License

MIT. See `LICENSE`.
