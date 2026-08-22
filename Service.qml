import QtQuick
import Quickshell
import Quickshell.Io

Item {
  id: root
  visible: false
  width: 0
  height: 0

  property var shell: null
  property var manifest: null

  readonly property string pluginId: "apurvanpradhan.lookout"
  readonly property string pluginDir: manifest && manifest.__sourceDir
    ? String(manifest.__sourceDir) : ""
  readonly property string pyPath: pluginDir + "/lookout.py"

  readonly property var defaultSettingValues: ({ refreshIntervalSec: 5, notifyOnChange: "On" })
  property var settings: defaultSettingValues
  function applySettings(values) {
    var next = {}
    for (var key in defaultSettingValues) next[key] = defaultSettingValues[key]
    var source = values || {}
    for (var name in source) if (source[name] !== undefined && source[name] !== null) next[name] = source[name]
    if (JSON.stringify(next) !== JSON.stringify(settings)) {
      settings = next
      pollTimer.interval = Math.max(2, Number(settings.refreshIntervalSec) || 5) * 1000
    }
  }

  property var servers: []
  property var labels: ({})
  property var paths: ({})
  readonly property int count: servers.length

  // Set by Panel.qml while a rename/URL field is open: a completed scan is
  // parked in pendingScan instead of rebuilding the Repeater mid-edit.
  property bool editingActive: false
  property var pendingScan: null

  // ---- scan chain ------------------------------------------------------
  // Self-healing: advances from `exited` AND `runningChanged` (Quickshell
  // never emits `exited` on FailedToStart), and the watchdog SIGKILLs any
  // child that outlives its slot so a hung scan cannot stop polling forever.
  property bool scanActive: false      // a scan child is running
  property bool scanPending: false     // a refresh was requested mid-scan

  function refresh() {
    if (root.scanActive) { root.scanPending = true; return }
    root.scanPending = false
    root.scanActive = true
    scanWatchdog.restart()
    scanProcess.command = ["python3", root.pyPath, "scan",
      "--notify", String(settings.notifyOnChange) === "On" ? "on" : "off"]
    scanProcess.running = true
  }

  function finishScan() {
    if (!root.scanActive) return
    root.scanActive = false
    scanWatchdog.stop()
    if (root.scanPending) {
      root.scanPending = false
      Qt.callLater(root.refresh)   // satisfy the request that arrived mid-scan
      return
    }
    pollTimer.start()   // fixed gap between polls, no overlap
  }

  // ---- action chain ----------------------------------------------------
  property var actionQueue: []
  property bool actionActive: false   // an action child is running

  function runAction(action, args) {
    var cmd = ["python3", root.pyPath, action]
    if (args) cmd = cmd.concat(args)
    actionQueue.push(cmd)
    if (!actionActive) runNextAction()
  }

  function runNextAction() {
    if (actionQueue.length === 0) return
    actionActive = true
    actionWatchdog.restart()
    actionProcess.command = actionQueue.shift()
    actionProcess.running = true
  }

  function finishAction() {
    if (!actionActive) return
    actionActive = false
    actionWatchdog.stop()
    if (actionQueue.length > 0) runNextAction()
    else root.refresh()   // kill/restart/rename/path results appear promptly
  }

  Process {
    id: scanProcess
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applyScan(text)
    }
    onExited: root.finishScan()
    onRunningChanged: if (!scanProcess.running) root.finishScan()
  }

  Process {
    id: actionProcess
    onExited: root.finishAction()
    onRunningChanged: if (!actionProcess.running) root.finishAction()
  }

  Timer { id: scanWatchdog; interval: 60000; repeat: false; onTriggered: scanProcess.signal(9) }
  Timer { id: actionWatchdog; interval: 30000; repeat: false; onTriggered: actionProcess.signal(9) }

  function applyScan(output) {
    var data = null
    try { data = JSON.parse(output) } catch (e) {
      console.warn("lookout: bad scan json: " + output)
      return
    }
    // Failed discovery (ss hiccup, probe crash): keep the last good state so
    // the bar does not flash empty or announce every server stopped.
    if (!data || data.ok === false) return
    if (root.editingActive) { root.pendingScan = data; return }
    applyScanData(data)
  }

  function applyScanData(data) {
    root.servers = (data && data.servers) || []
    root.labels = (data && data.labels) || {}
    root.paths = (data && data.paths) || {}
  }

  onEditingActiveChanged: {
    if (!root.editingActive && root.pendingScan) {
      var data = root.pendingScan
      root.pendingScan = null
      applyScanData(data)   // deferred scan lands once the edit ends
    }
  }

  Timer {
    id: pollTimer
    interval: 5000
    repeat: false
    triggeredOnStart: false
    onTriggered: root.refresh()
  }

  // First scan right after the loader hands us the manifest (immediate);
  // `onCompleted` fires before `manifest` is assigned, so it only backs the
  // poll loop.
  property bool manifestStarted: false
  onManifestChanged: {
    if (root.manifestStarted || !manifest || !manifest.__sourceDir) return
    root.manifestStarted = true
    Qt.callLater(root.refresh)
  }

  Component.onCompleted: { pollTimer.start() }
}