# Testresultat, omarchy-spacemouse

Körningar på **vanessa** (Omarchy 4.x, Arch, Hyprland 0.56.2, Python 3.14.7,
spacenavd 1.3.x, SpaceMouse Pro), 2026-09-18 till 2026-09-21.

---

## 1. Automatiska tester

`python3 tests/run.py`: **235 tester, alla gröna, 2.4 s**. Enbart standard-
biblioteket, ingen av dem rör kärnan, den körande daemonen eller skrivbordet.

| Fil | Antal | Vad det täcker |
|-----|-------|----------------|
| `test_protocol.py` | 8 | spacenavds ramformat mot en verklig hårdvaruinspelning |
| `test_pointer.py` | 47 | vilka enheter som får grabbas, vad som släpps igenom, vakthunden |
| `test_cursor.py` | 49 | markörparkering, keep-läget, clutch, kantskyddets loopspärr, per-profil-policy |
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

## 4. Tredje omgången: Fusions pivot

Elias testade Fusion-profilen och modellen hoppade iväg från centrum vid
geststart. Orsaken: **Fusion väljer sin orbit-pivot ur det som ligger under
markören** när knappen går ner. Vår warp till fönstermitt plus clutch-omtryck
lät Fusion välja om pivoten hela tiden.

### Vad som ändrades

Per-profil-policy i stället för en global inställning:

| Inställning | Fusion | Övriga |
|-------------|--------|--------|
| `cursor` | `keep` (ingen warp alls) | `center` (parkera i fönstermitt) |
| `clutch` | `off` (bara kantskydd) | `auto` (var 35 % av fönstret) |
| `idle_release_ms` | 350 | 80 |
| `switch_hold_ms` | 250 | 0 |
| `dominance_ratio` | 2.0 | 1.35 |

Kantskyddet i `clutch: off` warpar tillbaka till **markörens startposition**,
inte till fönstermitten, så pivoten hamnar där användaren valde den.

Elva inställningar går nu att sätta per profil, plus de två policyerna.

### Verifiering

**Torrkörning med fusion-profilen**, replay av hela hårdvaruinspelningen i 12
sekunder (`--dry-run --profile fusion --replay ... --replay-loop`), avläst ur
statusfilen:

```
profile        fusion
cursor_mode    keep
cursor_warps   0
clutches       0
edge_clutches  0
```

Alltså **ingen enda cursor.move och ingen clutch**, vilket var kravet.

**Enhetstester**: 41 i `test_cursor.py`, bland annat att ett drag som håller
sig inne i fönstret inte warpar alls, att ett drag på 400 px (långt förbi de
35 % som annars clutchar) inte clutchar, att kantskyddet ändå räddar ett drag
som når kanten och då warpar till startpunkten, att en paus på 250 ms inte
släpper knappen men 350 ms gör det, och att en kort wobble mot pan inte byter
gest medan en ihållande gör det.

### Pekar-arbitreringen skarpt

Udev-regeln för musen kom på plats under passet, så `/dev/input/event13` är
läsbar nu. `python3 tests/live_pointer.py` verifierar hela mekaniken mot riktig
kärna **utan att röra hans egen mus**: en andra uinput-enhet får agera fysisk
mus och `pointer_include` pekar ut den, medan Logitech-musen är explicit
utesluten.

```
watching: ['Omarchy SpaceMouse Test Pointer']
ok   the stand-in is watched and the real mouse is not
ok   nothing is forwarded outside a gesture: the kernel is delivering it directly
ok   its motion was swallowed (2 events dropped)
ok   its buttons still came through: [(EV_KEY, BTN_TASK, 1), (EV_KEY, BTN_TASK, 0)]
ok   the mouse was handed straight back
ok   a stalled main loop released the grab
```

Mätningen görs på händelseströmmen, inte på markörpositionen: markören delas
med den som sitter vid datorn, och Elias flyttade sin mus mitt i testet, vilket
gjorde positionsmätning meningslös. Händelseräknarna och avläsningen ur vår
egen enhets nod är exakta oavsett vad han gör.

