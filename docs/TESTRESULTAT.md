# Testresultat, omarchy-spacemouse

Körningar på **vanessa** (Omarchy 4.x, Arch, Hyprland 0.56.2, Python 3.14.7,
spacenavd 1.3.x, SpaceMouse Pro) 2026-09-18.

---

## 1. Automatiska tester

`python3 tests/run.py`: **207 tester, alla gröna, 2.3 s**. Enbart standard-
biblioteket, ingen av dem rör kärnan, den körande daemonen eller skrivbordet.

| Fil | Antal | Vad det täcker |
|-----|-------|----------------|
| `test_protocol.py` | 8 | spacenavds ramformat mot en verklig hårdvaruinspelning |
| `test_pointer.py` | 47 | vilka enheter som får grabbas, vad som släpps igenom, vakthunden |
| `test_cursor.py` | 24 | markörparkering, clutch, återställning, fokusskyddet |
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
maskinen (en annan körning), och daemonen valde Fusion-profilen för
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

## 3. Andra omgången: pekar-arbitrering, markör och Fusion

Efter att bifrost skrotats är pluginet enda vägen in för alla appar. Det här
verifierades efter den ombyggnaden.

### Live: hela kedjan genom riktig kärna

`python3 tests/live_gesture.py --window` öppnar ett eget fönster på workspace 4,
skapar den virtuella enheten, spelar hela hårdvaruinspelningen genom den
riktiga gestmaskinen och läser samtidigt tillbaka allt ur enhetens
`/dev/input/eventN`:

```
1950 events i 1778 batchar emitterade
1950 events tillbaka ur kärnan
rel-summor ut: {REL_X: 3484, REL_Y: -3999, REL_WHEEL_HI_RES: 709, REL_WHEEL: 5}
rel-summor in: {REL_X: 3484, REL_Y: -3999, REL_WHEEL_HI_RES: 709, REL_WHEEL: 5}
ok   varje event kom tillbaka oförändrat ur kärnan
ok   den relativa rörelsen summerar exakt (ingen subpixel går förlorad)
```

Alltså: **bit för bit identiskt in och ut**, och subpixel-ackumulatorerna
tappar ingenting.

### Live: markören

Samma körning, nio dragsessioner:

```
9 drag-sessioner, 14 clutchar
ok   drag 1..9 parkerade pekaren mitt i fönstret
ok   drag 1..9 satte tillbaka pekaren där den var
ok   pekaren står där den började: (1280, 733)
ok   ett långt drag clutchade i stället för att gå in i skärmkanten
```

Varje enskilt drag verifierades med exakta koordinater: positionen direkt efter
warpen jämförs med fönstrets mittpunkt, och positionen efter gestslut jämförs
med den sparade positionen. Inga ungefärligheter.

