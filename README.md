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
                       │                      Blender)
  Hyprland  ───────────┘           ──►  off      the puck stays quiet here
  .socket2.sock                    ──►  mouse    a virtual uinput mouse and
  (focused window class)                         keyboard: drag gestures + wheel
       ▲                           ──►  keys     axes bound to keys, with repeat
       │
  cursor parking, clutching, and the physical mice held aside mid-drag
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

**If `id -nG` already lists `uucp`, that is the whole job**: no logout, no
restart. The daemon retries every ten seconds, so it picks the device up on its
own within ten seconds of `udevadm trigger`. The logout is only needed when the
`usermod` line above actually had to add you to the group.

Writing to `/dev/uinput` is all the daemon needs. Reading the device back is a
separate permission, and only `tests/live_check.py` wants it, because event
nodes are `root:input` and a desktop user is usually in neither group. Add this
second line if you want to run that self-test:

```bash
echo 'SUBSYSTEM=="input", ATTRS{name}=="Omarchy SpaceMouse", MODE="0660", GROUP="uucp"' \
  | sudo tee -a /etc/udev/rules.d/99-omarchy-spacemouse-uinput.rules
sudo udevadm control --reload-rules
```

Bambu Studio, Orca Slicer and PrusaSlicer need a third rule, for a different
device and a different reason: they bypass spacenavd and read `/dev/hidraw*`
themselves. See [the slicers](#the-slicers-read-the-puck-themselves-once-they-are-allowed-to)
below. `install.sh` prints the line when it sees a puck it cannot read.

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

| profile                | matches                                        | type   |
|------------------------|------------------------------------------------|--------|
| `cad-native`           | KiCad, FreeCAD, Blender                        | native |
| `fusion`               | `fusion360.exe`                                | mouse  |
| `bambu`                | Bambu Studio, Orca Slicer, PrusaSlicer         | native |
| `bambu-mouse-fallback` | nothing; pin it by name                        | mouse  |
| `desktop-off`          | terminals (`org.omarchy.*` included), chat clients, Spotify, Obsidian | off |
| `browser-threejs`      | Opera, Chromium, Chrome, Brave, Firefox, Zen   | mouse  |
| `default`              | everything else                                | mouse  |

Fusion has its own profile for two reasons.

Its mouse conventions are backwards from everyone else's: in Fusion the middle
button **pans** and shift plus middle **orbits**, where every other viewer does
it the other way round. Its FIT button is deliberately unbound, because no
fit-to-view shortcut can be relied on under Wine; put one in `fit_key` if you
bind one yourself.

And **Fusion picks its orbit pivot from whatever is under the pointer** at the
moment the button goes down. Nothing under the pointer means it falls back to
the camera target, which after a pan is often nowhere near the model. So the
default behaviour of parking the pointer in the middle of the window, and
picking it up again at every clutch, made the model jump away from whatever
the user was looking at, repeatedly. The Fusion profile therefore never moves
the pointer at all:

```json
"cursor": "keep",          // never warp: the pointer is the pivot
"clutch": "off",           // no periodic recentring
"edge_guard": "off",       // and no warp at the window edge either
"idle_release_ms": 350,    // a pause must not release the button
"switch_hold_ms": 250,     // nor may a wobble swap orbit for pan
"dominance_ratio": 2.0
```

**In practice: put the pointer on the part of the model you want to turn
around, then take hold of the puck.** That is the same thing you would do with
a mouse in Fusion, and it is the only way to choose a pivot from outside the
application. Real automatic centring (orbit around the model, wherever the
pointer is) needs a plug-in on Fusion's own side that can call its camera API;
nothing an input daemon does can reach that.

The idle release and switch hold are there for the same reason. Every press of
the orbit button is a fresh pivot choice, so the profile is deliberately slow
to let go and slow to change its mind: a short pause mid-orbit keeps the
button down, and a brief wobble toward pan does not swap the button out and
back.

### The slicers read the puck themselves, once they are allowed to

`bambu` is a `native` profile, but for a different reason than `cad-native`.
Bambu Studio, Orca Slicer and PrusaSlicer are one family, and all three carry
their own `Mouse3DController` built on hidapi. They never talk to spacenavd:
they enumerate `/dev/hidraw*`, look for 3Dconnexion's vendor ids and open the
node directly. When they can, they give you real six-axis navigation with their
own pivot and their own sensitivity dialog, which beats anything an emulated
drag can do. So the daemon stays quiet and lets them have it.

Out of the box they cannot. `/dev/hidraw*` is `0600 root:root`, so the slicer
finds the puck, fails to open it and logs *"3DConnexion device cannot be
opened"*. One rule fixes that:

```bash
sudo install -m 644 udev/70-omarchy-spacemouse-hidraw.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=hidraw
```

The rule grants twice over, and both halves earn their place. `GROUP="plugdev"`
covers ordinary applications. `TAG+="uaccess"` covers the Flatpak builds, which
is how most people install Bambu Studio: **a Flatpak sandbox keeps your uid but
drops every supplementary group**, so a group grant alone never reaches inside
it, while the uaccess ACL is written for the uid and does. Check it from inside
the sandbox with `flatpak run --command=id com.bambulab.BambuStudio`. The
filename has to sort before `73-seat-late.rules`, where systemd turns the
uaccess tag into that ACL, so keep the `70-` prefix.

spacenavd is not in the way here and does not need to be stopped. It grabs the
**evdev** node; hidraw is a separate path out of the same kernel HID device, and
both readers see every report. That also means the daemon would otherwise drive
the pointer while the slicer drove its own camera, which is exactly the double
control the `native` type exists to avoid.

Until the rule is installed the puck does nothing in a slicer. Mouse emulation
is there under a name instead of a pattern:

```bash
spacemouse-ctl profile bambu-mouse-fallback   # pin it
spacemouse-ctl auto                           # back to following focus
```

It follows the slicers' own convention (left drag rotates, right drag pans, the
wheel zooms) and never moves the pointer, because a left drag that starts on a
model drags the model instead of the camera. Park the pointer on empty plate
before you take hold of the puck.

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

