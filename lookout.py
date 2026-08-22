#!/usr/bin/env python3
"""Lookout — dev-server watcher for the Omarchy shell. Detection and actions
only; the UI lives in Service.qml / Panel.qml. Python 3 stdlib only.

CLI: python3 <plugindir>/lookout.py <command> [args...]

Commands: scan, open, fm, term, edit, kill, kill-all, restart, label, path
`scan` is the only stdout writer; everything else is silent.
"""
import fcntl
import json
import os
import re
import shutil
import signal
import ssl
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
# CRITICAL: state must NOT live in the plugin dir — omarchy-shell inotify-
# watches the whole plugins dir and reloads a plugin on ANY write there.
# A prefs.json written every scan would cause a permanent reload loop.
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME") or
                         os.path.join(os.path.expanduser("~"), ".local/state"),
                         "omarchy", "lookout")
PREFS_PATH = os.path.join(STATE_DIR, "prefs.json")
LOCK_PATH = os.path.join(STATE_DIR, ".prefs.lock")
DEVNULL = subprocess.DEVNULL
HTTPS_CTX = ssl._create_unverified_context()

# Tier-2 classification only applies to these well-known dev ports.
DEV_PORTS = {3000, 3001, 3002, 3003, 3004, 3005, 4000, 4200, 4321, 5000,
             5001, 5173, 5174, 5500, 6006, 8000, 8001, 8080, 8081, 8443,
             8888, 9000, 9090, 24678}

# Lookout's classification table, verbatim. cmd = lowered full command line,
# name = lowered process name from ss. First Tier-1 hit wins.
TIER1 = [
    ("Next.js",       lambda c, n: "next" in c and ("dev" in c or "start" in c)),
    ("Next.js",       lambda c, n: "next-server" in c or "next-router-worker" in c),
    ("Vite",          lambda c, n: "vite" in c),
    ("Webpack",       lambda c, n: "webpack" in c and "serve" in c),
    ("Webpack",       lambda c, n: "webpack-dev-server" in c),
    ("React Scripts", lambda c, n: "react-scripts" in c and "start" in c),
    ("Angular",       lambda c, n: "ng serve" in c or "@angular" in c),
    ("Nuxt",          lambda c, n: "nuxt" in c),
    ("SvelteKit",     lambda c, n: "svelte-kit" in c or ("svelte" in c and "dev" in c)),
    ("Remix",         lambda c, n: "remix" in c and "dev" in c),
    ("Astro",         lambda c, n: "astro" in c and "dev" in c),
    ("Parcel",        lambda c, n: "parcel" in c),
    ("Turbopack",     lambda c, n: "turbopack" in c),
    ("esbuild",       lambda c, n: "esbuild" in c and "serve" in c),
    ("Flask",         lambda c, n: "flask" in c),
    ("Django",        lambda c, n: "manage.py" in c and "runserver" in c),
    ("Django",        lambda c, n: "django" in c),
    ("Uvicorn",       lambda c, n: "uvicorn" in c),
    ("Gunicorn",      lambda c, n: "gunicorn" in c),
    ("FastAPI",       lambda c, n: "fastapi" in c),
    ("Python HTTP",   lambda c, n: "http.server" in c),
    ("Rails",         lambda c, n: "rails" in c and "server" in c),
    ("Rails",         lambda c, n: "bin/rails" in c),
    ("Puma",          lambda c, n: "puma" in c),
    ("Hugo",          lambda c, n: "hugo" in c and "server" in c),
    ("Jekyll",        lambda c, n: "jekyll" in c and "serve" in c),
    ("Gatsby",        lambda c, n: "gatsby" in c and "develop" in c),
    ("Eleventy",      lambda c, n: "eleventy" in c and "--serve" in c),
    ("PHP Server",    lambda c, n: "php" in c and "-s" in c),
    ("Air (Go)",      lambda c, n: "air" in c and n == "air"),
    ("Cargo Watch",   lambda c, n: "cargo" in c and "watch" in c),
    ("live-server",   lambda c, n: "live-server" in c),
    ("http-server",   lambda c, n: "http-server" in c),
    ("Bun Dev",       lambda c, n: "bun" in c and "dev" in c),
    ("Deno",          lambda c, n: "deno" in c and ("serve" in c or "dev" in c)),
    ("NestJS",        lambda c, n: "nest" in c and "start" in c),
    ("Nodemon",       lambda c, n: "nodemon" in c),
    ("TS Node",       lambda c, n: "ts-node" in c or "tsx" in c),
]