Dispatcher-syntaxen på Omarchy 4 är Lua: `hl.dsp.cursor.move({ x = .., y = .. })`.
`hyprctl keyword` fungerar inte alls här ("keyword can't work with non-legacy
parsers"), så platt acceleration sätts med
`hyprctl eval hl.device({ name = "omarchy-spacemouse-1", accel_profile = "flat" })`,
mot det namn Hyprland faktiskt använder för enheten just då.

### Live: riktig 3D-app

three.js orbit controls (threejs.org/examples/#misc_controls_orbit) öppnad i
Opera på workspace 4, en orbit-burst på 1.2 s, grim-skärmdumpar av fönstret
före och efter: kameran har tydligt roterat runt scenen, konerna ligger i ett
annat mönster och ljussättningen har vridit sig. Fönstret hölls fokuserat i
**9.7 sekunder** totalt, sedan stängdes det och Elias fokus och workspace
återställdes.

Dessförinnan kördes samma sak mot `tests/orbit_probe.html`, som visar exakt vad
webbläsaren tog emot:

```
moves 673   dx 2427   dy -3975   wheel -550
yaw 198.8   pitch 786.8   zoom 4.00   pan 1930,-2008
```

### Pekar-arbitreringen skarpt

Daemonen hittar rätt enhet av sig själv. Loggen vid första musprofilen:

```
cannot read Logitech PRO X, so the mouse stays on its own: add the udev rule
  from the README (SUBSYSTEM=="input", ENV{ID_INPUT_MOUSE}=="1")
virtual device 'Omarchy SpaceMouse' created as input34
acceleration set to flat for omarchy-spacemouse-1
```

Den valde alltså ut `Logitech PRO X` (som udev kallar `ID_SERIAL=Logitech_USB_Receiver`)
och sorterade bort allt annat: 3Dconnexion-pucken (`ID_INPUT_3D_MOUSE`),
DualSense-touchpaden, Ducky-tangentbordets musgränssnitt och sin egen virtuella
enhet. Statusen blev `pointer: shared (no permission)` och daemonen fortsatte
precis som förut, vilket är kravet.

`UI_GET_SYSNAME` används för att veta exakt vilken `inputN` som är vår egen.
Det är enda pålitliga sättet: namnet är inte unikt (en andra instans heter
likadant) och eventnumret är vad som råkade vara ledigt.

### Två avsteg från beställningen, båda medvetna

1. **Grabben hålls bara under en gest**, inte hela tiden. En permanent grab
   skulle skicka all musrörelse genom en enhet med platt accelerationsprofil,
   alltså skulle musen kännas annorlunda hela dagen i stället för bara under
   ett drag. `pointer_grab: "always"` ger det ursprungliga beteendet.
2. **Aktiveringströskeln är inte helt borta, den flyttade till råa counts.**
   Med enbart deadzone 18 startade vilonivåerna gester av sig själva: den här
   puckens vilobrus når 20 counts på rx. Nu gäller hysteres i den enhet Elias
   kan mäta med `--dump`: starta vid 24, håll ner till 18.

---

## 4. Det som återstår

Uinput-regeln är **inlagd och verifierad**: `/dev/uinput` är `crw-rw---- root uucp`,
den virtuella enheten dyker upp som `/dev/input/event26` ("Omarchy SpaceMouse")
och allt i avsnitt 3 ovan kördes mot riktig kärna. Det som fanns i den här
listan tidigare är därmed avklarat.

Kvar finns en sak, och den kräver root:

### Musens eventnod är inte läsbar än

Pekar-arbitreringen kan inte ta över musen förrän dess `/dev/input/eventN` går
att öppna. Just nu är alla musnoder `root:input 0660` och voysys är inte i
gruppen `input`, så daemonen rapporterar `pointer: shared (no permission)`,
loggar regeln en gång och fortsätter precis som förut. Allt annat fungerar.

```bash
echo 'SUBSYSTEM=="input", KERNEL=="event*", ENV{ID_INPUT_MOUSE}=="1", MODE="0660", GROUP="uucp"' \
  | sudo tee /etc/udev/rules.d/99-omarchy-spacemouse-pointer.rules
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=input
ls -l /dev/input/event13     # ska bli crw-rw---- root uucp (Logitech PRO X)
spacemouse-ctl status        # pointer ska gå till: proxied (Logitech PRO X)
```

Ingen utloggning behövs: voysys ligger redan i `uucp` och daemonprocessen bär
gid 984, så den tar noden inom tre sekunder av sig själv.

Verifiera sedan, med musen i handen och pucken i den andra: rör båda samtidigt
i en 3D-vy. Musens rörelse ska inte längre smeta in i puckens drag, medan
musknappar och hjul fortfarande fungerar. `spacemouse-ctl pointer shared` är
nödutgången om något känns fel, och den släpper musen omedelbart.

### Det bara Elias kan avgöra

Hastighet och kurva är smaksak och står som exakta rattar i README:
`pointer_speed` (900, ändra 200 i taget) och `curve` (1.3, ändra 0.2 i taget) i
`~/.config/omarchy-spacemouse/profiles.json`. Filen läses om inom en sekund, så
det går att justera med 3D-vyn öppen.

Fusion-profilens FIT-knapp står avsiktligt tom: binder han en tangent för
"fit to view" i Fusion är det bara att skriva in den i `fit_key`.