### What happens to the mouse pointer

A synthetic drag has a problem a real mouse does not: it starts wherever the
pointer happens to be, and a long drag walks into a screen edge and stops. Both
are handled, and the handling is what makes the puck feel like a puck.

There are two policies, set per profile with `"cursor"`:

| `cursor`  | what happens                                                       |
|-----------|--------------------------------------------------------------------|
| `center`  | the default: park in the middle of the window, drive, put it back  |
| `keep`    | never move the pointer. For applications that read it themselves   |

and two clutch policies, set with `"clutch"`:

| `clutch` | what happens                                                        |
|----------|---------------------------------------------------------------------|
| `auto`   | the default: recentre after `clutch_fraction` of the window         |
| `off`    | no periodic recentring                                              |

and the edge guard, `"edge_guard"`, which jumps back to where the drag began
when the pointer is about to run off the window. It is on under `center` and
off under `keep`, where the pointer is the user's to place, and it can be set
either way per profile.

The guard is deliberately hard to trigger: it has to be armed (the pointer has
left the margin since it last fired), half a second has to have passed, and the
pointer has to have moved. Without those three gates it loops, because it warps
the pointer to a place that is itself inside the margin and then sees the same
thing on the next tick. That is not hypothetical: it once fired 301 times in a
single Fusion session.

**What a drag is measured against** is not simply the focused window. Palettes
and toolbars are windows too, and they carry the application's own class: Fusion
puts a 300x25 strip and a 300x450 panel next to its 2536x1390 viewport. Measured
against the strip, a pointer on the model is outside the window entirely and
every position looks like an edge. So the area is the **largest mapped window of
the focused class on that workspace**, falling back to the monitor.

Under `center`:

- **Parking.** When a drag starts, the pointer is saved and moved to the middle
  of the focused window, so there is room in every direction.
- **Putting it back.** When the drag ends, the pointer goes back exactly where
  it was. If `follow_mouse` handed the focus to whatever was under that spot,
  the focus is taken back too.
- **Clutching.** Once a drag has travelled `clutch_fraction` (0.35) of the
  window, the button is lifted, the pointer is recentred and the button goes
  back down, all inside one frame. The view does not stutter and the drag never
  reaches an edge.
- **Flat acceleration.** The virtual device is set to `accel_profile = flat`
  through Hyprland at startup, so a given deflection always moves the same
  distance.

Switching between orbit and pan mid-drag is a change of button, not a new drag:
nothing is warped, so the view does not jump. A profile can make that switch
harder to trigger with `switch_hold_ms`, which is the number of milliseconds a
competing gesture has to stay dominant before it takes over.

Turn the lot off with `"cursor_warp": false`, or `--no-cursor-warp` for one run.

### The mouse and the puck at the same time

Wayland has one pointer. A hand on the mouse and a hand on the puck both feed
it, and the two get mixed into the same drag: the view comes out crooked.

So for the length of a gesture, and only then, the physical mice are taken over
with `EVIOCGRAB` and piped through the same virtual device the puck uses. Their
motion is dropped while the puck owns the drag; their buttons and wheel go
straight through. The moment the drag ends they are handed back, untouched,
with their own acceleration profile and their own identity.