---

## 5. Fjärde omgången: kantskyddet loopade

Efter Elias Fusion-pass visade statusfilen `cursor_warps=301`, `clutches=301`,
`edge_clutches=301`. Räknarna var inte samma räknare: de var tre olika som
råkar öka exakt en gång var per kantclutch, så siffrorna var ärliga. Det var
**301 verkliga kantclutchar**, en per tick så länge draget pågick.

### Orsaken, båda hypoteserna stämde

Fusion har tre fönster med samma klass `fusion360.exe` på workspace 5:

```
2536x1390  at 2572,38     huvudfönstret (vyn)
 300x450   at 2613,269    flytande panel
 300x25    at 2613,719    flytande list
```

`hyprctl activewindow` pekar ut det som senast fick fokus, ofta en av
paletterna. Markören på modellen ligger då **helt utanför** det 300x25 stora
"fönstret", och min kantkontroll räknade `at_x - x <= margin` med ett negativt
tal, alltså alltid sant. Kantskyddet warpade tillbaka till startpositionen, som
också låg utanför, och trippade igen nästa tick. 120 Hz i några sekunder ger
301.

### Fyra ändringar

1. **Dragytan är det största mappade fönstret av den fokuserade klassen** på
   den workspacen, med monitorns yta som fallback, aldrig ett palettfönster.
   Verifierat live: `hypr_drag_area()` returnerar huvudfönstret även när en
   palett har fokus.
2. **Kantskyddet har tre grindar**: det måste vara armerat (markören har
   lämnat marginalen sedan förra gången), 500 ms måste ha gått, och markören
   måste ha rört sig. Efter en kantclutch flyttas startpositionen till punkten
   den warpade till.
3. **`edge_guard` är av som default i keep-läge** och står explicit `"off"` i
   fusion-profilen. Markören är Elias att placera, alltså är kanten hans också.
   Kan slås på per profil.
4. **Räknarna är separata och loggas per gest** på debug-nivå:
   `gesture cursor: N warp(s), N clutch(es), N edge clutch(es), area from window`.

### Verifiering

Replay av hårdvaruinspelningen genom fusion-profilen i 10 sekunder, mot en
fejkad kompositor som rapporterar **palettfönstret** som fokuserat:

```
viewport found (the fix)      warps=0  clutches=0  edge_clutches=0
only the palette exists       warps=0  clutches=0  edge_clutches=0
```

Och med kantskyddet påtvingat `on` i samma omöjliga läge (markören långt
utanför en 300x25-yta, 10 sekunder hårt orbit):

```
edge_clutches = 1     (en per tick hade varit ~1200)
```

Enhetstester för loopfallet: markör 5 px från kanten i keep-läge i 5 sekunder
ger **max en** kantclutch, markör helt utanför ytan likaså, och en markör som
studsar in och ut ur marginalen så fort den kan ger max en per 500 ms.

---

## 6. Femte omgången: orbit i Fusion blev pan

Elias Fusion-pass 2026-09-21: pan (mittenknapp) fungerade, men en vridning av
pucken sköt modellen **rakt i sidled** i stället för att rotera den, och en
enstaka gång roterade den ändå. Kantskyddet var friskt den här gången (14
gester, 0 warps, 0 clutchar, 0 kantclutchar, yta från fönstret), så felet låg
inte i markörhanteringen.

### Vad mätningen visade, steg för steg

**1. Shift når X-klienter, det var aldrig problemet.** `tests/modifier_probe.c`
är ett litet X-fönster som skriver ut modifiermasken på varje event, och
`tests/live_modifier.py` kör en riktig gest mot det. Även med hela kombon i en
enda evdev-ram såg klienten `KeyPress Shift_L` 0,2 ms före `ButtonPress
button=2 state=0x0011`, alltså med ShiftMask satt, och varje `MotionNotify`
under draget bar `0x0211`. Hypotesen att den virtuella enheten saknar
tangentbordsplats i kompositorn stämmer heller inte: `hyprctl devices` listar
`omarchy-spacemouse` både under mice och under Keyboards, och udev sätter
`ID_INPUT_KEYBOARD=1` på noden.

