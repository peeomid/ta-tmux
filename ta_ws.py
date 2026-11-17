#!/usr/bin/env python3
"""ta ws - herdr-like workspaces on plain tmux.

Workspace = 1 tmux session. Tab = 1 tmux window. Pane = 1 tmux pane.

Commands (called through `ta ws ...`):
  ls                          List workspaces: tabs, panes, agent states.
  new <name> [dir]            Make a workspace (and switch to it inside tmux).
  save [--every SECS] [-q]    Save all layouts + agent session ids.
  snapshots                   List the saved snapshots.
  restore [--start] [--dry-run] [--from FILE]
                              Build the workspaces again from the last
                              snapshot of an EARLIER tmux server.
  import-herdr [FILE] [--start] [--dry-run] [--only LABEL]
                              Build the herdr workspaces in tmux.
  bar [CURRENT_SESSION]       Workspace bar text (used by tmux.conf).

Agents in restored panes: by default the resume command is TYPED but not
run (press Enter in the pane). --start also presses Enter.
A Claude session that is running now somewhere else is never resumed twice.

Only tmux and ps are called. No network. Python 3 standard library only.
"""
import fcntl
import json
import os
import re
import socket
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

SOCKET = os.environ.get("TA_SOCKET", "")
STATE = Path(os.environ.get("TA_STATE", Path.home() / ".local/state/ta"))
SNAP_DIR = STATE / "ws"
REGISTRY = STATE / f"agents{'-' + SOCKET if SOCKET else ''}.tsv"
CLAUDE_SESSIONS = Path.home() / ".claude/sessions"
KEEP_SNAPSHOTS = 20
HARNESSES = ("claude", "codex", "agy")
pending = []   # (pane id, command, press Enter?) typed after the shells start


def ere_to_python(pattern):
    """The status patterns come from `ta` (POSIX ERE). Python has no [[:alpha:]]."""
    return pattern.replace("[[:alpha:]]", r"[^\W\d_]")


WORKING_RE = re.compile(ere_to_python(os.environ.get("TA_WORKING_RE", "esc to interrupt")), re.M)
BLOCKED_RE = re.compile(ere_to_python(os.environ.get("TA_BLOCKED_RE", "Do you want to ")), re.M)


# --- tmux --------------------------------------------------------------------

def tmux(*args, check=True):
    cmd = ["tmux"] + (["-L", SOCKET] if SOCKET else []) + list(args)
    for attempt in range(4):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            break
        except OSError:
            # "Resource temporarily unavailable": the machine is too busy to start
            # a process. Wait a little and try again. Do not crash the sidebar.
            if attempt == 3:
                if check:
                    sys.exit("ta ws: tmux: cannot start a process (machine overloaded)")
                return ""
            time.sleep(0.2 * (attempt + 1))
    if check and result.returncode != 0:
        sys.exit(f"ta ws: tmux {' '.join(args[:2])}: {result.stderr.strip()}")
    return result.stdout


def tmux_rows(command, fields):
    """Run a tmux list command. Return 1 dict per line, keyed by field name."""
    fmt = "\t".join("#{%s}" % f for f in fields)
    # -a (all sessions) exists only for list-panes / list-windows.
    scope = ["-a"] if command in ("list-panes", "list-windows") else []
    out = tmux(command, *scope, "-F", fmt, check=False)
    return [dict(zip(fields, line.split("\t"))) for line in out.splitlines() if line]


def sessions_in_order(fields):
    """Sessions in the order they were made (tmux sorts them by name)."""
    rows = tmux_rows("list-sessions", ["session_id"] + fields)
    return sorted(rows, key=lambda r: int(r["session_id"].lstrip("$")))


def server_running():
    return subprocess.run(["tmux"] + (["-L", SOCKET] if SOCKET else []) + ["ls"],
                          capture_output=True).returncode == 0


def server_start():
    return tmux("display", "-p", "#{start_time}", check=False).strip() or "none"


def inside_tmux():
    return bool(os.environ.get("TMUX"))


# --- which agent runs in a pane ---------------------------------------------

def process_table():
    """pid -> (parent pid, argv[0] name, full args)."""
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,args="],
                         capture_output=True, text=True).stdout
    table = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, ppid, args = int(parts[0]), int(parts[1]), parts[2]
        table[pid] = (ppid, os.path.basename(args.split()[0]), args)
    return table


def children_of(table):
    kids = {}
    for pid, (ppid, _, _) in table.items():
        kids.setdefault(ppid, []).append(pid)
    return kids


def find_agent(pane_pid, table, kids):
    """Walk the processes under the pane. Return (harness, pid, args) or None."""
    todo = [pane_pid]
    while todo:
        pid = todo.pop(0)
        if pid in table:
            _, name, args = table[pid]
            words = [os.path.basename(w) for w in args.split()[:2]]
            for harness in HARNESSES:
                if harness in words:
                    return harness, pid, args
        todo.extend(kids.get(pid, []))
    return None


def live_claude_sessions():
    """Claude Code writes ~/.claude/sessions/<pid>.json for each running app."""
    live = {}
    for f in CLAUDE_SESSIONS.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            os.kill(int(data["pid"]), 0)          # error if the process is gone
            live[int(data["pid"])] = data.get("sessionId", "")
        except (OSError, ValueError, KeyError):
            continue
    return live


def registry_ids():
    """(ta name, session) -> session id, from `ta run` (codex / agy ids)."""
    ids = {}
    if REGISTRY.exists():
        for line in REGISTRY.read_text().splitlines():
            cols = line.split("\t")
            if len(cols) >= 5 and cols[4]:
                ids[(cols[0], cols[1])] = cols[4]
    return ids


def session_id_for(harness, pid, args, claude_live, reg, ta_name, session):
    if harness == "claude" and pid in claude_live:
        return claude_live[pid]
    if (ta_name, session) in reg:
        return reg[(ta_name, session)]
    for flag in ("--resume", "resume", "--conversation", "--session-id"):
        m = re.search(r"(?:^|\s)%s\s+(\S+)" % re.escape(flag), args)
        if m:
            return m.group(1)
    return ""


def resume_command(harness, sid, ta_name=""):
    if harness == "claude":
        return f"claude --resume {sid}" + (f" -n {ta_name}" if ta_name else "") if sid else "claude"
    if harness == "codex":
        return f"codex resume {sid}" if sid else "codex"
    if harness == "agy":
        return f"agy --conversation {sid}" if sid else "agy"
    return ""


def status_of(pane_id):
    # Drop the empty rows under the last text first: on a tall pane the
    # bottom 30 rows can all be empty, and the status would read "idle".
    rows = tmux("capture-pane", "-p", "-J", "-t", pane_id, check=False).rstrip().splitlines()
    screen = "\n".join(rows[-30:])
    if BLOCKED_RE.search(screen):
        return "blocked"
    if WORKING_RE.search(screen):
        return "working"
    return "idle"


# --- self-reported Claude status (no screen text, no hooks) -------------------
# `claude agents --json --all` lists every Claude session with its own state.
# One call answers for all panes. The result is cached for CACHE_SECONDS so that
# many sidebars / status bars share one call.