```
spacemouse-ctl pointer shared      hand the mice back and leave them alone
spacemouse-ctl pointer proxied     take them over during gestures again
```

Four things stand between this and a dead mouse, which is the failure that
would matter:

1. the kernel drops a grab whenever the file descriptor closes, whatever
   killed the daemon,
2. a watchdog thread releases every grab if the main loop has not ticked for a
   second,
3. `spacemouse-ctl pointer shared` and the panel button hand them back at once,
4. anything unexpected (no permission, no virtual device, a node that cannot be
   opened) means nothing is grabbed at all and the daemon carries on.

Which devices are eligible: udev has to call it `ID_INPUT_MOUSE`, and it must
not be a touchpad, tablet, joystick or 3D mouse. The puck itself (`046d:c62b`)
and this daemon's own virtual device are never candidates. `pointer_exclude_names`
and `pointer_exclude_ids` take anything else out; `pointer_include` forces one
back in.

Reading a mouse's event node needs permission of its own:

```bash
echo 'SUBSYSTEM=="input", KERNEL=="event*", ENV{ID_INPUT_MOUSE}=="1", MODE="0660", GROUP="uucp"' \
  | sudo tee /etc/udev/rules.d/99-omarchy-spacemouse-pointer.rules
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=input
```

Without it the daemon says `pointer: shared (no permission)` once and works
exactly as it did before.

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
| `deadzone`           | 18      | counts ignored around centre. A running gesture keeps going down to here. |
| `engage_deadzone`    | 24      | counts needed to *start* a gesture. The gap to `deadzone` is the hysteresis. |
| `full_scale`         | 350     | counts that count as full deflection.                             |
| `curve`              | 1.3     | response exponent. Higher means gentler near centre.              |
| `sensitivity`        | 1.0     | overall multiplier.                                               |
| `axis_gain`          | all 1.0 | per-axis multiplier.                                              |
| `axis_invert`        | all false | per-axis direction flip.                                        |
| `pointer_speed`      | 900     | pixels per second at full deflection.                             |
| `wheel_speed`        | 3.0     | wheel detents per second at full deflection.                      |
| `wheel_hi_res`       | true    | emit `REL_WHEEL_HI_RES` for smooth scrolling.                     |
| `smoothing_ms`       | 30      | exponential average on the axes. 0 turns it off.                  |
| `idle_release_ms`    | 80      | how long it keeps the buttons after you let go.                   |
| `dominance_ratio`    | 1.35    | how much stronger a competing group must be to take over.         |
| `tick_hz`            | 120     | emit rate.                                                        |
| `cursor_warp`        | true    | park the pointer in the window while dragging.                    |
| `clutch_fraction`    | 0.35    | how far a drag travels before recentring.                         |
| `flat_acceleration`  | true    | set the virtual device to flat acceleration at startup.           |
| `pointer_mode`       | proxied | `shared` leaves the physical mice alone entirely.                 |
| `pointer_grab`       | gesture | `always` holds the grab the whole time instead.                   |
| `edge_margin`        | 20      | how close to the window edge the edge guard fires, in pixels.     |
| `edge_guard_cooldown_ms` | 500 | the edge guard may not fire more often than this.                 |
| `switch_hold_ms`     | 0       | how long a competitor must stay dominant before taking over.      |
| `activate_threshold` | 0.0     | extra gate on the normalized magnitude. Normally left at zero.    |
| `release_threshold`  | 0.0     | same, for keeping a gesture alive.                                |

Any profile can set these for itself, and the profile's value wins:
`idle_release_ms`, `switch_hold_ms`, `dominance_ratio`, `pointer_speed`,
`wheel_speed`, `smoothing_ms`, `clutch_fraction`, `engage_deadzone`,
`deadzone`, `curve`, `sensitivity`, plus the `cursor` and `clutch` policies.
An application's feel is its own business.

**The two dials worth your time.** Everything else has a defensible default;
these two are personal and nobody else can pick them for you:

- **`pointer_speed`** (900): how fast a full deflection drags. Too slow feels
  like wading, too fast feels nervous. Change it by 200 at a time.
- **`curve`** (1.3): how much of the travel is spent being gentle. 1.0 is
  linear and twitchy near centre, 1.8 is very soft and then sudden. Change it
  by 0.2 at a time.

Then, in this order, only if something is actually wrong:

1. **Drift at rest**: watch `spacemoused.py --dump` with your hand off the puck.
   Put `deadzone` just above the largest number you see, and `engage_deadzone`
   about 6 counts above that.
