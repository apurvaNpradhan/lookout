import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "apurvanpradhan.lookout"
  ipcTarget: "lookout"
  manageIpc: true

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property color surface: Color.popups.background
  readonly property color danger: bar ? bar.urgent : Color.urgent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property var service: bar && bar.shell ? bar.shell.serviceFor("apurvanpradhan.lookout") : null
  readonly property var servers: service ? service.servers : []
  readonly property var labels: service ? service.labels : ({})
  readonly property var paths: service ? service.paths : ({})

  // True while any server row's inline rename/URL field is open, so the
  // PanelKeyCatcher (which otherwise swallows all keys) lets the field type.
  property bool editingActive: false
  function scanEditing() {
    var any = false
    for (var i = 0; i < serverRepeater.count; i++) {
      var item = serverRepeater.itemAt(i)
      if (item && item.editing !== "") { any = true; break }
    }
    editingActive = any
    if (service) service.editingActive = any   // Service defers scans mid-edit
  }

  function displayName(server) {         // custom label override, like Lookout
    var key = "port_" + server.port
    return labels[key] ? String(labels[key]) : String(server.label)
  }
  function formatUptime(secs) {          // "< 1m", "45m", "2h 5m", "3d 4h"
    if (!secs) return ""
    if (secs < 60) return "< 1m"
    if (secs < 3600) return Math.floor(secs/60) + "m"
    if (secs < 86400) {
      var h = Math.floor(secs/3600)
      var m = Math.floor((secs%3600)/60)
      return m ? h+"h "+m+"m" : h+"h"
    }
    return Math.floor(secs/86400) + "d " + Math.floor((secs%86400)/3600) + "h"
  }
  function healthColor(status) {
    if (status === "green") return "#3fb950"
    if (status === "yellow") return "#d29922"
    return Qt.darker(foreground, 1.8)
  }
  function clampScroll(y) {
    if (!serverList) return 0
    var max = Math.max(0, serverList.contentHeight - serverList.height)
    return Math.max(0, Math.min(y, max))
  }

  function openServer(port) { if (service) service.runAction("open", [String(port)]) }

  // ------------------------------------------------------------- kill all

  property bool killAllArmed: false
  function armKillAll() { killAllArmed = true; rearmTimer.restart() }
  function confirmedKillAll() {
    killAllArmed = false
    var pids = []
    for (var i in root.servers) pids.push(String(root.servers[i].pid))
    if (service && pids.length > 0) service.runAction("kill-all", ["--pids"].concat(pids))
  }
  Timer { id: rearmTimer; interval: 3000; onTriggered: root.killAllArmed = false }

  // ------------------------------------------------------------- settings

  function pushSettings() { if (service) service.applySettings(root.settings) }
  onSettingsChanged: pushSettings()

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: {
    if (root.opened && service) service.refresh()
    if (!root.opened) {
      for (var i = 0; i < serverRepeater.count; i++) {
        var item = serverRepeater.itemAt(i)
        if (item) item.editing = ""
      }
      root.scanEditing()
    }
  }

  Component.onCompleted: pushSettings()

  // Bar button: ">_" glyph + count, like Lookout's icon. The count rides in the
  // text (">_ 3"); a separate badge overlay would be redundant.
  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.servers.length > 0 ? ">_ " + root.servers.length : ">_"
    tooltipText: root.servers.length + (root.servers.length === 1 ? " dev server" : " dev servers")
    active: root.servers.length > 0
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton || buttonCode === Qt.MiddleButton) return
      root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(430))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(560))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: root.editingActive

      onCloseRequested: root.close()
      onMoveRequested: function(dx, dy) {
        if (dy !== 0) serverList.contentY = root.clampScroll(serverList.contentY + dy * Style.space(56))
      }
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onDeleteRequested: if (root.servers.length > 1) root.armKillAll()

      Flickable {
        id: serverList
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: serverList.width
          spacing: Style.space(6)

          // ---- server rows ----
          Repeater {
            id: serverRepeater
            model: root.servers

            delegate: ServerRow {
              required property var modelData
              server: modelData
              width: column.width
            }
          }

          // ---- footer ----
          PanelSeparator {
            visible: root.servers.length > 0
            width: column.width
            foreground: root.foreground
          }

          Item {
            visible: root.servers.length > 0
            width: column.width
            implicitHeight: Math.max(countText.implicitHeight, killAllButton.implicitHeight)

            Text {
              id: countText
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              text: root.servers.length + " server(s)"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
            }

            Button {
              id: killAllButton
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              visible: root.servers.length > 1
              text: root.killAllArmed ? "Confirm kill all?" : "Kill All"
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              foreground: root.danger
              selected: root.killAllArmed
              onClicked: root.killAllArmed ? root.confirmedKillAll() : root.armKillAll()
            }
          }

          Text {
            visible: root.servers.length === 0
            width: column.width
            text: "No dev servers running"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            horizontalAlignment: Text.AlignHCenter
            topPadding: Style.space(24)
            bottomPadding: Style.space(24)
          }
        }
      }
    }
  }

  // Compact row buttons: smaller type and padding than the default control so
  // the five actions fit the 430px panel without wrapping.
  component ActionButton: Button {
    fontFamily: root.fontFamily
    fontSize: Style.font.bodySmall
    horizontalPadding: Style.space(7)
    verticalPadding: Style.space(3)
  }

  // ---- inline component: one server row ----
  component ServerRow: Rectangle {
    id: row
    required property var server
    property string editing: ""        // "", "label", "path"
    property bool hovered: false

    radius: Style.cornerRadius
    color: row.hovered ? Util.alpha(root.foreground, 0.06) : "transparent"
    height: inner.implicitHeight + Style.space(8)

    HoverHandler {
      onHoveredChanged: row.hovered = hovered
    }

    Column {
      id: inner
      anchors { left: parent.left; right: parent.right; margins: Style.space(8) }
      spacing: Style.space(4)

      RowLayout {
        width: parent.width
        spacing: Style.space(6)

        Rectangle {
          Layout.alignment: Qt.AlignVCenter
          implicitWidth: 8
          implicitHeight: 8
          radius: 4
          color: root.healthColor(row.server.health)
        }

        Text {
          Layout.fillWidth: true
          Layout.alignment: Qt.AlignVCenter
          text: root.displayName(row.server)
            + (row.server.appName ? " [" + row.server.appName + "]" : "")
            + "  :" + row.server.port
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          font.bold: true
          elide: Text.ElideMiddle
        }

        Item { width: 6; height: 1 }

        Text {
          Layout.alignment: Qt.AlignVCenter
          text: root.formatUptime(row.server.uptimeSec)
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
        }
      }

      // meta line: path · CPU · RAM
      Text {
        visible: text !== ""
        width: inner.width
        text: [row.server.projectPath,
               (row.server.cpu != null ? "CPU " + Number(row.server.cpu).toFixed(1) + "%" : ""),
               (row.server.memMB ? row.server.memMB + " MB" : "")]
              .filter(Boolean).join("  ·  ")
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        elide: Text.ElideMiddle
      }

      // actions, primary row
      Row {
        spacing: Style.spacing.xs
        ActionButton { text: "Open"; onClicked: root.openServer(row.server.port) }
        ActionButton {
          text: "Terminal"
          enabled: !!row.server.projectPath
          opacity: enabled ? 1 : 0.45
          onClicked: root.service.runAction("term", [row.server.projectPath])
        }
        ActionButton {
          text: "Editor"
          enabled: !!row.server.projectPath
          opacity: enabled ? 1 : 0.45
          onClicked: root.service.runAction("edit", [row.server.projectPath])
        }
        ActionButton {
          text: "Files"
          enabled: !!row.server.projectPath
          opacity: enabled ? 1 : 0.45
          onClicked: root.service.runAction("fm", [row.server.projectPath])
        }
        ActionButton {
          text: "Restart"
          enabled: !!(row.server.projectPath && row.server.argv && row.server.argv.length > 0)
          opacity: enabled ? 1 : 0.45
          onClicked: root.service.runAction("restart",
            [String(row.server.pid), row.server.projectPath, JSON.stringify(row.server.argv)])
        }
      }

      // actions, secondary row: rename / url / kill
      Row {
        spacing: Style.spacing.xs
        ActionButton {
          text: row.editing === "label" ? "Cancel" : "Rename"
          onClicked: row.editing = row.editing === "label" ? "" : "label"
        }
        ActionButton {
          text: row.editing === "path" ? "Cancel" : "URL"
          onClicked: row.editing = row.editing === "path" ? "" : "path"
        }
        Item { width: 4; height: 1 }
        ActionButton {
          text: "Kill"
          foreground: root.danger
          onClicked: root.service.runAction("kill", [String(row.server.pid)])
        }
      }

      // inline edit: custom label
      Row {
        visible: row.editing === "label"
        width: inner.width
        spacing: Style.spacing.xs
        onVisibleChanged: if (visible) labelField.forceActiveFocus()

        TextField {
          id: labelField
          width: parent.width - saveLabelButton.implicitWidth - parent.spacing
          text: root.labels["port_" + row.server.port] || row.server.label
          Keys.onEscapePressed: row.editing = ""
          onAccepted: {
            root.service.runAction("label", [String(row.server.port), labelField.text])
            row.editing = ""
          }
        }
        ActionButton {
          id: saveLabelButton
          text: "Save"
          onClicked: {
            root.service.runAction("label", [String(row.server.port), labelField.text])
            row.editing = ""
          }
        }
      }

      // inline edit: custom URL path suffix
      Row {
        visible: row.editing === "path"
        width: inner.width
        spacing: Style.spacing.xs
        onVisibleChanged: if (visible) pathField.forceActiveFocus()

        TextField {
          id: pathField
          width: parent.width - savePathButton.implicitWidth - parent.spacing
          text: root.paths["port_" + row.server.port] || ""
          placeholderText: "/api/docs"
          Keys.onEscapePressed: row.editing = ""
          onAccepted: {
            root.service.runAction("path", [String(row.server.port), pathField.text])
            row.editing = ""
          }
        }
        ActionButton {
          id: savePathButton
          text: "Save"
          onClicked: {
            root.service.runAction("path", [String(row.server.port), pathField.text])
            row.editing = ""
          }
        }
      }
    }

    onEditingChanged: root.scanEditing()
  }
}