CACHE_SECONDS = 2.0
AGENTS_CACHE = STATE / "claude-agents.json"


def claude_state_to_status(item):
    """Map one `claude agents --json` entry to blocked | working | idle."""
    state, status = item.get("state"), item.get("status")
    if state == "blocked" or status == "waiting":
        return "blocked"
    if state == "working" or status == "busy":
        return "working"
    return "idle"


def claude_status_by_pid():
    """{pid: status} for running interactive Claude sessions. {} if unavailable."""
    try:
        if time.time() - AGENTS_CACHE.stat().st_mtime < CACHE_SECONDS:
            return {int(k): v for k, v in json.loads(AGENTS_CACHE.read_text()).items()}
    except (OSError, ValueError):
        pass
    try:
        out = subprocess.run(["claude", "agents", "--json", "--all"], capture_output=True,
                             text=True, timeout=4).stdout
        items = json.loads(out)
        result = {int(i["pid"]): claude_state_to_status(i) for i in items
                  if isinstance(i, dict) and isinstance(i.get("pid"), int)}
    except (OSError, ValueError, subprocess.SubprocessError, TypeError):
        return {}
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        tmp = AGENTS_CACHE.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(result))
        os.replace(tmp, AGENTS_CACHE)
    except OSError:
        pass
    return result


# "ready" = an agent finished or waits for you and you have not looked yet.
# Looking at its pane makes it "seen" and it is shown as idle again.
SEEN_FILE = STATE / "seen.json"


def update_seen(panes, active_pane):
    """Set p["shown"] on each agent pane: blocked | working | ready | idle."""
    try:
        state = json.loads(SEEN_FILE.read_text())
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    current = {}
    for p in panes:
        if not p["status"]:
            continue
        key = f"{p['harness']}:{p['pane_id']}"
        status = p["status"]
        if status == "idle" and (state.get(key) in (None, "seen") or p["pane_id"] == active_pane):
            status = "seen"
        current[key] = status
        p["shown"] = "ready" if status == "idle" else ("idle" if status == "seen" else status)
    if current != state:
        try:
            STATE.mkdir(parents=True, exist_ok=True)
            tmp = SEEN_FILE.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(current))
            os.replace(tmp, SEEN_FILE)
        except OSError:
            pass


# --- snapshot ----------------------------------------------------------------

PANE_FIELDS = ["session_name", "window_index", "pane_index", "pane_id", "pane_pid",
               "pane_current_path", "pane_active", "@ta_name", "@ta_harness", "@ta_pending", "@ta_side"]
WINDOW_FIELDS = ["session_name", "window_index", "window_name", "window_layout",
                 "window_active", "window_width", "window_height", "automatic-rename"]


def take_snapshot():
    table = process_table()
    kids = children_of(table)
    claude_live = live_claude_sessions()
    reg = registry_ids()

    sessions = {}
    for s in sessions_in_order(["session_name", "session_path"]):
        sessions[s["session_name"]] = {"name": s["session_name"], "dir": s["session_path"],
                                       "tabs": {}}
    for w in tmux_rows("list-windows", WINDOW_FIELDS):
        sessions[w["session_name"]]["tabs"][w["window_index"]] = {
            "name": w["window_name"],
            "auto_name": w["automatic-rename"] == "1",
            "layout": w["window_layout"],
            "width": int(w["window_width"]), "height": int(w["window_height"]),
            "active": w["window_active"] == "1",
            "panes": [],
        }
    for p in tmux_rows("list-panes", PANE_FIELDS):
        if p["@ta_side"] == "1":
            # The sidebar is not saved as a pane. Its place in the layout
            # string is kept, so restore can rebuild the same splits.
            tab = sessions[p["session_name"]]["tabs"][p["window_index"]]
            tab["side_pane_index"] = int(p["pane_index"])
            continue
        pane = {"dir": p["pane_current_path"], "active": p["pane_active"] == "1",
                "harness": "", "session_id": "", "ta_name": p["@ta_name"]}
        agent = find_agent(int(p["pane_pid"]), table, kids)
        if agent:
            harness, pid, args = agent
            pane["harness"] = harness
            pane["session_id"] = session_id_for(harness, pid, args, claude_live, reg,
                                                p["@ta_name"], p["session_name"])
        elif p["@ta_pending"]:
            # Restored pane where the resume command is typed but not run yet.
            harness, _, sid = p["@ta_pending"].partition(" ")
            pane["harness"], pane["session_id"] = harness, sid
        tab = sessions[p["session_name"]]["tabs"][p["window_index"]]
        tab["panes"].append((int(p["pane_index"]), pane))

    workspaces = []
    for s in sessions.values():
        tabs = []
        for index in sorted(s["tabs"], key=int):
            tab = s["tabs"][index]
            if "side_pane_index" in tab:
                # Position from 0 (pane numbers can start at 1: pane-base-index).
                side = tab.pop("side_pane_index")
                tab["side_index"] = sum(1 for i, _ in tab["panes"] if i < side)
            tab["panes"] = [pane for _, pane in sorted(tab["panes"], key=lambda x: x[0])]
            tabs.append(tab)
        workspaces.append({"name": s["name"], "dir": s["dir"], "tabs": tabs})
    return {"saved_at": time.strftime("%Y-%m-%d %H:%M:%S"), "socket": SOCKET or "default",
            "server_start": server_start(), "workspaces": workspaces}


def snapshot_files():
    prefix = f"{SOCKET or 'default'}-"
    return sorted(SNAP_DIR.glob(prefix + "*.json"), key=lambda f: f.stat().st_mtime,
                  reverse=True)


def cmd_save(args):
    every = int(args[args.index("--every") + 1]) if "--every" in args else 0
    quiet = "-q" in args or every > 0
    if not server_running():
        return
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    # 1 file per tmux server life. A new server (after a crash) writes a new
    # file, so the last snapshot of the old server stays for `restore`.
    target = SNAP_DIR / f"{SOCKET or 'default'}-{server_start()}.json"
    if every and target.exists() and time.time() - target.stat().st_mtime < every:
        return
    snap = take_snapshot()
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, indent=1))
    tmp.replace(target)
    for old in snapshot_files()[KEEP_SNAPSHOTS:]:
        old.unlink()
    if not quiet:
        panes = sum(len(t["panes"]) for w in snap["workspaces"] for t in w["tabs"])
        print(f"saved {len(snap['workspaces'])} workspaces, {panes} panes -> {target}")


def cmd_snapshots(_args):
    current = server_start() if server_running() else ""
    for f in snapshot_files():
        snap = json.loads(f.read_text())
        names = ", ".join(w["name"] for w in snap["workspaces"])
        mark = " (this server)" if snap["server_start"] == current else ""
        print(f"{snap['saved_at']}{mark}  {f.name}\n    {names}")


# --- build workspaces --------------------------------------------------------