2. **Zoom too slow or too fast**: `wheel_speed`, in detents per second.
3. **A gesture keeps flipping to another**: raise `dominance_ratio`.
4. **The gesture drops out mid-move**: raise `idle_release_ms`.
5. **It feels like it lags**: lower `smoothing_ms` to 15, or 0.
6. **Wrong direction**: flip the `gain` sign on that axis in the gesture, or
   set `axis_invert` if you want it flipped everywhere.

---

## The bar widget

The Quickshell plugin (`jernberg.spacemouse`) shows the icon and the active
profile name.

- **left click** enable or disable
- **middle click** back to following the focused window
- **right click** the panel: window class, profile, gesture, spacenavd, uinput
  and pointer health, the profile list with a manual override, and buttons for
  enable/disable, reload, and handing the physical mice back

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
spacemouse-ctl pointer proxied     take the physical mice over during gestures
spacemouse-ctl pointer shared      hand them straight back (escape hatch)
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
`spacemouse-ctl status` shows the window class the daemon sees and whether
`hyprland` is connected. If it is disconnected, the daemon retries every two
seconds, and after a three second grace period it falls back to the fallback
profile so a session without a compositor still works. Check also that `mode`
says `auto`: a manual override pins one profile until `spacemouse-ctl auto`.

**Middle-clicking pastes in my terminal.**
The terminal is getting a `mouse` profile. Add its class to `desktop-off`.

**Which profile does an empty workspace get?**
None. With focus on no window at all, nothing is emitted. A window that sets
no class of its own is a different case: it has a title, so it counts as a
window and gets the fallback profile.

**My mouse feels different / stopped working.**
`spacemouse-ctl pointer shared` hands it back immediately, and so does
stopping the service. `spacemouse-ctl status` shows the `pointer` line: it
says `proxied (<name>)` when a mouse is being arbitrated and `shared (...)`
with the reason when it is not.

**The pointer jumps to the middle of the window when I use the puck.**
That is the parking, and it is deliberate: it is what gives a drag room to run
and what real 3D mouse drivers do. Set `"cursor": "keep"` on that profile if
the application would rather read the pointer itself, or `"cursor_warp": false`
to turn it off everywhere.

**The model jumps away from the middle when I start to orbit.**
The application is choosing its pivot from what is under the pointer, the way
Fusion does. Give that profile `"cursor": "keep"`, `"clutch": "off"` and
`"edge_guard": "off"`, then put the pointer on the model before taking hold of
the puck.

**The view is jittering, and the counters are climbing.**
`spacemouse-ctl status --json` reports `cursor_warps`, `clutches` and
`edge_clutches` separately, and the daemon logs a per gesture tally at debug
level. Three numbers climbing together means the edge guard is firing: either
the profile should have `"edge_guard": "off"`, or the drag area is wrong, which
the tally line names (`area from window` or `area from monitor`).

**Logs**: `journalctl --user -u omarchy-spacemouse -f`

---

## Working on it

```bash
python3 tests/run.py            # 238 tests, standard library only
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

`tests/live_gesture.py` is the big one: it creates the device, plays the
recorded capture through the real engine, reads every event back out of
`/dev/input/eventN`, and checks that what came back is what went in, that the
pointer was parked and put back, and that a long drag clutched.

```bash
python3 tests/live_gesture.py --window      # opens its own window to drag in
```

Once `/dev/uinput` is writable, one command checks the whole kernel path:
it creates the device, finds the `/dev/input/eventN` the kernel gave it, plays
the recorded capture through the real gesture engine, and checks that what
comes back out is what went in.

```bash
python3 tests/live_check.py
```

It is not part of `tests/run.py`, because it is the only thing here that
touches the kernel. Without the second udev rule above it still creates the
device and emits, and reports the readback as skipped rather than failed.

### Checking the directions in a browser

`tests/orbit_probe.html` is a single page with no dependencies that shows what
the browser actually received: which buttons went down, whether shift was held,
how far the pointer moved, how much wheel arrived, and a cube that orbits, pans
and zooms the way a Three.js viewer would. Open it, focus it, move the puck.

```bash
xdg-open tests/orbit_probe.html
```

To drive it without touching the puck, play the recording into whatever window
has focus:

```bash
python3 daemon/spacemoused.py --no-focus --profile browser-threejs \
  --replay tests/fixtures/hardware/calibration_capture.bin --replay-speed 4
```

If something moves the wrong way, flip the `gain` sign for that axis in the
profile (see Tuning).

To watch the device by hand instead:

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
