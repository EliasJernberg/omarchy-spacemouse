# Testresultat, omarchy-spacemouse

Körningar på **vanessa** (Omarchy 4.x, Arch, Hyprland 0.56.2, Python 3.14.7,
spacenavd 1.3.x, SpaceMouse Pro) 2026-09-18.

---

## 1. Automatiska tester

`python3 tests/run.py`: **129 tester, alla gröna, 2.3 s**. Enbart standard-
biblioteket, ingen av dem rör kärnan, den körande daemonen eller skrivbordet.

| Fil | Antal | Vad det täcker |
|-----|-------|----------------|
| `test_protocol.py` | 8 | spacenavds ramformat mot en verklig hårdvaruinspelning |
| `test_profiles.py` | 42 | profilmatchning, defaultfilen, normalisering, hot reload, fokus-grace |
| `test_gestures.py` | 35 | gestmaskinen: dominant grupp, tryck/släpp, idle-release, profilbyte |
| `test_uinput.py` | 27 | ioctl-nummer, structstorlekar, eventbytes, rättighetsfel |
| `test_replay.py` | 17 | hela daemonprocessen end to end plus kontrollsocketen |

### Protokollet mot facit

Fixturen `tests/fixtures/hardware/calibration_capture.bin` är en riktig
inspelning från pucken på den här maskinen: 2004 ramar med svep på samtliga
axlar följt av tre tryck på FIT-knappen. Textfilen bredvid skrevs vid samma
tillfälle och används som facit: **varje ram avkodas till exakt de värden som
spelades in**, inklusive att FIT är knapp 5 och att varje press har en release.

Toppvärden per axel i inspelningen ligger mellan 50 och 350 counts, vilket
stämmer med att fullt utslag är ungefär 350.

### Gestmaskinen

Testerna kör en klocka som flyttas för hand, så idle-timeouten och bytet av
dominant grupp blir exakta i stället för tidsberoende. Verifierat:

- rotation startar orbit (mittenknapp), translation startar pan (shift plus
  mittenknapp), lyft startar zoom (hjul, ingen knapp),
- **aldrig mer än en knapp nere samtidigt**, även när två axelgrupper får
  utslag samtidigt (överhörning),
- byte av grupp släpper den gamla gruppens knappar **före** den nya trycks,
  i omvänd tryckordning,
- vilonivåerna i inspelningen (4, -18, 20 counts) startar ingen gest alls,
- knappen hålls kvar genom en paus på 100 ms men släpps efter 150 ms stillhet,
- profilbyte, byte till en native-profil och avstängning släpper allt direkt,
- ett skript med åtta olika utslag i rad lämnar noll knappar nere: varje press
  har en matchande release.

### uinput-lagret

ioctl-numren jämförs mot vad `linux/uinput.h` expanderar till
(`UI_DEV_CREATE 0x5501`, `UI_DEV_SETUP 0x405c5503`, `UI_SET_EVBIT 0x40045564`
och så vidare), structstorlekarna mot kärnans (`uinput_setup` 92 byte,
`uinput_user_dev` 1116 byte, `input_event` 24 byte på 64 bitar). Hela
tangentbordsblocket 1 till 31 sätts, annars taggar inte udev enheten som
tangentbord och shift-modifierade gester skulle inte registreras.

Fallbacken till gamla `uinput_user_dev` testas genom att låta `UI_DEV_SETUP`
misslyckas i fejk-lagret.

### End to end

`test_replay.py` startar den riktiga daemonprocessen precis som systemd gör,
fast med `--dry-run` och `--replay`, i en egen `XDG_RUNTIME_DIR` så att en
skarp daemon inte störs. Inspelningen matas genom hela kedjan och traceflödet
kontrolleras: pekarrörelse i båda axlarna, hjul, mittenknapp, shift, tre
KEY_HOME-tryck från FIT-knappen, och **inget kvar nedtryckt när daemonen
stannar**. Därefter drivs samma daemon genom `spacemouse-ctl`: status, enable,
disable, toggle, manuell profil, auto, reload och ett avvisat profilnamn.

---

## 2. Skarp körning på maskinen

### Daemon och tjänst

```
systemctl --user status omarchy-spacemouse.service   → active (running)
```

Loggen vid start: profiler laddade, ansluten till spacenavd på `/run/spnav.sock`,
ansluten till Hyprlands eventsocket, profil satt efter det fokuserade fönstret.
`install.sh` kördes två gånger: andra gången behölls den befintliga
`profiles.json`, pluginet kändes igen som redan installerat och tjänsten fortsatte
köra. Idempotent alltså.