TIER2 = [
    ("Node",   lambda c, n: n == "node" or c.startswith("node ") or " /node " in c),
    ("Python", lambda c, n: "python" in n or "python" in c),
    ("Ruby",   lambda c, n: n == "ruby" or "ruby" in c),
    ("Go",     lambda c, n: n == "go" or c.startswith("go ")),
    ("Bun",    lambda c, n: n == "bun" or c.startswith("bun ")),
    ("Deno",   lambda c, n: n == "deno" or c.startswith("deno ")),
    ("Java",   lambda c, n: "java" in n or "java" in c),
    ("PHP",    lambda c, n: "php" in n or "php" in c),
]

# Terminals, in preference order: (binary, flag list, how the path is passed).
# kind "path" = separate argument; "joined" = glued onto the flag
# (ghostty wants --working-directory=PATH); "shell" = xterm receives a
# command string instead of a path.
TERMINALS = [
    ("foot",      ("--working-directory",),  "path"),
    ("kitty",     ("--directory",),          "path"),
    ("alacritty", ("--working-directory",),  "path"),
    ("ghostty",   ("--working-directory=",), "joined"),
    ("wezterm",   ("start", "--cwd"),        "path"),
    ("konsole",   ("--workdir",),            "path"),
    ("xterm",     ("-e",),                   "shell"),
]

EDITOR_FALLBACKS = ["code", "codium", "zed", "subl", "hx", "nvim", "vim"]

NOT_LIKELY_PROJECT = {"users", "usr", "opt", "bin", "lib", "tmp", "var",
                      "node_modules", "."}


def shq(s):
    """Lookout's shellEscape: single-quote for the shell."""
    return "'" + str(s or "").replace("'", "'\\''") + "'"


def run_detached(command):
    """Launch a fully detached shell command (own session, no stdio)."""
    try:
        subprocess.Popen(["setsid", "sh", "-c", command],
                         start_new_session=True,
                         stdout=DEVNULL, stderr=DEVNULL)
    except Exception:
        pass


# ------------------------------------------------------------------ prefs
#
# prefs.json lives in $XDG_STATE_HOME/omarchy/lookout — outside the watched
# plugins dir (see STATE_DIR note above). The lock file is never modified
# (no truncate); the tmp file + os.replace writes atomically, only when a
# value actually changed.

