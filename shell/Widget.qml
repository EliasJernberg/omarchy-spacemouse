import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Bar widget for the omarchy-spacemouse daemon.
//
// Everything it shows comes from the daemon's status file, which is rewritten
// whenever something changes (and at least every couple of seconds), so the
// widget never has to ask a process for anything. Everything it does goes out
// through spacemouse-ctl, so the widget and the command line share one path
// into the daemon.
//
//   left click   enable / disable
//   right click  open the panel: profile override, gestures, health
Panel {
  id: root
  moduleName: "jernberg.spacemouse"
  manageIpc: false

  // ---------------------------------------------------------------- state

  readonly property string runtimeDir: String(Quickshell.env("XDG_RUNTIME_DIR") || "")
  readonly property string statusPath: runtimeDir === "" ? "" : runtimeDir + "/omarchy-spacemouse/status.json"
  // bin/spacemouse-ctl inside this plugin directory: the widget drives the
  // same daemon as the copy on PATH, and works before one is symlinked.
  readonly property string ctlPath: String(Qt.resolvedUrl("../bin/spacemouse-ctl")).replace("file://", "")

  property var status: null
  property double lastRead: 0
  property double nowSeconds: 0

  readonly property bool daemonAlive: status !== null && (nowSeconds - Number(status.updated || 0)) < 10
  readonly property bool daemonEnabled: daemonAlive && status.enabled === true
  readonly property string profileName: daemonAlive ? String(status.profile || "") : ""
  readonly property string profileType: daemonAlive ? String(status.profile_type || "") : ""
  readonly property string windowClass: daemonAlive ? String(status.window_class || "") : ""
  readonly property string gestureName: daemonAlive ? String(status.gesture || "") : ""
  readonly property bool manualMode: daemonAlive && String(status.mode || "auto") === "manual"
  readonly property var profiles: daemonAlive && Array.isArray(status.profiles) ? status.profiles : []

  readonly property string uinputState: daemonAlive ? String(status.uinput || "") : ""
  readonly property bool uinputBlocked: uinputState === "denied" || uinputState === "error"

  // Pointer arbitration: "proxied (<mouse>)" while the physical mice are held
  // during gestures, "shared (...)" with the reason when they are not.
  readonly property string pointerState: daemonAlive ? String(status.pointer || "-") : "-"
  readonly property bool pointerProxied: pointerState.indexOf("proxied") === 0

  // The daemon is not emitting anything in these two, which the bar shows by
  // dimming the icon: "native" means the application talks to spacenavd on its
  // own, "off" means the profile asked for silence.
  readonly property bool profileQuiet: profileType === "native" || profileType === "off"

  readonly property string icon: "7"                     // cube outline
  readonly property string heroIcon: "E"                 // axis arrow
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color barFg: bar ? bar.barForeground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color dim: Qt.darker(foreground, 1.55)

  readonly property string barLabel: {
    if (!daemonAlive) return icon
    if (!daemonEnabled) return icon + "  off"
    if (profileName === "") return icon
    return icon + "  " + profileName
  }

  readonly property string tooltip: {
    if (statusPath === "") return "SpaceMouse: no XDG_RUNTIME_DIR"
    if (!daemonAlive) return "SpaceMouse daemon is not running"
    if (!daemonEnabled) return "SpaceMouse disabled - click to enable"
    var line = "SpaceMouse: " + profileName + " (" + profileType + ")"
    if (windowClass !== "") line += "\n" + windowClass
    if (uinputBlocked) line += "\nuinput unavailable"
    return line
  }

  // ---------------------------------------------------------------- actions

  function ctl(command, argument) {
    if (ctlPath === "") return
    var argv = ["bash", "-lc", 'exec "$@"', "bash", ctlPath, command]
    if (argument !== undefined && argument !== "") argv.push(String(argument))
    Quickshell.execDetached(argv)
    refreshSoon.restart()
  }

  function parseStatus(text) {
    try {
      var parsed = JSON.parse(String(text || ""))
      root.status = (parsed && typeof parsed === "object") ? parsed : null
    } catch (error) {
      root.status = null
    }
    root.nowSeconds = Date.now() / 1000
  }

  // ---------------------------------------------------------------- plumbing

  FileView {
    id: statusFile
    path: root.statusPath
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.parseStatus(text())
    onLoadFailed: {
      root.status = null
      root.nowSeconds = Date.now() / 1000
    }
  }

  // The file watch catches every write, but the "is it still alive" check is a
  // clock comparison, so the widget needs a heartbeat of its own as well.
  Timer {
    interval: 2000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: {
      root.nowSeconds = Date.now() / 1000
      if (root.statusPath !== "") statusFile.reload()
    }
  }

  // A command changes the status file a moment after it is sent.
  Timer {
    id: refreshSoon
    interval: 250
    repeat: false
    onTriggered: if (root.statusPath !== "") statusFile.reload()
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.barLabel
    fontSize: Style.font.caption
    horizontalMargin: 6
    tooltipText: root.tooltip
    dimmed: !root.daemonAlive || !root.daemonEnabled || root.profileQuiet
    active: root.uinputBlocked
    useActiveColor: true
    onPressed: function (pressedButton) {
      if (pressedButton === Qt.RightButton) root.toggle()
      else if (pressedButton === Qt.MiddleButton) root.ctl("auto")
      else root.ctl("toggle")
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(360))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(620))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()

      ScrollView {
        id: scrollArea
        anchors.fill: parent
        clip: true
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: column.implicitHeight > height ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff

        Column {
          id: column
          width: scrollArea.availableWidth
          spacing: Style.space(12)

          // ---------- hero ----------
          Item {
            width: parent.width
            implicitHeight: Math.max(hero.implicitHeight, heroText.implicitHeight)

            Text {
              id: hero
              textFormat: Text.PlainText
              text: root.heroIcon
              color: root.daemonEnabled && !root.profileQuiet ? root.foreground : root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.display
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
            }

            Column {
              id: heroText
              anchors.left: hero.right
              anchors.leftMargin: Style.space(14)
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(2)

              Text {
                text: "SpaceMouse"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.title
                font.bold: true
                elide: Text.ElideRight
                width: parent.width
              }

              Text {
                textFormat: Text.PlainText
                text: {
                  if (!root.daemonAlive) return "DAEMON NOT RUNNING"
                  if (!root.daemonEnabled) return "DISABLED"
                  if (root.profileType === "native") return "NATIVE - THE APP READS THE PUCK ITSELF"
                  if (root.profileType === "off") return "QUIET IN THIS APPLICATION"
                  return (root.gestureName !== "" ? root.gestureName.toUpperCase() : "READY")
                }
                color: root.uinputBlocked ? root.urgent : Qt.darker(root.foreground, 1.4)
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
                elide: Text.ElideRight
                width: parent.width
              }
            }
          }

          PanelSeparator { width: parent.width }

          // ---------- facts ----------
          Column {
            width: parent.width
            spacing: Style.space(4)

            Repeater {
              model: [
                { key: "Window", value: root.windowClass !== "" ? root.windowClass : "-" },
                { key: "Profile", value: root.profileName !== "" ? root.profileName + "  (" + root.profileType + ")" : "-" },
                { key: "Mode", value: root.manualMode ? "manual override" : "follows focus" },
                { key: "spacenavd", value: root.daemonAlive ? String(root.status.spnav || "-") : "-" },
                { key: "uinput", value: root.daemonAlive ? String(root.status.uinput || "-") : "-" },
                { key: "pointer", value: root.pointerState }
              ]

              Item {
                width: column.width
                implicitHeight: Math.max(factKey.implicitHeight, factValue.implicitHeight)

                Text {
                  id: factKey
                  textFormat: Text.PlainText
                  text: modelData.key
                  color: Qt.darker(root.foreground, 1.5)
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  anchors.left: parent.left
                  anchors.verticalCenter: parent.verticalCenter
                }

                Text {
                  id: factValue
                  textFormat: Text.PlainText
                  text: modelData.value
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  horizontalAlignment: Text.AlignRight
                  elide: Text.ElideRight
                  anchors.right: parent.right
                  anchors.left: factKey.right
                  anchors.leftMargin: Style.space(10)
                  anchors.verticalCenter: parent.verticalCenter
                }
              }
            }
          }

          Text {
            width: parent.width
            visible: root.uinputBlocked
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
            text: "No access to /dev/uinput. Add the udev rule from the README; the daemon picks it up within ten seconds."
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          PanelSeparator { width: parent.width }

          PanelSectionHeader {
            text: "Profile"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Column {
            width: parent.width
            spacing: Style.space(2)

            Button {
              width: parent.width
              leftAlign: true
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              foreground: root.foreground
              text: "Follow the focused window"
              iconText: root.manualMode ? "" : ""
              selected: !root.manualMode
              onClicked: root.ctl("auto")
            }

            Repeater {
              model: root.profiles

              Button {
                width: parent.width
                leftAlign: true
                fontFamily: root.fontFamily
                fontSize: Style.font.caption
                foreground: root.foreground
                text: String(modelData.name) + "   " + String(modelData.type)
                iconText: (root.manualMode && String(modelData.name) === root.profileName) ? "" : ""
                selected: root.manualMode && String(modelData.name) === root.profileName
                onClicked: root.ctl("profile", String(modelData.name))
              }
            }
          }

          PanelSeparator { width: parent.width }

          Row {
            width: parent.width
            spacing: Style.space(6)

            Button {
              bordered: true
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              foreground: root.foreground
              text: root.daemonEnabled ? "Disable" : "Enable"
              onClicked: root.ctl("toggle")
            }

            Button {
              bordered: true
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              foreground: root.foreground
              text: "Reload profiles"
              onClicked: root.ctl("reload")
            }

            Button {
              bordered: true
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              foreground: root.foreground
              // The escape hatch, on the panel as well as the command line:
              // one click hands the physical mice straight back.
              text: root.pointerProxied ? "Share the mouse" : "Take the mouse"
              onClicked: root.ctl("pointer", root.pointerProxied ? "shared" : "proxied")
            }
          }
        }
      }
    }
  }
}
