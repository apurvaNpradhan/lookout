#!/usr/bin/env python3
"""Lookout — dev-server watcher for the Omarchy shell. Detection and actions
only; the UI lives in Service.qml / Panel.qml. Python 3 stdlib only.

CLI: python3 <plugindir>/lookout.py <command> [args...]

Commands: scan, open, fm, term, edit, kill, kill-all, restart, label, path
`scan` is the only stdout writer; everything else is silent.
"""
import errno
import fcntl
import json
import os
import re
import select
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
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
MAX_PREFS_BYTES = 256 * 1024
MAX_SS_OUTPUT_BYTES = 512 * 1024
MAX_PS_OUTPUT_BYTES = 512 * 1024
MAX_SCAN_OUTPUT_BYTES = 512 * 1024
MAX_LISTENERS = 512
MAX_OWNERS_PER_LINE = 32
MAX_ARGV_BYTES = 64 * 1024
MAX_ARGC = 128
MAX_TEXT_BYTES = 16 * 1024


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


HTTP_OPENER = urllib.request.build_opener(
    NoRedirectHandler(), urllib.request.HTTPSHandler(context=HTTPS_CTX))

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
        if os.path.getsize(PREFS_PATH) > MAX_PREFS_BYTES:
            return default
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
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(STATE_DIR, 0o700)
    except OSError:
        pass
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(LOCK_PATH, flags, 0o600)
    os.fchmod(fd, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        prefs = _load_prefs()
        before = json.dumps(prefs, sort_keys=True)
        yield prefs
        if json.dumps(prefs, sort_keys=True) != before:
            tmp = None
            tmp_fd = None
            try:
                tmp_fd, tmp = tempfile.mkstemp(prefix=".prefs.", suffix=".tmp",
                                               dir=STATE_DIR)
                os.fchmod(tmp_fd, 0o600)
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    tmp_fd = None
                    json.dump(prefs, f, indent=2)
                os.replace(tmp, PREFS_PATH)
                tmp = None
            finally:
                if tmp_fd is not None:
                    os.close(tmp_fd)
                if tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


# ------------------------------------------------------------- detection

def run_bounded(command, max_bytes, timeout):
    """Run a command while bounding stdout and wall-clock time."""
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=DEVNULL)
    start = time.monotonic()
    chunks = []
    total = 0
    try:
        while True:
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                raise TimeoutError("command timed out")
            ready, _, _ = select.select([proc.stdout], [], [], remaining)
            if not ready:
                raise TimeoutError("command timed out")
            chunk = os.read(proc.stdout.fileno(), min(65536, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("command output exceeded limit")
            chunks.append(chunk)
        remaining = max(0, timeout - (time.monotonic() - start))
        returncode = proc.wait(timeout=remaining)
        if returncode != 0:
            raise RuntimeError("command exited %d" % returncode)
        return b"".join(chunks).decode("utf-8", "replace")
    except Exception:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=1)
        except Exception:
            pass
        raise


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
    for index, pm in enumerate(re.finditer(r'"([^"]*)"\s*,\s*pid=(\d+)', m.group(1))):
        if index >= MAX_OWNERS_PER_LINE:
            raise RuntimeError("ss owner limit exceeded")
        entries.append((int(pm.group(2)), int(port), pm.group(1)[:MAX_TEXT_BYTES]))
    return entries


def scan_listeners():
    """Listeners via `ss -tlnp` -> [(pid, port, process_name)].

    Raises RuntimeError when `ss` is missing, times out, or fails: a failed
    discovery must not look like an empty scan (the caller keeps the last
    good snapshot instead)."""
    try:
        out = run_bounded(["ss", "-tlnp"], MAX_SS_OUTPUT_BYTES, 10)
    except Exception as e:
        raise RuntimeError("ss failed: %s" % e)
    entries = []
    for line in out.splitlines():
        entries.extend(parse_ss_line(line))
        if len(entries) > MAX_LISTENERS:
            raise RuntimeError("listener limit exceeded")
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
        out = run_bounded(
            ["ps", "-p", ",".join(str(p) for p in pids[:MAX_LISTENERS]),
             "-o", "pid=,pcpu=,rss=,etimes=,args="],
            MAX_PS_OUTPUT_BYTES, 10)
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
            "command": m.group(5)[:MAX_TEXT_BYTES],
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
    """Exact, bounded argv for /proc/<pid>/cmdline, or None."""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            raw = f.read(MAX_ARGV_BYTES + 1)
        if len(raw) > MAX_ARGV_BYTES:
            return None
        parts = [p for p in raw.split(b"\0") if p]
        if not parts or len(parts) > MAX_ARGC:
            return None
        argv = [p.decode("utf-8", "replace") for p in parts]
        if any(len(p.encode("utf-8")) > MAX_TEXT_BYTES for p in argv):
            return None
        return argv
    except Exception:
        return None


def process_identity(pid):
    """PID identity: process start time plus the executable's inode.

    The start time changes on PID reuse; the exe inode changes when the
    process execs a different binary (start time alone survives exec, so a
    stored target could become another executable without invalidating it).
    """
    try:
        with open("/proc/%d/stat" % pid, "r", encoding="utf-8") as f:
            stat = f.read(4096)
        end_comm = stat.rfind(")")
        if end_comm < 0:
            return None
        fields = stat[end_comm + 2:].split()
        start = fields[19] if len(fields) > 19 else None
        if start is None:
            return None
        return "%s:%s" % (start, os.stat("/proc/%d/exe" % pid).st_ino)
    except Exception:
        return None


def target_matches(pid, identity):
    return bool(identity) and process_identity(pid) == str(identity)


def signal_if_matches(pid, identity):
    """Send SIGTERM iff the identity still matches, with no PID-reuse window.

    pidfd_open pins the task, so its numeric PID cannot be recycled between
    the identity check and os.kill (os.pidfd_send_signal was removed from
    Python's os module in 3.14; the pin makes plain kill safe). Returns True
    when a signal was delivered.
    """
    try:
        pidfd = os.pidfd_open(pid)
    except OSError as e:
        if e.errno == errno.EINVAL:
            # ponytail: kernels <5.3 lack pidfd_open; the check+kill race remains there
            if not target_matches(pid, identity):
                return False
            try:
                os.kill(pid, signal.SIGTERM)
                return True
            except OSError:
                return False
        return False  # ESRCH: already gone
    try:
        if not target_matches(pid, identity):
            return False
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False  # signalled a zombie or lost the race to natural exit
    finally:
        os.close(pidfd)


def parse_pid(s):
    """Positive real PID (> 1); rejects 0/negative/group-kill forms."""
    try:
        pid = int(s)
    except (TypeError, ValueError):
        return None
    return pid if pid > 1 else None


def probe(port):
    """Probe localhost without following redirects to another host."""
    try:
        HTTP_OPENER.open("https://localhost:%d" % port, timeout=1.5)
        return ("green", True)
    except urllib.error.HTTPError:
        return ("green", True)
    except Exception:
        pass
    try:
        HTTP_OPENER.open("http://localhost:%d" % port, timeout=1.5)
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
                project_path = os.readlink("/proc/%d/cwd" % pid)[:MAX_TEXT_BYTES]
            except Exception:
                project_path = None
            servers.append({
                "pid": pid,
                "identity": process_identity(pid),
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

        payload = json.dumps({"ok": True, "servers": servers, "labels": labels, "paths": paths})
        if len(payload.encode("utf-8")) > MAX_SCAN_OUTPUT_BYTES:
            raise RuntimeError("scan payload exceeded limit")
        print(payload)
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
    # <pid> <identity>; refuse a recycled PID instead of signalling it.
    if len(args) < 2 or len(args[1]) > 64:
        return 2
    pid = parse_pid(args[0])
    if not pid or not signal_if_matches(pid, args[1]):
        return 2
    return 0


def cmd_kill_all(args):
    # --targets <json>, where each item is {pid, identity} from one scan.
    if (len(args) != 2 or args[0] != "--targets"
            or len(args[1].encode("utf-8")) > MAX_SCAN_OUTPUT_BYTES):
        return 2
    try:
        targets = json.loads(args[1])
    except ValueError:
        return 2
    if not isinstance(targets, list) or len(targets) > MAX_LISTENERS:
        return 2
    for target in targets:
        if not isinstance(target, dict):
            return 2
        pid = parse_pid(target.get("pid"))
        if pid:
            signal_if_matches(pid, target.get("identity"))
    return 0


def cmd_restart(args):
    # <pid> <identity> <cwd> <argv-json>; no shell is involved.
    if len(args) < 4:
        return 2
    pid = parse_pid(args[0])
    if (not pid or len(args[1]) > 64
            or len(args[2].encode("utf-8")) > MAX_TEXT_BYTES
            or len(args[3].encode("utf-8")) > MAX_ARGV_BYTES):
        return 2
    cwd = args[2] or None
    try:
        argv = json.loads(args[3])
    except ValueError:
        return 2
    if (not isinstance(argv, list) or not argv
            or not all(isinstance(a, str) and a for a in argv)):
        return 2
    if not signal_if_matches(pid, args[1]):
        return 2

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
    if len(value.encode("utf-8")) > MAX_TEXT_BYTES:
        return 2
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
    # A live process matches its own identity; a stale one does not.
    identity = process_identity(os.getpid())
    assert identity and target_matches(os.getpid(), identity)
    assert not target_matches(os.getpid(), "stale")
    # Identity is exec-sensitive: a pid that execs a different binary no
    # longer matches the identity recorded for the previous executable.
    child = subprocess.Popen(
        [sys.executable, "-c", "import os,time; time.sleep(1); os.execv('/usr/bin/sleep', ['sleep', '30'])"])
    try:
        pre = process_identity(child.pid)
        assert pre and pre.split(":", 1)[1] == str(os.stat(sys.executable).st_ino)
        sleep_ino = str(os.stat("/usr/bin/sleep").st_ino)
        deadline = time.time() + 5
        while time.time() < deadline:
            pid_ident = process_identity(child.pid)
            if pid_ident and pid_ident.split(":", 1)[1] == sleep_ino:
                break
            time.sleep(0.05)
        assert pid_ident and pid_ident.split(":", 1)[1] == sleep_ino, \
            "exec must swap the exe inode inside the identity"
        assert not target_matches(child.pid, pre)  # pre-exec identity is stale now
    finally:
        try:
            os.kill(child.pid, signal.SIGKILL)
        except OSError:
            pass
        child.wait()
    # signal_if_matches matches+signals a live child, refuses a stale
    # identity without signalling, and never signals an already-gone pid.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        child_ident = process_identity(child.pid)
        assert child_ident and signal_if_matches(child.pid, child_ident) is True
        assert child.wait(timeout=5) == -signal.SIGTERM
        assert signal_if_matches(child.pid, child_ident) is False
        assert not signal_if_matches(os.getpid(), "1:2")
    finally:
        if child.poll() is None:
            child.kill()
    # The old, identity-less kill-all protocol is rejected.
    assert cmd_kill_all(["--pids", str(os.getpid())]) == 2
    assert run_bounded([sys.executable, "-c", "print('ok')"], 1024, 2).strip() == "ok"
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