**2. Wine ser shift.** `wine notepad` i Fusions egen prefix, matad med `a`,
sedan `shift+b` i en ram och `shift+b` med 50 ms lead: resultatet blev `aBB`.
Tangentvägen genom Wine är alltså hel.

**3. Fusion gör det ändå inte.** Med Fusion öppet och ViewCube som vittne (den
rör sig när vyn roterar och aldrig när den panorerar) kördes samma drag om och
om igen mot den körande applikationen:

```
gestmotorn, shift+mitten i en enda ram      0 orbit av 9   (4 bekräftade pan)
för hand, shift och mitten i två ramar      3 orbit av 4
för hand, två ramar med 20 ms emellan       4 orbit av 4
för hand, två ramar med 60 ms emellan       4 orbit av 4
enbart mittenknappen (kontroll)             0 orbit, alltid pan
```

Det är hela buggen. Ramen är gränsen: ligger shift och knappen i samma
evdev-ram hinner Fusion aldrig se modifieraren när knappen går ner, och draget
blir en pan. Att det "ibland roterade" var de gånger tidsfönstret råkade falla
rätt.

**4. Släppet var fel åt andra hållet.** Inne i en ram levererar kompositorn
tangenten före knappen oavsett vilken ordning de skrevs i, så den gamla koden
som släppte `[mitten upp, shift upp]` i en ram gav klienten `KeyRelease
Shift_L` **före** `ButtonRelease`: varje orbit slutade som slutet på ett vanligt
mittendrag.

### Ändringen

`modifier_lead_ms` (20 ms default, per profil överskuggbar). Modifierarna går
ner i en egen ram, leaden väntas ut, sedan knappen. Släppet är spegelvänt:
knappen först, modifierarna efter. `0` behåller de två ramarna och släpper bara
väntan. Gester som bara håller en musknapp, alltså alla i webbläsar- och
slicer-profilerna, emitteras precis som förut och väntar på ingenting.
Nödvägen `release_all()` är orörd: där är en fastnad knapp värre än ordningen.

Dessutom loggar `-v` numera vilken grupp som tog pucken:
`gesture orbit holding shift+middle`. Journalen från passet ovan kunde inte
svara på om det ens var orbit som var aktiv, vilket kostade en halv felsökning.

### Verifiering

- `python3 tests/run.py`: **250 tester, alla gröna** (238 plus 12 nya för
  ramordning, lead, spegelvänt släpp, gruppbyte och `release_all`).
- `tests/live_modifier.py` mot X-fönstret, med fixen: shift 20,1 ms före
  knappen, `ButtonPress state=0x0011`, knappen upp medan shift fortfarande är
  nere, `ButtonRelease state=0x0211`. Alla sex kontroller gröna, tre körningar
  i rad.
- Gestmotorn mot körande Fusion efter fixen: **6 orbit av 6** (före: 0 av 9).
- Tjänsten omstartad, `spacemouse-ctl status` friskt, `control.sock` och
  `status.json` på plats.

En sak till mättes och förkastades: en separat virtuell tangentbordsenhet för
modifierarna. Mätningen ovan visar att den kombinerade enheten redan levererar
shift korrekt till X-lagret, så en andra enhet hade lagt till udev-yta,
pekar-arbitrering och en till sak som kan fastna, utan att lösa något.

---

## 7. Sjätte omgången: vridriktning och en zoom som inte fanns

Elias känselpass 2026-09-21 20:34 med ramfixen inne: orbit och pan registreras
friskt i journalen (`gesture orbit holding shift+middle`, `gesture pan holding
middle`), mekaniken sitter. Två saker kvar: vridningen gick åt fel håll, och
zoom via lyft/tryck gjorde ingenting.

### Vridriktningen