def checksum(layout):
    """tmux layout checksum (layout.c, layout_checksum)."""
    csum = 0
    for ch in layout:
        csum = (csum >> 1) + ((csum & 1) << 15)
        csum = (csum + ord(ch)) & 0xFFFF
    return f"{csum:04x}"


def build_window(session, first, tab, start, dry_run, live_ids, notes):
    """Make 1 tab: create its panes in order, apply the layout, type the commands."""
    panes = tab["panes"]
    if dry_run:
        print(f"  tab '{tab['name']}': {len(panes)} panes")
    else:
        # If the window had a sidebar, its layout string has 1 more pane.
        # Make a stand-in pane at that place, apply the layout, then close it.
        dirs = [pane["dir"] for pane in panes]
        side_index = tab.get("side_index")
        if side_index is not None:
            dirs.insert(side_index, str(Path.home()))
        if first:
            tmux("new-session", "-d", "-s", session, "-c", dirs[0],
                 "-x", str(tab["width"]), "-y", str(tab["height"]))
            window = tmux("display", "-p", "-t", f"={session}:", "#{window_id}").strip()
        else:
            window = tmux("new-window", "-d", "-P", "-F", "#{window_id}", "-t", f"={session}:",
                          "-c", dirs[0]).strip()
        last = tmux("display", "-p", "-t", window, "#{pane_id}").strip()
        made = [last]
        for folder in dirs[1:]:
            # Split the newest pane each time: tmux then keeps the panes in
            # creation order, which is the order the layout string uses.
            last = tmux("split-window", "-d", "-P", "-F", "#{pane_id}", "-t", last,
                        "-c", folder).strip()
            made.append(last)
        tmux("resize-window", "-t", window, "-x", str(tab["width"]), "-y", str(tab["height"]))
        tmux("select-layout", "-t", window, tab["layout"], check=False)
        tmux("set-option", "-wu", "-t", window, "window-size", check=False)
        if side_index is not None:
            tmux("kill-pane", "-t", made.pop(side_index))
        pane_ids = made
        if not tab.get("auto_name", True):
            tmux("rename-window", "-t", window, tab["name"])
        for pane_id, pane in zip(pane_ids, panes):
            if pane["active"]:
                tmux("select-pane", "-t", pane_id)
            if pane.get("ta_name"):
                tmux("set-option", "-p", "-t", pane_id, "@ta_name", pane["ta_name"])
                tmux("set-option", "-p", "-t", pane_id, "@ta_harness", pane["harness"])
        if tab["active"]:
            tmux("select-window", "-t", window)

    for i, pane in enumerate(panes):
        if not pane["harness"]:
            if dry_run:
                print(f"    pane {i}: shell in {pane['dir']}")
            continue
        sid = pane["session_id"]
        if sid and sid in live_ids:
            notes.append(f"{session}: {pane['harness']} {sid} runs now in another terminal; not resumed")
            command = ""
        else:
            command = resume_command(pane["harness"], sid, pane.get("ta_name", ""))
        if dry_run:
            print(f"    pane {i}: {command or '(skip: running elsewhere)'}  in {pane['dir']}")
            continue
        # Remember the agent on the pane until it really starts, so the next
        # snapshot keeps it (also when it was skipped or not started yet).
        tmux("set-option", "-p", "-t", pane_ids[i], "@ta_pending",
             f"{pane['harness']} {sid}".strip())
        if command:
            pending.append((pane_ids[i], command, start))


def build_workspaces(workspaces, start, dry_run):
    existing = {s["session_name"] for s in tmux_rows("list-sessions", ["session_name"])} \
        if server_running() else set()
    live_ids = set(live_claude_sessions().values())
    notes = []
    made = []
    for ws in workspaces:
        name = ws["name"].replace(":", " ").replace(".", " ")
        if name in existing:
            notes.append(f"'{name}' exists already; skipped")
            continue
        if dry_run:
            print(f"workspace '{name}'")
        for n, tab in enumerate(ws["tabs"]):
            if tab["panes"]:
                build_window(name, n == 0, tab, start, dry_run, live_ids, notes)
        made.append(name)
    if pending:
        time.sleep(1.5)  # let each shell start before typing into it
        for pane_id, command, enter in pending:
            tmux("send-keys", "-t", pane_id, "-l", command)
            if enter:
                tmux("send-keys", "-t", pane_id, "Enter")
    for note in notes:
        print("note:", note)
    if made and not dry_run:
        word = "started" if start else "typed (press Enter in each pane to resume)"
        print(f"built {len(made)} workspaces: {', '.join(made)}. Agent commands {word}.")
    return made


def cmd_restore(args):
    start, dry_run = "--start" in args, "--dry-run" in args
    if "--from" in args:
        source = Path(args[args.index("--from") + 1])
    else:
        current = server_start() if server_running() else ""
        older = [f for f in snapshot_files()
                 if json.loads(f.read_text())["server_start"] != current]
        if not older:
            sys.exit("ta ws: no snapshot from an earlier tmux server (see `ta ws snapshots`)")
        source = older[0]
    snap = json.loads(source.read_text())
    print(f"from {source.name} (saved {snap['saved_at']})")
    build_workspaces(snap["workspaces"], start, dry_run)


# --- herdr import ------------------------------------------------------------

def guillotine(rects, x, y, w, h):
    """Turn herdr pane rectangles into a split tree.

    Return ("leaf", pane_id) or ("row" | "col", [(size share, subtree), ...]).
    "row" = side by side (tmux {}), "col" = top to bottom (tmux []).
    """
    if len(rects) == 1:
        return ("leaf", rects[0]["pane_id"])
    for axis, pos, size, start, length in (("row", "x", "width", x, w),
                                           ("col", "y", "height", y, h)):
        cuts = sorted({r["rect"][pos] for r in rects if r["rect"][pos] > start})
        cuts = [c for c in cuts
                if not any(r["rect"][pos] < c < r["rect"][pos] + r["rect"][size] for r in rects)]
        if not cuts:
            continue
        edges = [start] + cuts + [start + length]
        parts = []
        for a, b in zip(edges, edges[1:]):
            group = [r for r in rects if a <= r["rect"][pos] < b]
            sub = (a, y, b - a, h) if axis == "row" else (x, a, w, b - a)
            parts.append(((b - a) / length, guillotine(group, *sub)))
        return (axis, parts)
    raise ValueError("herdr layout is not made of straight splits")


def layout_string(tree, x, y, w, h, order):
    """tmux layout body for a tree in a w x h box. Borders take 1 cell."""
    if tree[0] == "leaf":
        order.append(tree[1])
        return f"{w}x{h},{x},{y},{len(order) - 1}"
    axis, parts = tree
    total = (w if axis == "row" else h) - (len(parts) - 1)
    sizes = [max(1, round(share * total)) for share, _ in parts]
    sizes[-1] = total - sum(sizes[:-1])
    pieces, pos = [], (x if axis == "row" else y)
    for size, (_, sub) in zip(sizes, parts):
        if axis == "row":
            pieces.append(layout_string(sub, pos, y, size, h, order))
        else:
            pieces.append(layout_string(sub, x, pos, w, size, order))
        pos += size + 1
    open_, close = ("{", "}") if axis == "row" else ("[", "]")
    return f"{w}x{h},{x},{y}{open_}{','.join(pieces)}{close}"