### Fokusföljning

Två fönster på workspace 4 (foot och Opera), fokus flyttat fram och tillbaka
med `hyprctl dispatch`:

| Fokuserat fönster | Profil som aktiverades | Typ |
|-------------------|------------------------|-----|
| `foot` | `desktop-off` | off |
| `Opera` | `browser-threejs` | mouse |
| `foot` | `desktop-off` | off |
| `Opera` | `browser-threejs` | mouse |

Bytet syns i `status.json` inom 0.6 s. Under samma pass dök Fusion 360 upp på
maskinen (en annan körning), och daemonen valde `fusion-bifrost` (native) för
klassen `fusion360.exe` helt av sig själv. Det är alltså verifierat mot en
riktig Fusion-fönsterklass och inte bara mot testdata.

Fokus återställdes till samma fönster och workspace som före testet.

### Två buggar som bara den skarpa körningen hittade

1. **Omarchys egna TUI-fönster fick musprofilen.** Efter en omstart råkade ett
   fönster med klassen `org.omarchy.terminal` vara fokuserat, och eftersom
   `desktop-off` bara listade terminalemulatorerna vid namn föll det igenom till
   fallbacken `default`, alltså mittenknappsorbit i en terminal. Hela prefixet
   `org.omarchy.` täcks nu, med ett test på köpet.
2. **Loggraden kunde namnge fel fönster.** Profilraden läste fönsterklassen
   efter beslutet, så ett fokusbyte däremellan gav en logg som påstod att till
   exempel `browser-threejs` valdes för klassen `foot`. Klassen läses först nu.

Dessutom ändrades starten: daemonen låg en kort stund på fallbackprofilen innan
Hyprland hunnit svara, vilket armerade gesterna mot vilket fönster som helst.
Nu hålls allt av tills det första fokussvaret kommit, med tre sekunders
grace-period om kompositorn aldrig svarar.

En tredje sak syntes när en tom workspace var i fokus: Hyprland rapporterar då
tom klass och tom titel, vilket föll igenom till fallbackprofilen. Ett tomt
skrivbord ska inte ha några gester armerade, så det fallet är av nu. Ett fönster
som saknar egen klass men har en titel (Oden Scope är ett sådant på den här
maskinen) räknas fortfarande som ett fönster och får fallbacken.

### Kontrollsocketen

`spacemouse-ctl status`, `profiles`, `enable`, `disable`, `toggle`,
`profile <namn>`, `auto` och `reload` körda skarpt mot den installerade
daemonen. Svaret beskriver alltid det tillstånd som faktiskt är aktivt: en
manuell profil syns direkt i svaret på kommandot, inte en tick senare.

### Hot reload

En extra profil lades in överst i `~/.config/omarchy-spacemouse/profiles.json`
medan daemonen körde. Inom två sekunder loggades "profiles.json changed on
disk, reloading", profillistan blev sex poster och det fokuserade
foot-fönstret bytte till den nya profilen. Efter att filen återställts gick
allt tillbaka.

### Webbläsarprofilen genom hela kedjan

Samma inspelning spelad genom `browser-threejs` i stället för `default` ger
högerknapp för pan och vänsterknapp för orbit, hjul för zoom, och noll knappar
kvar nedtryckta:

```
BTN_LEFT 2, BTN_RIGHT 2, REL_X 32, REL_Y 41, REL_WHEEL_HI_RES 11
sekvens: RIGHT ner, RIGHT upp, LEFT ner, LEFT upp
```

FIT-knappen loggar att profilen saknar `fit_key`, vilket är meningen: det finns
ingen allmän "zooma till allt"-tangent i en webbläsare.

### install.sh och uninstall.sh

`install.sh` kördes tre gånger totalt, `uninstall.sh` en gång mot ett sandlåde-
HOME. Det avslöjade en bugg: raden "removed the plugin" skrevs ut även när
borttagningen misslyckades, eftersom den låg efter kommandot i stället för i en
else-gren. Fixat. Efter testet kördes `install.sh` igen och allt var tillbaka:
tjänsten enabled och active, symlänken på plats, pluginet kvar, profilfilen
orörd.

### Bar-widgeten