Ren smaksak som bara handen kan avgöra, och handen sa spegelvänt.
`ry → dx`-gainen i fusion-profilens orbit är nu `+1.0` i stället för `-1.0`, i
både `profiles.default.json` och Elias live-config. `rx` (tippningen) är orörd,
den klagade han inte på, och `browser-threejs` behåller `-1.0`: Fusion snurrar
kameran runt en pivot medan en Three.js-vy drar i modellen, så samma puckaxel
kräver verkligen olika tecken i de två profilerna. Tecknet är nu fastnaglat i
`test_profiles.py` så en städning inte byter tillbaka det i tysthet.

### Zoomen: hjulet fanns, men nådde aldrig ett helt klick

Stegvis, med proben från förra omgången utbyggd till att räkna knapp 4 och 5
(X-hjulet är knapptryck 4 upp och 5 ner):

**(a) Gesten fajrar.** 1,5 s med `y=300` genom den riktiga gestmotorn gav 176
`wheel()`-anrop och 427 hi-res-enheter. Inga trösklar stoppade något.

**(b) Eventen når fram.** Samma körning gav X-klienten 4 tryck på knapp 4, och
mot körande Fusion zoomade vyn. Transporten genom libinput, Hyprland och
Xwayland är alltså hel.

**(c) Men takten räcker inte, och det är hela buggen.** Ett klick är 120
hi-res-enheter, och **allt som kör på Xwayland reagerar bara på hela klick**.
En webbläsare reagerar på varje enskild enhet, vilket är varför exakt samma
emission kändes bra i Sketchfab 18/9. Med bekvämt halvutslag och en normal kort
tryckning, alltså det handen faktiskt gör:

```
0,4 s vid y=150, wheel_speed 3 (globala)    40 enheter    0 klick    Fusion: 0.00 px ändrade
0,4 s vid y=150, wheel_speed 12 (nya)      160 enheter    1 klick    Fusion: zoomar
0,3 s vid y=120, wheel_speed 3               20 enheter    0 klick
0,5 s vid y=350, wheel_speed 12             678 enheter    5 klick
```

Noll klick betyder att Fusion inte fick ett enda event. Inte en långsam zoom:
ingen zoom. Fusion-profilen har därför `"wheel_speed": 12`, mätt så att en kort
halvtryckning ger ett klick och fullt utslag ger tio i sekunden. Globala 3.0
står kvar, det är siffran webbläsarprofilen trimmades till för hand.

### En riktig emissionsbugg på köpet

`wheel_hi_res: false` emitterade bara klassiska `REL_WHEEL` och inget annat, och
**klassiska hjulevent scrollar ingenting alls**: enheten deklarerar
`REL_WHEEL_HI_RES`, och libinput tar då hjulet från den axeln och ignorerar den
klassiska. Mätt mot Fusion: fem klassiska detents flyttade vyn `0.00` pixlar,
samma fem med 120 enheter var zoomade den. Inställningen betyder nu det den
säger, hela klick i stället för ett mjukt flöde, och varje klick bär sina 120
enheter.

### Hjulet syns i loggen nu

`-v` sa ingenting alls om hjulgester, vilket är precis den information som
saknades för att svara på "fajrade zoomen?". Nu:

```
omarchy-spacemouse: gesture zoom scrolled 4 click(s), 512 unit(s) of 120
omarchy-spacemouse: gesture zoom scrolled 0 click(s), 40 unit(s) of 120
```

Den andra raden är felläget, direkt läsbart: gesten körde och nådde aldrig ett
klick. En axel som bara snuddar dödzonen loggar inget, annars skulle raden
dränka loggen den ska förklara.

### Verifiering

- `python3 tests/run.py`: **259 tester, alla gröna** (250 plus 9 nya: klick-
  takten vid tre och tolv, profilens egen `wheel_speed`, den skickade
  fusion-profilen mot en kort tryckning, loggraderna, dödzonssnuddningen,
  klassiskt läge som bär hi-res, samt teckenlåsen för vridriktningen).