def herdr_workspaces(data, only):
    panes_by_id = {p["pane_id"]: p for p in data["panes"]}
    tabs_by_ws = {}
    for layout in data["layouts"]:
        tabs_by_ws.setdefault(layout["workspace_id"], []).append(layout)
    result = []
    for ws in sorted(data["workspaces"], key=lambda w: w["number"]):
        if only and ws["label"] != only:
            continue
        tabs = []
        for layout in tabs_by_ws.get(ws["workspace_id"], []):
            area = layout["area"]
            tree = guillotine(layout["panes"], area["x"], area["y"], area["width"], area["height"])
            order = []
            body = layout_string(tree, 0, 0, area["width"], area["height"], order)
            panes = []
            for pane_id in order:
                p = panes_by_id.get(pane_id, {})
                agent = p.get("agent") or ""
                panes.append({"dir": p.get("cwd") or str(Path.home()),
                              "harness": agent if agent in HARNESSES else "",
                              "session_id": p.get("session_id") or "", "ta_name": "",
                              "active": pane_id == layout.get("focused_pane_id")})
            label = (panes_by_id.get(order[0], {}).get("tab") or "")
            tabs.append({"name": label, "auto_name": not label or label.isdigit(),
                         "layout": f"{checksum(body)},{body}",
                         "width": area["width"], "height": area["height"],
                         "active": layout["tab_id"] == ws.get("active_tab_id"),
                         "panes": panes})
        result.append({"name": ws["label"], "dir": "", "tabs": tabs})
    return result


def cmd_import_herdr(args):
    files = [a for a in args if not a.startswith("--")
             and (args.index(a) == 0 or args[args.index(a) - 1] != "--only")]
    if not files:
        sys.exit("import-herdr: give the path of a herdr workspaces.json export")
    source = Path(files[0])
    only = args[args.index("--only") + 1] if "--only" in args else ""
    data = json.loads(source.read_text())
    build_workspaces(herdr_workspaces(data, only), "--start" in args, "--dry-run" in args)


# --- list, new, bar ----------------------------------------------------------

def pane_agents():
    """All panes (no sidebars), each with its agent and status ("" = no agent)."""
    table = process_table()
    kids = children_of(table)
    claude_status = claude_status_by_pid() if any(
        "claude" in args for _, _, args in table.values()) else {}
    rows = tmux_rows("list-panes", ["session_name", "window_id", "window_index", "window_name",
                                    "window_active", "pane_id", "pane_index", "pane_active",
                                    "pane_pid", "pane_dead", "automatic-rename",
                                    "@ta_side", "@ta_name", "pane_title"])
    result = []
    for p in rows:
        if p["@ta_side"] == "1":
            continue
        agent = None if p["pane_dead"] == "1" else find_agent(int(p["pane_pid"]), table, kids)
        p["harness"] = agent[0] if agent else ""
        if agent and agent[0] == "claude" and agent[1] in claude_status:
            p["status"] = claude_status[agent[1]]          # self-reported by Claude
        else:
            p["status"] = status_of(p["pane_id"]) if agent else ""   # screen text
        p["shown"] = p["status"]
        result.append(p)
    return result


def agent_states(panes=None):
    """session name -> {"working": n, "blocked": n, "idle": n} for agent panes."""
    states = {}
    for p in panes if panes is not None else pane_agents():
        counts = states.setdefault(p["session_name"],
                                   {"working": 0, "blocked": 0, "ready": 0, "idle": 0})
        if p["status"]:
            counts[p.get("shown", p["status"])] += 1
    return states


def cmd_ls(_args):
    if not server_running():
        sys.exit("ta ws: tmux is not running")
    panes = pane_agents()
    states = agent_states(panes)
    print(f"{'WORKSPACE':<28} {'TABS':>4} {'PANES':>5}  AGENTS")
    for s in sessions_in_order(["session_name", "session_windows", "session_attached"]):
        n_panes = sum(1 for p in panes if p["session_name"] == s["session_name"])
        c = states.get(s["session_name"], {})
        agents = f"working {c.get('working', 0)}, blocked {c.get('blocked', 0)}, idle {c.get('idle', 0)}"
        mark = "*" if s["session_attached"] != "0" else " "
        print(f"{mark}{s['session_name']:<27} {s['session_windows']:>4} {n_panes:>5}  {agents}")


def cmd_new(args):
    if not args:
        sys.exit("usage: ta ws new <name> [dir]")
    name = args[0].replace(":", " ").replace(".", " ")
    folder = os.path.abspath(os.path.expanduser(args[1])) if len(args) > 1 else os.getcwd()
    tmux("new-session", "-d", "-s", name, "-c", folder)
    if inside_tmux():
        tmux("switch-client", "-t", f"={name}")
    else:
        print(f"made '{name}'. Open it: tmux attach -t '{name}'")


def cmd_bar(args):
    """1 status line: each workspace with its agent counts. Current one is marked."""
    current = args[0] if args else ""
    states = agent_states()
    items = []
    for s in sessions_in_order(["session_name"]):
        name = s["session_name"]
        c = states.get(name, {})
        flags = ""
        if c.get("blocked"):
            flags += f" #[fg=black,bg=yellow] wait {c['blocked']} #[default]"
        if c.get("working"):
            flags += f" #[fg=green]● {c['working']}#[default]"
        if name == current:
            items.append(f"#[reverse,bold] {name} #[default]{flags}")
        else:
            items.append(f" {name} {flags}")
    print("  ".join(items))


# --- sidebar (herdr-like workspace list on the left) ------------------------
#
# tmux has no sidebar. We add a narrow pane on the left of the window you
# look at (marked @ta_side=1). It runs `ta ws side`: a live list you can
# click. Hooks in tmux.conf call `side-ensure` when you change window or
# workspace, so the sidebar follows you. `ta ws sidebar toggle` (prefix e).

SIDE_WIDTH = 30
MARK_CHAR = {"blocked": "!", "working": "●", "ready": "✓"}
STATUS_MARK = {"working": "\x1b[33m●\x1b[0m", "blocked": "\x1b[1;31m!\x1b[0m",
               "ready": "\x1b[36m✓\x1b[0m", "idle": "\x1b[2m○\x1b[0m"}
# 256-colour numbers. Override with tmux options, e.g. `set -g @ta_color_ready 6`
# (needs / working / ready / current). Idle has no colour: it is dimmed.
PALETTE = {"blocked": "1", "working": "3", "ready": "6", "current": "4"}
PALETTE_OPTIONS = {"blocked": "@ta_color_needs", "working": "@ta_color_working",
                   "ready": "@ta_color_ready", "current": "@ta_color_current"}