- `omarchy plugin validate .` går igenom.
- `omarchy plugin add git@github.com:EliasJernberg/omarchy-spacemouse.git --enable --yes`
  installerade och aktiverade `jernberg.spacemouse`; den ligger i
  `bar.layout.right` i `shell.json`.
- Shell-loggen: `DEBUG qml: Local plugin changed, reloading: jernberg.spacemouse`,
  **inga QML-fel eller varningar** från pluginet.
- Skärmdump av baren: ikon plus aktivt profilnamn (`desktop-off`), nedtonad
  eftersom profilen inte skickar något.
- Skärmdump av panelen (öppnad med `omarchy-shell shell toggle
  jernberg.spacemouse`): hero med status, faktaraderna Window/Profile/Mode/
  spacenavd/uinput, den röda raden om `/dev/uinput`, profillistan med bock på
  det aktiva valet och knapparna Disable och Reload profiles.
- Kommandot widgeten kör (`bash -lc 'exec "$@"' bash <pluginkatalog>/bin/spacemouse-ctl profile ...`)
  kördes för hand och fungerade, både manuell profil och auto.

---

## 3. Det som inte gick att verifiera

**`/dev/uinput` var `crw------- root root` under hela bygget**, alltså
root-only, och udev-regeln hade inte lagts in när arbetet avslutades. Därför är
följande **oprövat på riktig kärna**:

1. att den virtuella enheten går att skapa på den här maskinen (koden är testad
   mot ett fejk-lager, ioctl-numren mot kärnans headers, men själva
   `open("/dev/uinput")` har bara verifierats misslyckas med EACCES, vilket
   daemonen rapporterar som `uinput denied` precis som tänkt),
2. att kärnan publicerar enheten som `/dev/input/eventN` med namnet
   "Omarchy SpaceMouse" och att eventen läses tillbaka korrekt,
3. att Hyprland och libinput accepterar en enhet som är både mus och
   tangentbord, alltså att shift-modifierade gester verkligen registreras,
4. apptestet i webbläsaren: att orbit, pan och zoom rör modellen åt rätt håll i
   en Three.js-vy,
5. därmed också att teckenkonventionerna i defaultprofilerna (vilket håll
   rx, ry, x, z och y drar) känns rätt i handen.

### Så här slutförs det

```bash
# 1. regeln, en gång, med sudo i en riktig terminal
echo 'KERNEL=="uinput", MODE="0660", GROUP="uucp", OPTIONS+="static_node=uinput"' \
  | sudo tee /etc/udev/rules.d/99-omarchy-spacemouse-uinput.rules
# valfritt, men krävs för punkt 2 ovan (självtestet läser tillbaka enheten)
echo 'SUBSYSTEM=="input", ATTRS{name}=="Omarchy SpaceMouse", MODE="0660", GROUP="uucp"' \
  | sudo tee -a /etc/udev/rules.d/99-omarchy-spacemouse-uinput.rules
sudo udevadm control --reload-rules && sudo udevadm trigger /dev/uinput

# 2. kontrollera. voysys ligger redan i uucp (id -nG), och den körande
#    daemonprocessen har gid 984 bland sina supplementary groups, så ingen
#    utloggning och ingen omstart behövs: daemonen försöker igen var tionde
#    sekund och tar enheten av sig själv.
ls -l /dev/uinput          # ska vara crw-rw---- root uucp
spacemouse-ctl status      # uinput ska gå från denied till ready

# 3. självtestet: skapar enheten, spelar inspelningen, läser tillbaka
python3 tests/live_check.py

# 4. apptestet: fokusera en Three.js-vy och spela upp inspelningen i den
systemctl --user restart omarchy-spacemouse
# (fokusera webbläsarfönstret, ta i pucken)
```

Punkt 4 går också att göra utan att röra pucken. Öppna `tests/orbit_probe.html`
(en sida utan beroenden som visar exakt vilka knappar, modifierare, pekardeltan
och hjulsteg webbläsaren faktiskt tog emot, plus en kub som roterar, panorerar
och zoomar), fokusera den och spela upp inspelningen mot det fokuserade
fönstret:

```bash
xdg-open tests/orbit_probe.html
python3 daemon/spacemoused.py --no-focus --profile browser-threejs \
  --replay tests/fixtures/hardware/calibration_capture.bin --replay-speed 4
```

Om något drar åt fel håll är det ett teckenbyte i `gain` för den axeln i
`~/.config/omarchy-spacemouse/profiles.json`, se avsnittet Tuning i README.