- Samma tryckning mot körande Fusion före och efter: `wheel_speed 3` ger
  `0.00` i vyförändring, `wheel_speed 12` ger 4475. Samma skript, samma
  sekund, enda skillnaden är talet.
- Orbit efter teckenbytet: 4 av 4 mot körande Fusion, ingen regression.
- Replay genom hela daemonprocessen i egen `XDG_RUNTIME_DIR`: hjulraderna
  loggas, tjänstens `control.sock` överlevde.
- Tjänsten omstartad, status friskt.

Lyft av pucken (`+y`) zoomar **ut** i Fusion, tryck ner zoomar in. Känns det
bakvänt är det ett teckenbyte på zoomgestens `gain`, samma sorts ändring som
vridriktningen ovan.

### Öppet fynd: Fusion latchar shift efter en syntetisk orbit

Hygienkontrollen efter passet hittade en sak till, och den är värd att skriva
ner även om den inte går att fixa härifrån. Efter en shift+mitten-gest tror
Fusion att shift fortfarande är nere, så nästa rena mittendrag orbitar i
stället för att panorera. Ett shift-tryck på tangentbordet nollställer det,
ibland krävs två.

Det viktiga: **det är inte ramfixens fel**. Samma mätning kördes med tre
släppordningar, med rent utgångsläge verifierat före varje försök (ett rent
mittendrag som måste panorera):

```
knappen upp, 20 ms, shift upp   (nuvarande)     latch i 1 av 1 giltiga försök
shift upp, 20 ms, knappen upp   (före fixen)    latch i 3 av 3
allt i en ram                   (originalet)    latch i 3 av 3
```

Alla tre latchar lika. Det är alltså ett Wine/Fusion-beteende som fanns hela
tiden, inte en regression, och en ändrad släppordning skulle inte hjälpa. Att
blint skicka en extra shift-tapp efter varje gest vore en dålig generell fix:
för `alt` och `super` öppnar en ensam tapp menyer respektive launchern.

Elias pass 20:34 visade inte problemet trots åtta orbit och sex pan om
vartannat, troligen för att en hand som rör mus och tangentbord mellan
gesterna genererar precis den trafik som synkar om Wines tangentläge.
Reproduktion: `latch_ab.py` i sessionens scratchpad, mätt med ViewCube-masken
och pekaren parkerad på tom yta (Fusions hover-highlight förorenar annars
mätningen).

---

## 8. Sjunde omgången: zoomriktning och hur len hjulvägen kan bli

Elias pass 3: zoomen var spegelvänd och "inte alls smooth".

### Riktningen

`y → wheel`-gainen i fusion-profilens zoom är nu `-1.0`. **Lyft av pucken
zoomar in, tryck ner zoomar ut**, tvärtemot förra rundan. Verifierat mot
körande Fusion: knapp 5 i stället för knapp 4 ut på tråden, och modellen växer
i bild vid lyft.

### Smoothness: tre vägar undersökta

**(a) Drag-zoom i Fusion.** Skulle lösa problemet i grunden, men kräver ett
presetbyte. Fusions default-preset har ingen drag-zoom: uppmätt mot körande
Fusion panorerar `mitten`, `ctrl+mitten`, `alt+mitten` och `ctrl+shift+mitten`
allihop, `shift+mitten` orbitar, och hjulet zoomar. Installationen innehåller
preferensen `PanZoomOrbitShortcuts` med presets Fusion360, Alias, Inventor,
SolidWorks, Tinkercad och PowerMill, och Elias har aldrig ändrat den (den står
inte i `NGlobalOptions.xml`, alltså default). **Inte implementerat**, eftersom
ett byte flyttar hans fysiska mus samtidigt: se rapportens beslutspunkt.

**(b) Jämnare kadens i daemonen.** Mätt mot en riktig X-klient: klicken kommer
redan helt jämnt, 338 ms, 210 ms respektive 153 ms mellan klick beroende på
utslag, med 3 ms jitter eller mindre. Det fanns inga skurar att jämna ut. (Ett
par körningar såg skurartade ut tills mätningen började kasta pass där proben
tappade fokus mitt i, alltså där klicken gick till ett annat fönster.)