def set_palette(values):
    """values: option texts in PALETTE_OPTIONS order. Bad or empty values keep the default."""
    for key, value in zip(PALETTE_OPTIONS, values):
        if value.isdecimal() and int(value) < 256:
            PALETTE[key] = value
    for key in ("blocked", "working", "ready"):
        bold = "1;" if key == "blocked" else ""
        STATUS_MARK[key] = f"\x1b[{bold}38;5;{PALETTE[key]}m{MARK_CHAR[key]}\x1b[0m"


def name_style(status, current):
    """Colour code for the agent name: current = blue, needs/working/ready = their
    colour, idle = dim."""
    if current:
        return f"\x1b[1;38;5;{PALETTE['current']}m"
    if status in ("blocked", "working", "ready"):
        return f"\x1b[1;38;5;{PALETTE[status]}m"
    return "\x1b[2m"


STATUS_ORDER = {"blocked": 0, "working": 1, "ready": 2, "idle": 3}   # most urgent first


def sidebar_on():
    return tmux("show-options", "-gqv", "@ta_sidebar", check=False).strip() == "on"


def side_panes():
    rows = tmux_rows("list-panes", ["pane_id", "window_id", "@ta_side"])
    return [r for r in rows if r["@ta_side"] == "1"]


def ensure_side(window):
    """Add a sidebar to this window if the sidebar is on and it has none."""
    if not window or not sidebar_on():
        return
    # Several hooks fire at once (attach = client-attached + session-changed).
    # A lock lets only 1 of them check and add, so a tab never gets 2 sidebars.
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / f"side-{SOCKET or 'default'}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        mine = [p["pane_id"] for p in side_panes() if p["window_id"] == window]
        for extra in mine[1:]:                      # repair an old double sidebar
            tmux("kill-pane", "-t", extra, check=False)
        if mine:
            return
        pane =tmux("split-window", "-hbf", "-d", "-l", str(SIDE_WIDTH), "-t", window,
                    "-P", "-F", "#{pane_id}", "-c", str(Path.home()), "ta ws side",
                    check=False).strip()
        if pane:
            tmux("set-option", "-p", "-t", pane, "@ta_side", "1")
            tmux("set-option", "-p", "-t", pane, "remain-on-exit", "off")


def clean_side():
    """Close windows that only have a sidebar left (the work pane was closed)."""
    rows = tmux_rows("list-panes", ["window_id", "@ta_side"])
    work = {r["window_id"] for r in rows if r["@ta_side"] != "1"}
    for window in {r["window_id"] for r in rows} - work:
        tmux("kill-window", "-t", window, check=False)


def cmd_side_ensure(args):
    clean_side()
    ensure_side(args[0] if args else "")


def cmd_turn(args):
    """Put this pane and the next pane side by side (h) or on top of each
    other (v). The sidebar does not move. Use: ta ws turn h|v [pane id]."""
    if not args or args[0] not in ("h", "v"):
        sys.exit("use: ta ws turn h|v [pane id]")
    where = ["-t", args[1]] if len(args) > 1 else []
    out = tmux("list-panes", *where, "-F", "#{pane_id}\t#{@ta_side}\t#{pane_active}", check=False)
    rows = [line.split("\t") for line in out.splitlines() if line]
    work = [r for r in rows if r[1] != "1"]
    if len(work) < 2:
        tmux("display-message", "Only 1 pane here: nothing to turn", check=False)
        return
    ids = [r[0] for r in work]
    here = args[1] if len(args) > 1 and args[1] in ids else next((r[0] for r in work if r[2] == "1"), ids[0])
    other = ids[(ids.index(here) + 1) % len(ids)]
    tmux("move-pane", "-" + args[0], "-s", other, "-t", here, check=False)
    tmux("select-pane", "-t", here, check=False)


def cmd_fix(_args):
    """Repair the layout in all tabs: 1 sidebar per tab (or none when the
    sidebar is off), sidebar 30 columns wide, tabs fit the terminal again."""
    clean_side()
    fixed = []
    by_window = {}
    for p in side_panes():
        by_window.setdefault(p["window_id"], []).append(p["pane_id"])
    for window, panes in by_window.items():
        keep = panes[:1] if sidebar_on() else []
        for extra in panes[len(keep):]:
            tmux("kill-pane", "-t", extra, check=False)
            fixed.append(f"{window}: closed an extra sidebar")
        for pane in keep:
            tmux("resize-pane", "-t", pane, "-x", str(SIDE_WIDTH), check=False)
    for w in tmux_rows("list-windows", ["window_id"]):
        # A fixed size (left by a restore or resize-window) stops a tab from
        # following the terminal size. Unset it, so the tab fits again.
        tmux("set-option", "-wu", "-t", w["window_id"], "window-size", check=False)
    for w in tmux_rows("list-windows", ["window_id"]):   # every tab, not only the shown ones:
        ensure_side(w["window_id"])                      # a tab without one shifts when opened
    for line in fixed:
        print(line)
    print(f"layout fixed ({len(fixed)} extra sidebars closed). "
          "Ctrl-b Space cycles the split if a tab still looks wrong.")


def cmd_sidebar(args):
    mode = args[0] if args else "toggle"
    if mode == "toggle":
        mode = "off" if sidebar_on() else "on"
    tmux("set-option", "-g", "@ta_sidebar", mode)
    if mode == "on":
        tmux("set-option", "-g", "status", "on")       # the sidebar replaces line 2
        for c in tmux_rows("list-clients", ["window_id"]):
            ensure_side(c["window_id"])
    else:
        tmux("set-option", "-g", "status", "2")
        for p in side_panes():
            tmux("kill-pane", "-t", p["pane_id"], check=False)


def char_cols(ch):
    """Terminal columns of 1 character: 2 for wide (CJK, emoji), 0 for combining marks, else 1."""
    if unicodedata.category(ch) in ("Mn", "Me", "Cf"):   # combining mark, zero-width joiner ...
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def cut_pad(text, cols):
    """Cut text to at most `cols` terminal columns, then pad with spaces to exactly `cols`.
    Python's len() counts characters, but a CJK or emoji character fills 2 columns.
    With len() such a name is too wide, wraps, and moves every line below it."""
    used, out = 0, []
    for ch in text:
        w = char_cols(ch)
        if used + w > cols:
            break
        out.append(ch)
        used += w
    return "".join(out) + " " * (cols - used)


def dim(text, width):
    """A dimmed sidebar line, cut to the pane width."""
    return f"\x1b[2m{cut_pad(text, width).rstrip()}\x1b[0m"


# Leading characters an agent puts in its pane title: spinner / status marks and spaces.
TITLE_NOISE = re.compile("^[\\s\u2722\u2733\u2736\u273b\u273d\u00b7*\u2800-\u28ff]+")


def agent_label(pane):
    """Name of an agent for the sidebar and `ta top`.
    Order: @ta_name, then the pane title (agents set it), then the window name.
    The window name is last: it can be a version like "2.1.318"."""
    if pane.get("@ta_name"):
        return pane["@ta_name"]
    title = TITLE_NOISE.sub("", pane.get("pane_title", "")).strip()
    host = socket.gethostname().split(".")[0].lower()
    # tmux sets the title to the host name when the program sets none.
    if title and title.split(".")[0].lower() != host:
        return title
    return pane["window_name"]