def _load_prefs():
    default = {"labels": {}, "paths": {}, "lastPorts": None, "lastHealth": {}}
    try:
        with open(PREFS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("labels", {})
            data.setdefault("paths", {})
            # None = no snapshot taken yet (fresh state); [] = snapshot with
            # zero servers. Only a list triggers start/stop notifications.
            data.setdefault("lastPorts", None)
            data.setdefault("lastHealth", {})
            return data
    except Exception:
        pass
    return default


@contextmanager
def locked_prefs():
    """Read-modify-write prefs.json under a lock; atomic tmp + os.replace.

    Persists only when a value actually changed, so a steady-state scan
    never touches the file.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        prefs = _load_prefs()
        before = json.dumps(prefs, sort_keys=True)
        yield prefs
        if json.dumps(prefs, sort_keys=True) != before:
            tmp = os.path.join(STATE_DIR, ".prefs.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(prefs, f, indent=2)
            os.replace(tmp, PREFS_PATH)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


# ------------------------------------------------------------- detection

def parse_ss_line(line):
    """One `ss -tlnp` line -> [(pid, port, name), ...]; empty on non-listener lines.
    Owns every (name, pid) tuple, not just the first."""
    parts = line.split()
    if len(parts) < 5 or parts[0] != "LISTEN":
        return []
    port = parts[3].rsplit(":", 1)[-1]
    if not port.isdigit():
        return []
    proc = " ".join(parts[5:]) if len(parts) > 5 else ""
    m = re.search(r"users:\(\((.*)\)\)", proc)
    if not m:
        return []
    entries = []
    for pm in re.finditer(r'"([^"]*)"\s*,\s*pid=(\d+)', m.group(1)):
        entries.append((int(pm.group(2)), int(port), pm.group(1)))
    return entries


def scan_listeners():
    """Listeners via `ss -tlnp` -> [(pid, port, process_name)].

    Raises RuntimeError when `ss` is missing, times out, or fails: a failed
    discovery must not look like an empty scan (the caller keeps the last
    good snapshot instead)."""
    try:
        proc = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True,
                              timeout=10)
    except Exception as e:
        raise RuntimeError("ss failed: %s" % e)
    if proc.returncode != 0:
        raise RuntimeError("ss exited %d: %s" % (proc.returncode, (proc.stderr or "").strip()[:120]))
    entries = []
    for line in (proc.stdout or "").splitlines():
        entries.extend(parse_ss_line(line))
    # dedupe (pid, port)
    seen, uniq = set(), []
    for e in entries:
        key = (e[0], e[1])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(e)
    return uniq

def query_ps(pids):
    """One batched `ps` call for every pid -> {pid: {cpu, memKB, uptime, command}}."""
    if not pids:
        return {}
    try:
        out = subprocess.run(
            ["ps", "-p", ",".join(str(p) for p in pids),
             "-o", "pid=,pcpu=,rss=,etimes=,args="],
            capture_output=True, text=True, timeout=10).stdout or ""
    except Exception:
        return {}
    info = {}
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+([\d.]+)\s+(\d+)\s+(\d+)\s+(.*)$", line)
        if not m:
            continue
        info[int(m.group(1))] = {
            "cpu": float(m.group(2)),
            "memKB": int(m.group(3)),
            "uptime": int(m.group(4)),
            "command": m.group(5),
        }
    return info


def classify(cmd, name, port):
    """Classified label or None (server skipped). cmd/name already lowered."""
    for label, test in TIER1:
        if test(cmd, name):
            return label
    if port in DEV_PORTS:
        for label, test in TIER2:
            if test(cmd, name):
                return label
    return None


def likely_project(name):
    return bool(name) and name.lower() not in NOT_LIKELY_PROJECT


def infer_app_name(project_path, command, home):
    if project_path and project_path.rstrip("/") != home.rstrip("/"):
        cand = os.path.basename(project_path.rstrip("/")) or ""
        if likely_project(cand):
            return cand
    if command:
        for tok in command.split():
            tok = tok.strip("'\"")
            if tok.startswith("/"):
                cand = os.path.basename(os.path.dirname(tok)) or ""
                if likely_project(cand):
                    return cand
    return None


def read_argv(pid):
    """Exact argv for /proc/<pid>/cmdline (NUL-separated), or None."""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            parts = f.read().split(b"\0")
        return [p.decode("utf-8", "replace") for p in parts if p] or None
    except Exception:
        return None


def parse_pid(s):
    """Positive real PID (> 1); rejects 0/negative/group-kill forms."""
    try:
        pid = int(s)
    except (TypeError, ValueError):
        return None
    return pid if pid > 1 else None


def probe(port):
    """(health, https). https first: any connection -> green+https. Then http:
    connection with status < 500 -> green, >= 500 -> yellow; refusal/timeout -> unknown."""
    try:
        urllib.request.urlopen("https://localhost:%d" % port,
                               timeout=1.5, context=HTTPS_CTX)
        return ("green", True)
    except urllib.error.HTTPError:
        return ("green", True)
    except Exception:
        pass
    try:
        urllib.request.urlopen("http://localhost:%d" % port, timeout=1.5)
        return ("green", False)
    except urllib.error.HTTPError as e:
        return (("yellow" if e.code >= 500 else "green"), False)
    except Exception:
        return ("unknown", False)


def probe_ports(ports):
    """Parallel health probe per unique port, max 8 workers."""
    result = {}
    unique = sorted(set(ports))
    if not unique:
        return result
    with ThreadPoolExecutor(max_workers=8) as ex:
        for p, outcome in zip(unique, ex.map(probe, unique)):
            result[p] = outcome
    return result


def notify(title, body):
    # Prefer the omarchy-native sender (passes through to the shell's
    # notification daemon); fall back to plain notify-send.
    cmd = ["omarchy-notification-send"] if shutil.which("omarchy-notification-send") \
        else ["notify-send", "-a", "Lookout"]
    cmd += [title, body]
    try:
        subprocess.run(cmd, stdout=DEVNULL, stderr=DEVNULL, timeout=5)
    except Exception:
        pass


def notify_changes(last_ports, current_ports, labels, server_by_port, notify_on):
    """Diff the last snapshot against current ports and announce changes.

    last_ports None means no snapshot exists yet: this run only establishes
    the baseline, so servers already running are not announced as just
    started. The snapshot stays current even when notify_on is False, so
    re-enabling notifications does not flood stale changes. Returns the new
    snapshot to store in prefs.
    """
    if last_ports is None:
        return sorted(current_ports)
    last_set = set(int(x) for x in last_ports)
    if notify_on:
        for p in sorted(current_ports - last_set):
            label = labels.get("port_%d" % p)
            if not label and p in server_by_port:
                label = server_by_port[p]["label"]
            notify("Server Started", "%s started on :%d" % (label or "Server", p))
        for p in sorted(last_set - current_ports):
            notify("Server Stopped", "Server on :%d stopped" % p)
    return sorted(current_ports)


# ---------------------------------------------------------------- commands

def cmd_scan(notify_on):
    try:
        prefs = _load_prefs()
        labels = prefs.get("labels", {})
        paths = prefs.get("paths", {})
        listeners = scan_listeners()
        psdata = query_ps(sorted({p for p, _, _ in listeners}))

        # One entry per pid: its lowest listening port (drops HMR duplicates).
        by_pid = {}
        for pid, port, name in listeners:
            if pid not in by_pid or port < by_pid[pid][0]:
                by_pid[pid] = (port, name)

        home = os.path.expanduser("~")
        servers = []
        for pid in sorted(by_pid):
            port, name = by_pid[pid]
            pinfo = psdata.get(pid, {})
            raw_cmd = pinfo.get("command")
            cmd_l = (raw_cmd or "").lower()
            name_l = name.lower()
            label = classify(cmd_l, name_l, port)
            if not label:
                continue
            project_path = None
            try:
                project_path = os.readlink("/proc/%d/cwd" % pid)
            except Exception:
                project_path = None
            servers.append({
                "pid": pid,
                "port": port,
                "label": label,
                "appName": infer_app_name(project_path, raw_cmd or "", home),
                "projectPath": project_path,
                "argv": read_argv(pid),
                "command": raw_cmd if raw_cmd else None,
                "cpu": pinfo.get("cpu") if "cpu" in pinfo else None,
                "memMB": round(pinfo["memKB"] / 1024) if "memKB" in pinfo else None,
                "uptimeSec": pinfo.get("uptime") if "uptime" in pinfo else None,
                "health": "unknown",
                "https": False,
            })

        servers.sort(key=lambda s: (s["port"], s["pid"]))
        health = probe_ports([s["port"] for s in servers])
        with locked_prefs() as prefs:
            for s in servers:
                s["health"], s["https"] = health.get(s["port"], ("unknown", False))
                prefs.setdefault("lastHealth", {})[str(s["port"])] = s["https"]

        with locked_prefs() as prefs:
            current = {s["port"] for s in servers}
            prefs["lastPorts"] = notify_changes(
                prefs.get("lastPorts"), current, labels,
                {s["port"]: s for s in servers}, notify_on)

        print(json.dumps({"ok": True, "servers": servers, "labels": labels, "paths": paths}))
    except Exception:
        traceback.print_exc(file=sys.stderr)
        # Discovery failure must not look like an empty scan: the service
        # keeps its last good snapshot when ok is false.
        print(json.dumps({"ok": False, "servers": [], "labels": labels if 'labels' in locals() else {},
                          "paths": paths if 'paths' in locals() else {}}))


def cmd_open(port):
    prefs = _load_prefs()
    suffix = prefs.get("paths", {}).get("port_%d" % port, "")
    # scheme from last scan's health probe (instant); probe live as fallback.
    https = prefs.get("lastHealth", {}).get(str(port))
    if https is None:
        _, https = probe(port)
    scheme = "https" if https else "http"
    run_detached("exec xdg-open %s" % shq("%s://localhost:%d%s" % (scheme, port, suffix)))
    time.sleep(0.5)


def cmd_fm(path):
    run_detached("exec xdg-open %s" % shq(path))


def cmd_term(path):
    for binary, flags, kind in TERMINALS:
        if not shutil.which(binary):
            continue
        if kind == "joined":
            args = " ".join(f + shq(path) for f in flags)
        elif kind == "shell":
            inner = "cd %s && exec $SHELL" % shq(path)
            args = " ".join(flags) + " " + shq(inner)
        else:
            args = " ".join(flags) + " " + shq(path)
        run_detached("cd %s && exec %s %s" % (shq(path), binary, args))
        return


def cmd_edit(path):
    editor = None
    for var in ("VISUAL", "EDITOR"):
        val = (os.environ.get(var) or "").strip()
        if val and shutil.which(val.split()[0]):
            editor = "$" + var  # let the shell expand it (may contain args)
            break
    if not editor:
        for name in EDITOR_FALLBACKS:
            if shutil.which(name):
                editor = name
                break
    if not editor:
        return
    run_detached("exec %s %s" % (editor, shq(path)))


def cmd_kill(args):
    # SIGTERM only, and return immediately: an escalation thread would delay
    # the follow-up refresh by seconds and could SIGKILL a recycled PID.
    pid = parse_pid(args[0]) if args else None
    if not pid:
        return 2
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass  # already gone
    return 0


def cmd_kill_all(args):
    pids = []
    for a in args:
        if a == "--pids":
            continue
        pid = parse_pid(a)
        if not pid:
            return 2
        pids.append(pid)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    return 0


def cmd_restart(args):
    # <pid> <cwd> <argv-json> — argv is the exact NUL-read command vector,
    # relayed as JSON so argument boundaries survive without a shell.
    if len(args) < 3:
        return 2
    pid = parse_pid(args[0])
    if not pid:
        return 2
    cwd = args[1] or None
    try:
        argv = json.loads(args[2])
    except ValueError:
        return 2
    if (not isinstance(argv, list) or not argv
            or not all(isinstance(a, str) and a for a in argv)):
        return 2
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass

    def relaunch():
        time.sleep(0.8)  # let the old process release its port
        try:
            subprocess.Popen(argv, cwd=cwd, start_new_session=True,
                             stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL)
        except Exception:
            pass
    threading.Thread(target=relaunch, daemon=False).start()
    return 0


def edit_pref_value(kind, args):
    # label|path <port> <value> — empty value removes the key.
    if len(args) < 2:
        return 2
    port, value = args[0], args[1]
    key = "port_%s" % port
    with locked_prefs() as prefs:
        bucket = prefs.setdefault(kind, {})
        if value:
            bucket[key] = value
        else:
            bucket.pop(key, None)
    return 0


def _selftest():
    """assert-based check for notify_changes; run via `lookout.py selftest`."""
    global notify
    calls = []

    def fake_notify(title, body):
        calls.append((title, body))

    orig_notify = notify
    notify = fake_notify
    try:
        by_port = {3000: {"label": "Vite"}, 4000: {"label": "Node"}}
        lab = {"port_3000": "My Vite"}
        # Never-initialized snapshot: establish baseline, announce nothing.
        assert notify_changes(None, {3000, 4000}, lab, by_port, True) == [3000, 4000]
        assert calls == []
        # Steady state: nothing announced.
        assert notify_changes([3000, 4000], {3000, 4000}, lab, by_port, True) == [3000, 4000]
        assert calls == []
        # A start with a saved label: the custom label wins.
        assert notify_changes([4000], {3000, 4000}, lab, by_port, True) == [3000, 4000]
        assert calls == [("Server Started", "My Vite started on :3000")]
        calls.clear()
        # A stop.
        assert notify_changes([3000, 4000], {3000}, lab, by_port, True) == [3000]
        assert calls == [("Server Stopped", "Server on :4000 stopped")]
        calls.clear()
        # Notifications Off: snapshot still updated, nothing announced.
        assert notify_changes([3000], {3000, 4000}, lab, by_port, False) == [3000, 4000]
        assert calls == []
        # No saved label: fall back to the classified server label.
        assert notify_changes([4000], {3000, 4000}, {}, by_port, True) == [3000, 4000]
        assert calls == [("Server Started", "Vite started on :3000")]
        print("lookout selftest: ok")
    finally:
        notify = orig_notify


def _selftest2():
    """assert-based checks for the ss parser, PID validation, and restart argv."""
    # Single owner.
    assert parse_ss_line(
        "LISTEN 0      4096      127.0.0.1:3000       0.0.0.0:*    users:((\"next-server\",pid=123,fd=36))"
    ) == [(123, 3000, "next-server")]
    # Multiple owners on one socket: every tuple is owned, then deduped.
    two = parse_ss_line(
        "LISTEN 0      4096      127.0.0.1:5173       0.0.0.0:*    users:((\"node\",pid=111,fd=1),(\"node\",pid=222,fd=2))"
    )
    assert two == [(111, 5173, "node"), (222, 5173, "node")]
    # Non-listener lines and unparseable rows yield nothing.
    assert parse_ss_line("ESTAB 0 0 1.2.3.4:5 6.7.8.9:10") == []
    assert parse_ss_line("  garbage") == []
    # PID validation: positive real PIDs only.
    assert parse_pid("0") is None and parse_pid("-1") is None and parse_pid("abc") is None
    assert parse_pid("123") == 123
    # Restart argv round-trips through JSON as a list of strings.
    argv = ["node", "server.js", "--port", "3000"]
    assert json.loads(json.dumps(argv)) == argv
    print("lookout selftest2: ok")


def main(argv):
    if not argv:
        print("usage: lookout.py <scan|open|fm|term|edit|kill|kill-all|restart|label|path> ...",
              file=sys.stderr)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "scan":
        notify_on = "--notify" in args and "on" in args
        cmd_scan(notify_on)
        return 0
    if cmd == "selftest":
        _selftest()
        _selftest2()
        return 0
    if cmd == "open":
        if not args:
            return 2
        try:
            cmd_open(int(args[0]))
        except ValueError:
            return 2
        return 0
    if cmd == "fm":
        if not args:
            return 2
        cmd_fm(args[0])
        return 0
    if cmd == "term":
        if not args:
            return 2
        cmd_term(args[0])
        return 0
    if cmd == "edit":
        if not args:
            return 2
        cmd_edit(args[0])
        return 0
    if cmd == "kill":
        return cmd_kill(args)
    if cmd == "kill-all":
        return cmd_kill_all(args)
    if cmd == "restart":
        return cmd_restart(args)
    if cmd == "label":
        return edit_pref_value("labels", args)
    if cmd == "path":
        return edit_pref_value("paths", args)
    print("lookout.py: unknown command: %s" % cmd, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))