**(c) Det som faktiskt gick att förbättra: den mjuka halvan av utslaget.**
Pekaren sparar sina bråkdelar av en pixel, hjulet kan inte röra sig en bråkdel
av ett klick. Därför är just det område där pekaren kryper det område där
hjulet inte gör någonting alls på en sekund. `wheel_curve` ger hjulgrupper en
egen responskurva, default den globala `curve`, och fusion-profilen sätter
1.0:

```
utslag 100 (mjukt)   1.9 -> 3.0 klick/s
utslag 150           3.6 -> 4.8
utslag 200           5.5 -> 6.6
utslag 350 (fullt)  12.0 -> 12.0   (toppen orörd)
```

Uppmätt efter ändringen mot X-klienten: 3.0 klick/s vid utslag 100, gap 338 ms
± 0 ms. Webbläsarprofilen behåller den globala kurvan, den konsumerar varje
enhet och har ingen död zon att lyfta.

**Vad som inte går att fixa härifrån:** zoomsteget per klick är Fusions eget
och finns inte som inställning (genomsökt installationens option-id:n:
`zoomScale`, `isZoomDirectionReversed` och `reverseZoomDirection` finns, men
inget steg eller känslighet). På hjulvägen är alltså zoomhastighet och
jämnhet samma ratt: enda sättet att få stegen tätare är att zooma snabbare.
`wheel_speed` står kvar på 12, eftersom en 0,6-sekunders halvtryckning redan
tar vyn från översikt till närbild.

### Verifiering

- `python3 tests/run.py`: **265 tester, alla gröna** (259 plus 6 nya för
  kurvan, att fullt utslag är oförändrat, `reshape`, jämna mellanrum, samt
  riktnings- och kurvlåsen i profiltesterna).
- Kadens mot X-klient efter ändringen: 338 ± 0 ms vid mjukt utslag, 153 ± 2 ms
  vid hårt.
- Zoomriktning och zoomeffekt verifierad mot körande Fusion, före/efter-bilder.
- Tjänsten omstartad, status friskt.

---

## 9. Det som återstår

Uinput-regeln är **inlagd och verifierad**: `/dev/uinput` är `crw-rw---- root uucp`,
den virtuella enheten dyker upp som `/dev/input/event26` ("Omarchy SpaceMouse")
och allt i avsnitt 3 ovan kördes mot riktig kärna. Det som fanns i den här
listan tidigare är därmed avklarat.

Kvar finns en sak, och den kräver root:

### Musen och pucken samtidigt, i handen

Udev-regeln är inlagd (Elias la in den under passet) och mekaniken är
verifierad mot riktig kärna med en stand-in-mus. Kvar är själva handgreppet:
håll musen i ena handen och pucken i den andra i en 3D-vy och rör båda
samtidigt. Musens rörelse ska inte längre smeta in i puckens drag, medan
musknappar och hjul fortfarande fungerar.

`spacemouse-ctl pointer shared` är nödutgången om något känns fel, och den
släpper musen omedelbart. Samma sak finns som knapp i panelen.

### Fusion-profilen med Fusion öppet

Fusion kördes av Elias under hela passet, så inga syntetiska drag skickades mot
den. `cursor: keep` och `clutch: off` är verifierade i torrkörning och i
enhetstester, men hur pivoten känns i handen kan bara han avgöra. Lägg markören
på modellen först, ta sedan i pucken.


### Det bara Elias kan avgöra

Hastighet och kurva är smaksak och står som exakta rattar i README:
`pointer_speed` (900, ändra 200 i taget) och `curve` (1.3, ändra 0.2 i taget) i
`~/.config/omarchy-spacemouse/profiles.json`. Filen läses om inom en sekund, så
det går att justera med 3D-vyn öppen.

Fusion-profilens FIT-knapp står avsiktligt tom: binder han en tangent för
"fit to view" i Fusion är det bara att skriva in den i `fit_key`.