def agents_by_workspace(panes, order):
    """[(number, workspace name, [agent panes by tab index])], in workspace order.
    Only workspaces that have agents."""
    groups = []
    for n, s in enumerate(order, start=1):
        agents = [p for p in panes if p["session_name"] == s["session_name"] and p["status"]]
        if agents:
            agents.sort(key=lambda p: int(p["window_index"]))
            groups.append((n, s["session_name"], agents))
    return groups


HIGHLIGHT_BG = "\x1b[48;5;238m"


ANSI_CODE = re.compile(r"\x1b\[[0-9;]*m")


def highlight(text, width):
    """Give a sidebar line a grey background over the full width, like herdr.
    Marks end with a reset code, so the background is set again after each one."""
    padded = text + " " * max(width - visible_cols(text), 0)
    return HIGHLIGHT_BG + padded.replace("\x1b[0m", "\x1b[0m" + HIGHLIGHT_BG) + "\x1b[0m"


def visible_cols(text):
    """Terminal columns of a text with color codes."""
    return sum(char_cols(ch) for ch in ANSI_CODE.sub("", text))


def agent_short_name(pane):
    """Line 2 of an agent: @ta_name if set, else the harness (claude / codex / agy)."""
    return pane.get("@ta_name") or pane["harness"]


def agent_row(pane, tab_count, width, current=False):
    """One line per agent: "<›><mark> <workspace>[ · <tab>] <name>", whole row coloured
    by status (current row blue, idle dim), cut to the width. The tab part shows only
    when the workspace has more than 1 tab. A tab name set by hand wins; an automatic
    name says nothing, so use the index."""
    title = pane["session_name"]
    if tab_count > 1:
        by_hand = pane.get("automatic-rename") == "0"
        title += f" \u00b7 {pane['window_name'] if by_hand else pane['window_index']}"
    short = agent_short_name(pane)
    if short == pane["harness"]:
        title += f" \u00b7 {short}"             # default name: workspace first
    elif short and short != title:
        title = f"{short} \u00b7 {title}"       # a name you chose: it goes first
    text_w = width - 4                           # marker + mark + 2 spaces
    if visible_cols(title) > text_w:             # cut 1 column short to fit the ellipsis
        title = cut_pad(title, text_w - 1).rstrip() + "\u2026"
    marker = f"\x1b[1;38;5;{PALETTE['current']}m\u203a\x1b[0m" if current else " "
    style = name_style(pane["shown"], current)
    return f"{marker}{STATUS_MARK[pane['shown']]} {style}{cut_pad(title, text_w).rstrip()}\x1b[0m"


def agent_block(panes, order, width, room, current=None):
    """AGENTS block lines [(text, action)]; action = pane id. One line per agent.
    Order: the pane you are on first, then needs you, working, ready, idle; tab
    order inside a group. room = max lines. The caller must give room >= 2."""
    lines = [("\x1b[1m AGENTS\x1b[0m", None)]
    agents = [p for _, _, group in agents_by_workspace(panes, order) for p in
              sorted(group, key=lambda p: (int(p["window_index"]), int(p["pane_index"])))]
    if not agents:
        return lines + [(dim(" (no agents)", width), None)]
    agents.sort(key=lambda p: (p["pane_id"] != current, STATUS_ORDER[p["shown"]]))
    tab_counts = {}                              # workspace -> number of tabs
    for name, window in {(p["session_name"], p["window_id"]) for p in panes}:
        tab_counts[name] = tab_counts.get(name, 0) + 1
    room = max(room - 1, 0)                      # the AGENTS title line
    fit = room
    if len(agents) > fit:                        # keep 1 line for "+N more"
        fit = max(room - 1, 0)
    for p in agents[:fit]:
        lines.append((agent_row(p, tab_counts[p["session_name"]], width, p["pane_id"] == current),
                      p["pane_id"]))
    if len(agents) > fit:
        lines.append((dim(f" +{len(agents) - fit} more", width), None))
    return lines


def cmd_agents_tree(_args):
    """Text for `ta top`: workspaces, each with its agents indented.
    Columns: pane id, tab (index:name), harness, status, name."""
    panes = pane_agents()
    for n, name, agents in agents_by_workspace(panes, sessions_in_order(["session_name"])):
        print(f"{n} {name}")
        for p in agents:
            tab = cut_pad(f"{p['window_index']}:{p['window_name']}", 14)
            print(f"  {p['pane_id']:<5} {tab} {p['harness']:<7} {p['status']:<8} {agent_label(p)}")


def fit_height(top, bottom, height, width):
    """Return top + bottom as at most `height` lines. If too tall: cut the end of
    `top` and add a dimmed "+N more" (N = hidden lines). If even that is too tall,
    drop `bottom` (the footer)."""
    if len(top) + len(bottom) <= height:
        return top + bottom
    if height - len(bottom) < 3:                 # no room for the header, 1 entry and "+N more"
        bottom = []
    if len(top) + len(bottom) <= height:
        return top + bottom
    keep = max(height - len(bottom) - 1, 0)
    return top[:keep] + [(dim(f" +{len(top) - keep} more", width), None)] + bottom


def current_work_pane(window, panes):
    """Pane id of the work pane you are on: the active pane of this sidebar's window.
    When the sidebar itself has focus, no work pane is active; use the first one."""
    here = [p for p in panes if p["window_id"] == window]
    active = [p for p in here if p["pane_active"] == "1"] or here[:1]
    return active[0]["pane_id"] if active else None


SIDE_CURRENT = {}        # set by side_lines: the current workspace / agent actions


