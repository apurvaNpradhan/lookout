# Lookout

Lookout monitors local development servers and puts them in the Omarchy bar. Open a server in your browser, terminal, editor, or file manager; restart or stop it; rename it; and configure a URL path for browser links.

## Features

- Detects common Node, Python, Ruby, Go, Bun, Deno, Java, PHP, and framework dev servers.
- Shows the listening port, project directory, process uptime, CPU, memory, and health.
- Opens the server URL, project terminal, editor, or file manager from one popup.
- Restarts a server with its original argument boundaries.
- Stops one server or all listed servers.
- Sends optional start/stop desktop notifications.
- Stores labels, URL suffixes, and scan state outside the plugin directory.
- Uses only Python 3's standard library and Omarchy/Quickshell APIs.

## Install

Requires Omarchy with the Quickshell plugin system, Python 3, `ss` from iproute2, and the usual desktop commands (`xdg-open` plus an installed terminal/editor/file manager for those actions).

```bash
omarchy plugin add https://github.com/apurvaNpradhan/lookout.git --enable
```

Lookout appears in the bar after installation. Its default scan interval is five seconds and can be changed in the plugin settings.

## Development

The source files are:

- `manifest.json` — Omarchy plugin metadata and settings schema.
- `Service.qml` — polling and action lifecycle.
- `Panel.qml` — bar button and popup UI.
- `lookout.py` — server discovery and actions.

Validate before publishing or installing:

```bash
omarchy plugin validate .
python3 lookout.py selftest
```

On the current Quickshell build, restart the shell after changing QML files so the new component is loaded:

```bash
omarchy restart shell
```

## Remove

```bash
omarchy plugin remove apurvanpradhan.lookout
```

Lookout stores its preferences in `$XDG_STATE_HOME/omarchy/lookout`, or `~/.local/state/omarchy/lookout` when `XDG_STATE_HOME` is unset. Remove that directory separately if you also want to delete saved labels, URL suffixes, and notification state:

```bash
rm -rf "${XDG_STATE_HOME:-$HOME/.local/state}/omarchy/lookout"
```

The panel only offers signals for PIDs currently detected as development servers. Lookout does not modify project files or overwrite Omarchy configuration without an explicit action.

## License

MIT. See [`LICENSE`](LICENSE).

Marketplace listing: [omarchyplugins.com](https://omarchyplugins.com/).