def side_lines(me, width, height=None, focused=False):
    """Build the sidebar text. Return [(text, action)]; action = tmux target or None.
    Actions: "=name:" workspace, "@id" window, "%id" agent pane.
    Layout like tmux-agent-status-sidebar: WORKSPACES on top, a divider at about
    half height, AGENTS below, one line each.
    Never more than `height` lines: a longer text scrolls the pane, and then
    a click lands on the wrong line."""
    fmt = "\t".join(["#{session_name}", "#{window_id}"] +
                    ["#{%s}" % o for o in PALETTE_OPTIONS.values()])
    my_session, my_window, *colors = tmux("display", "-p", "-t", me, fmt).rstrip("\n").split("\t")
    set_palette(colors)
    panes = pane_agents()
    order = sessions_in_order(["session_name"])
    current = current_work_pane(my_window, panes)
    update_seen(panes, current)                  # sets p["shown"] (ready / idle)
    states = agent_states(panes)
    ws_lines = [("\x1b[1m WORKSPACES\x1b[0m", None)]
    SIDE_CURRENT.clear()
    SIDE_CURRENT["agent"] = current
    for n, s in enumerate(order, start=1):
        name = s["session_name"]
        counts = states.get(name, {})
        worst = next((k for k in ("blocked", "working", "ready") if counts.get(k)), None)
        is_me = name == my_session
        mark = STATUS_MARK[worst] if worst else " "
        marker = f"\x1b[1;38;5;{PALETTE['current']}m\u203a\x1b[0m" if is_me else " "
        style = name_style(worst or "idle", is_me)
        label = cut_pad(f"{n} {name}", width - 4).rstrip()
        ws_lines.append((f"{marker}{mark} {style}{label}\x1b[0m", f"={name}:"))
        if is_me:
            SIDE_CURRENT["ws"] = f"={name}:"
            tabs = {}
            for p in panes:
                if p["session_name"] == name:
                    tabs.setdefault((int(p["window_index"]), p["window_id"], p["window_name"],
                                     p["window_active"]), []).append(p)
            for (index, window_id, window_name, active), tab_panes in sorted(tabs.items()):
                # At most 5 marks, so a tab with many agents does not wrap.
                shown = [STATUS_MARK[p["shown"]] for p in sorted(
                    tab_panes, key=lambda p: STATUS_ORDER.get(p["shown"], 9)) if p["status"]][:5]
                arrow = "\u25b8" if active == "1" else " "
                text = cut_pad(f"   {arrow}{index} {window_name}", width - 6)
                ws_lines.append((f"\x1b[2m{text}\x1b[0m {''.join(shown)}".rstrip(), window_id))
    if focused:
        footer = [(dim(" j/k move  Tab section", width), None),
                  (dim(" Enter open  Esc back", width), None)]
    else:
        footer = [(dim(" click or Ctrl-b Tab", width), None),
                  (dim(" ? keys  1-9 workspace", width), None)]
    height = height if height else 10**6
    body = max(height - len(footer), 4)
    # The divider sits at half height. Workspaces may push it down, but AGENTS
    # keeps at least 4 lines (title + 2 agents + "+N more").
    middle = min(max(len(ws_lines) + 1, body // 2), max(body - 4, 1))
    top = fit_height(ws_lines, [], middle - 1, width)
    top += [("", None)] * (middle - 1 - len(top))
    lines = top + [(dim("\u2500" * width, width), None)]
    room = body - len(lines)
    if room >= 2:
        lines += agent_block(panes, order, width, room, current)
    lines += [("", None)] * max(body - len(lines), 0) + footer
    return lines[:height]                        # last guard: never taller than the pane


def reverse_row(text, width):
    """Selected row: reverse video over the full width. Colour codes end with a
    reset, so reverse is set again after each one."""
    padded = text + " " * max(width - visible_cols(text), 0)
    return "\x1b[7m" + padded.replace("\x1b[0m", "\x1b[0m\x1b[7m") + "\x1b[0m"


KEY_TOKEN = re.compile(r"\x1b\[[AB]|\x1bO[AB]|\x1b|.", re.S)


def open_target(target, me):
    """Jump to a sidebar action: "%pane", "=session:" or "@window"."""
    if target.startswith("%"):
        where = tmux("display", "-p", "-t", target,
                     "#{session_name}\t#{window_id}", check=False).strip()
        session, _, window = where.partition("\t")
        if window:
            # One tmux process for all 3 steps (";" chains commands).
            tmux("switch-client", "-t", f"={session}:", ";",
                 "select-window", "-t", window, ";",
                 "select-pane", "-t", target, check=False)
        return                                    # focus is on the agent pane now
    # Switch, then give the keyboard back to the work pane: 1 process.
    verb = "switch-client" if target.startswith("=") else "select-window"
    tmux(verb, "-t", target, ";", "last-pane", "-t", me, check=False)


def cmd_side(_args):
    """The sidebar program. Redraws every 2 s while you can see it.
    Mouse: click a row. Keyboard (when the sidebar has focus, Ctrl-b Tab):
    j/k or arrows move, Tab switches WORKSPACES / AGENTS, Enter opens, Esc gives
    the keyboard back to the work pane."""
    import select
    import termios
    import tty
    me = os.environ.get("TMUX_PANE", "")
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    # Hide the cursor; ask the terminal (tmux) to send mouse clicks (SGR mode).
    sys.stdout.write("\x1b[?25l\x1b[?1000h\x1b[?1006h")
    click = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
    lines, actions = [], []
    focused, visible, section = False, False, 0
    selected = {0: None, 1: None}                # section -> selected action
    last_check, force = 0.0, True

    def section_actions(n):
        seen, out = set(), []
        for a in actions:
            if a and (a.startswith("%") == bool(n)) and a not in seen:
                seen.add(a)
                out.append(a)
        return out

    def fix_selection():
        for n in (0, 1):
            items = section_actions(n)
            if selected[n] not in items:
                default = SIDE_CURRENT.get("agent" if n else "ws")
                selected[n] = default if default in items else (items[0] if items else None)

    def paint():
        try:
            width = os.get_terminal_size().columns
            out = []
            for text, action in lines:
                if focused and action and action == selected[1 if action.startswith("%") else 0] \
                        and action == selected[section]:
                    text = reverse_row(text, width)
                out.append(text + "\x1b[K")
            sys.stdout.write("\x1b[H" + "\n".join(out) + "\x1b[J")
            sys.stdout.flush()
        except OSError:
            pass

    try:
        while True:
            now = time.time()
            if force or now - last_check >= 2:
                force, last_check = False, now
                state = tmux("display", "-p", "-t", me,
                             "#{&&:#{window_active},#{session_attached}}\t#{pane_active}",
                             check=False).strip().split("\t")
                visible = state[0] == "1"
                focus_now = visible and state[-1] == "1"
                if visible or not lines:
                    try:
                        size = os.get_terminal_size()
                        lines = side_lines(me, size.columns, size.lines, focus_now)
                        actions = [a for _, a in lines]
                        focused = focus_now
                        fix_selection()
                        paint()
                    except (OSError, ValueError):
                        pass                # busy machine: skip this redraw, try in 2 s
            ready, _, _ = select.select([fd], [], [], max(0.05, 2 - (time.time() - last_check)))
            if not ready:
                continue
            data = os.read(fd, 256).decode(errors="ignore")
            for button, _x, row, kind in click.findall(data):
                line = int(row) - 1
                if kind == "M" and button == "0" and 0 <= line < len(actions) and actions[line]:
                    open_target(actions[line], me)
                    force = True
            keys = KEY_TOKEN.findall(click.sub("", data))
            moved = False
            for key in keys:
                items = section_actions(section)
                if key in ("j", "\x1b[B", "\x1bOB") and items:
                    at = items.index(selected[section]) if selected[section] in items else -1
                    selected[section] = items[min(at + 1, len(items) - 1)]
                    moved = True
                elif key in ("k", "\x1b[A", "\x1bOA") and items:
                    at = items.index(selected[section]) if selected[section] in items else 0
                    selected[section] = items[max(at - 1, 0)]
                    moved = True
                elif key == "\t":
                    section = 1 - section
                    moved = True
                elif key in ("\r", "\n") and selected[section]:
                    open_target(selected[section], me)
                    force = True
                elif key == "\x1b":
                    tmux("last-pane", "-t", me, check=False)    # give the keyboard back
                    force = True
                elif key == "?":
                    show_help_popup(me)
                elif key.isdigit() and key != "0":
                    order = sessions_in_order(["session_name"])
                    if int(key) <= len(order):
                        tmux("switch-client", "-t", f"={order[int(key) - 1]['session_name']}:",
                             check=False)
                        force = True
            if moved:
                paint()
    except BaseException:  # also SystemExit (tmux error) and Ctrl-C: log why it stopped
        import traceback
        STATE.mkdir(parents=True, exist_ok=True)
        with open(STATE / "sidebar-error.log", "a") as log:
            log.write(time.strftime("%Y-%m-%d %H:%M:%S ") + traceback.format_exc())
        raise
    finally:
        sys.stdout.write("\x1b[?1000l\x1b[?1006l\x1b[?25h")
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def cmd_side_focus(args):
    """Give the keyboard to the sidebar of a window (prefix Tab). Shows it if hidden."""
    window = args[0] if args else tmux("display", "-p", "#{window_id}").strip()
    mine = [p for p in side_panes() if p["window_id"] == window]
    if not mine:
        ensure_side(window)
        mine = [p for p in side_panes() if p["window_id"] == window]
    if mine:
        tmux("select-pane", "-t", mine[0]["pane_id"], check=False)


# --- key help (prefix ?) -------------------------------------------------------

HELP = [
    ("Get in / get out of tmux", [
        ("Ctrl-b d (or D)", "Get out (detach). All agents keep running"),
        ("ta", "Get back in (type it in any terminal)"),
        ("exit", "Close this pane only (Ctrl-b & = close the tab)"),
        ("Ctrl-b F", "Layout looks broken: fix it (= `ta fix`; agents not touched)"),
        ("tmux kill-server", "Stop ALL of tmux and its agents. Last resort"),
    ]),
    ("Workspace  (tmux name: session)", [
        ("Ctrl-b w", "Pick a workspace / tab / pane (arrows, Enter; → opens a workspace)"),
        ("Ctrl-b ( / )", "Previous / next workspace"),
        ("Ctrl-b L", "Last workspace you used"),
        ("Ctrl-b W", "New workspace (asks name + folder)"),
        ("Ctrl-b $", "Rename this workspace"),
        ("Ctrl-b d", "Detach: close the view, everything keeps running"),
        ("ta ws ls", "List workspaces + agent states (in a shell)"),
    ]),
    ("Tab  (tmux name: window)", [
        ("click a tab", "The tab bar is on top (like herdr): click a tab to open it"),
        ("Ctrl-b c", "New tab"),
        ("Ctrl-b n / p", "Next / previous tab"),
        ("Ctrl-b 1 … 9", "Go to tab N"),
        ("Ctrl-b l", "Last tab you used"),
        ("Ctrl-b ,", "Rename this tab"),
        ("Ctrl-b &", "Close this tab (asks first)"),
        ("Ctrl-b B", "Next tab that rang the bell (agent needs you)"),
    ]),
    ("Pane", [
        ("Ctrl-b %", "Split: new pane on the right"),
        ('Ctrl-b "', "Split: new pane below"),
        ("Ctrl-b |", "This pane + next pane: side by side (sidebar stays)"),
        ("Ctrl-b _", "This pane + next pane: on top of each other (sidebar stays)"),
        ("Ctrl-b ← ↑ → ↓", "Go to the pane in that direction (or click it)"),
        ("Ctrl-b o / ;", "Next pane / last pane"),
        ("Ctrl-b z", "Zoom: this pane full size (again = back)"),
        ("Ctrl-b q", "Show pane numbers (press a number to go there)"),
        ("Ctrl-b x", "Close this pane (asks first)"),
        ("Ctrl-b { / }", "Swap this pane with the previous / next one"),
        ("Ctrl-b !", "Move this pane to its own new tab"),
        ("Ctrl-b Space", "Next layout (cycle the split arrangement)"),
        ("Ctrl-b Alt-arrow", "Resize by 5 (or drag the border with the mouse)"),
    ]),
    ("Sidebar", [
        ("Ctrl-b e", "Show / hide the sidebar"),
        ("click", "Open the workspace or tab on that line"),
        ("1-9  (in it)", "Open workspace 1-9"),
        ("click an agent", "Jump to that agent's pane"),
        ("Ctrl-b Tab", "Keyboard in the sidebar: j/k or arrows move, Tab = WORKSPACES / AGENTS, Enter opens, Esc back"),
        ("! ● ✓ ○", "Needs you (red) / working (yellow) / ready, not seen yet (cyan) / idle;  › = you are here"),
    ]),
    ("Agents  (ta)", [
        ("Ctrl-b a", "Live list of agents by workspace (popup; q or Esc closes)"),
        ("Ctrl-b N", "New agent here (asks harness + task)"),
        ("ta run / read / send / wait", "Start, read, message, wait (in a shell; `ta help`)"),
    ]),
    ("Save and restore", [
        ("Ctrl-b S", "Save all workspaces now (autosave every 30 s)"),
        ("ta ws restore", "After a crash: build all workspaces again"),
        ("ta restore", "After a crash: resume agents started with `ta run`"),
    ]),
    ("Scroll and copy", [
        ("mouse wheel", "Scroll back (q or Esc to leave)"),
        ("Ctrl-b [", "Scroll mode with keys: arrows, PgUp/PgDn, / to search, q leaves"),
        ("mouse drag", "Select text = copy (hold Option for the iTerm2 selection)"),
    ]),
]


HELP_POPUP = "ta ws help | less -R -~ -P' q = close '"


def show_help_popup(pane):
    """Open the help on the client that shows this pane (a popup needs a client)."""
    session = tmux("display", "-p", "-t", pane, "#{session_name}", check=False).strip()
    clients = tmux("list-clients", "-t", f"={session}", "-F", "#{client_name}",
                   check=False).split()
    if clients:
        tmux("display-popup", "-c", clients[0], "-w", "96", "-h", "90%", "-E", HELP_POPUP,
             check=False)


def cmd_help(_args):
    """Key help grouped like herdr. Shown in a popup by `prefix ?`."""
    out = ["\x1b[1m tmux keys \x1b[0m\x1b[2m  Ctrl-b = press Ctrl+b, let go, then the key."
           "  q closes this.\x1b[0m", ""]
    for title, rows in HELP:
        out.append(f"\x1b[1;36m {title}\x1b[0m")
        for key, what in rows:
            out.append(f"   \x1b[1m{key:<26}\x1b[0m {what}")
        out.append("")
    print("\n".join(out))


COMMANDS = {"ls": cmd_ls, "help": cmd_help, "new": cmd_new, "save": cmd_save, "snapshots": cmd_snapshots,
            "restore": cmd_restore, "import-herdr": cmd_import_herdr, "bar": cmd_bar,
            "sidebar": cmd_sidebar, "side": cmd_side, "side-focus": cmd_side_focus, "side-ensure": cmd_side_ensure, "fix": cmd_fix, "turn": cmd_turn,
            "agents-tree": cmd_agents_tree}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(0 if len(sys.argv) < 2 else 1)
    COMMANDS[sys.argv[1]](sys.argv[2:])
