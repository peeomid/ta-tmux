#!/usr/bin/env python3
"""ta agent / workspace / tab / pane - herdr-style commands on plain tmux.

Called by `ta` (see the dispatch there). Mapping:
  workspace = tmux session, tab = tmux window, pane = tmux pane.

Text output by default (aligned columns). --json on every command.
Errors: 1 line on stderr, exit 1. Usage errors: exit 2 (argparse).
Only tmux and ps are called. Python 3 standard library only.
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ta_ws  # noqa: E402
from ta_ws import (HARNESSES, REGISTRY, STATE, children_of, find_agent,  # noqa: E402
                   pane_agents, process_table, sessions_in_order, status_of,
                   tmux, tmux_rows)

SOCKET = ta_ws.SOCKET
SHELLS = ("zsh", "bash", "sh", "fish")
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
TA_BIN = os.environ.get("TA_BIN") or str(Path(__file__).resolve().parent / "ta")

PANE_FIELDS = ["session_id", "session_name", "window_id", "window_index", "window_name",
               "window_active", "window_zoomed_flag", "pane_id", "pane_index", "pane_pid",
               "pane_dead", "pane_active", "pane_current_command", "pane_current_path",
               "pane_title", "@ta_name", "@ta_side", "@ta_harness"]


class Fail(Exception):
    pass


def fail(msg):
    raise Fail(msg)


# --- tmux helpers ----------------------------------------------------------------

def run(*args):
    """Run a tmux command. Return stdout. A tmux error becomes a clear 1-line error."""
    cmd = ["tmux"] + (["-L", SOCKET] if SOCKET else []) + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        fail(f"tmux {args[0]}: {r.stderr.strip() or 'failed'}")
    return r.stdout


def need_server():
    if not ta_ws.server_running():
        fail("tmux is not running")


def current_pane():
    """$TMUX_PANE, but only when we really run inside the tmux server we talk to."""
    pane, env = os.environ.get("TMUX_PANE", ""), os.environ.get("TMUX", "")
    if not pane or not env:
        return ""
    sock = os.path.basename(env.split(",")[0])
    return pane if sock == (SOCKET or "default") else ""


def inside_tmux():
    return bool(current_pane())


def all_panes():
    """Every pane except the sidebar panes, as dicts. Agent fields are filled in."""
    need_server()
    table = process_table()
    kids = children_of(table)
    claude_live = ta_ws.live_claude_sessions()
    reg = ta_ws.registry_ids()
    panes = []
    for p in tmux_rows("list-panes", PANE_FIELDS):
        if p["@ta_side"] == "1":
            continue
        p["name"] = p["@ta_name"]
        p["agent"] = None
        found = None if p["pane_dead"] == "1" else find_agent(int(p["pane_pid"]), table, kids)
        if found:
            kind, pid, args = found
            p["agent"] = {"kind": kind, "pid": pid, "args": args,
                          "session_id": ta_ws.session_id_for(kind, pid, args, claude_live, reg,
                                                             p["name"], p["session_name"])}
        panes.append(p)
    return panes


def agent_status(p):
    """working | blocked | idle | exited (exited = no agent process in the pane)."""
    if p["pane_dead"] == "1":
        return "exited"
    return status_of(p["pane_id"]) if p["agent"] else "exited"


def refresh(pane_id):
    for p in all_panes():
        if p["pane_id"] == pane_id:
            return p
    fail(f"pane {pane_id} is gone")


# --- registry (same format as `ta` reg_add / reg_del) ---------------------------

def reg_add(name, session, folder, harness, sid):
    reg_del(name, session)
    with open(REGISTRY, "a") as f:
        f.write("\t".join([name, session, folder, harness, sid,
                           datetime.now().strftime("%Y-%m-%d %H:%M")]) + "\n")


def reg_del(name, session):
    if not name or not REGISTRY.exists():
        return
    keep = [ln for ln in REGISTRY.read_text().splitlines()
            if ln.split("\t")[:2] != [name, session]]
    REGISTRY.write_text("".join(ln + "\n" for ln in keep))


def forget_panes(panes):
    for p in panes:
        if p["name"]:
            reg_del(p["name"], p["session_name"])


# --- references ------------------------------------------------------------------

def resolve_pane(target, panes=None):
    """A pane id (%N) or a unique @ta_name."""
    panes = all_panes() if panes is None else panes
    if target.startswith("%"):
        hits = [p for p in panes if p["pane_id"] == target]
        if not hits:
            fail(f"no pane {target}")
        return hits[0]
    hits = [p for p in panes if p["name"] == target]
    if not hits:
        fail(f"no agent or pane named '{target}'")
    if len(hits) > 1:
        # An old name can stay on a pane whose agent has exited. Only panes
        # where an agent runs count. Still 0 or more than 1: the user decides.
        running = [p for p in hits if p["agent"]]
        if len(running) == 1:
            return running[0]
        ids = ", ".join(p["pane_id"] for p in hits)
        fail(f"'{target}' matches {len(hits)} panes ({ids}), {len(running)} with a running agent; "
             "use the pane id")
    return hits[0]


def resolve_agent(target, panes=None):
    p = resolve_pane(target, panes)
    if not p["agent"]:
        # A bare shell (the agent has exited): anything typed there would RUN as a command.
        fail(f"no agent runs in pane {p['pane_id']}")
    p["status"] = agent_status(p)
    return p


def workspaces():
    """Sessions in sidebar order, with order number and counts."""
    need_server()
    rows = sessions_in_order(["session_name", "session_attached", "session_windows", "session_path"])
    for i, s in enumerate(rows, 1):
        s["num"] = i
    return rows


def resolve_ws(ref):
    rows = workspaces()
    for s in rows:
        if s["session_id"] == ref:
            return s
    # A pure number is the order number first (as in the sidebar). If there is
    # no such number, it can still be a label like "2026".
    if ref.isdigit() and 1 <= int(ref) <= len(rows):
        return rows[int(ref) - 1]
    for s in rows:
        if s["session_name"] == ref:
            return s
    fail(f"no workspace '{ref}' (use $id, label or number; see `ta workspace list`)")


def current_ws(ws_ref):
    """--workspace value, else the workspace of $TMUX_PANE."""
    if ws_ref:
        return resolve_ws(ws_ref)
    cp = current_pane()
    if not cp:
        fail("no --workspace given and not inside tmux")
    sid = run("display", "-p", "-t", cp, "#{session_id}").strip()
    return resolve_ws(sid)


WINDOW_FIELDS = ["session_id", "session_name", "window_id", "window_index", "window_name",
                 "window_active", "window_panes"]


def windows():
    need_server()
    return tmux_rows("list-windows", WINDOW_FIELDS)


def resolve_tab(ref):
    wins = windows()
    if ref.startswith("@"):
        for w in wins:
            if w["window_id"] == ref:
                return w
        fail(f"no tab {ref}")
    if ":" in ref:
        ws_ref, idx = ref.split(":", 1)
        s = resolve_ws(ws_ref)
        for w in wins:
            if w["session_id"] == s["session_id"] and w["window_index"] == idx:
                return w
        fail(f"no tab {ref}")
    fail(f"bad tab '{ref}': use @id or workspace:index")


def window_panes(window_id, panes=None):
    return [p for p in (all_panes() if panes is None else panes) if p["window_id"] == window_id]


# --- output ----------------------------------------------------------------------

def emit(args, data, text_fn):
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        text_fn(data)


def table(headers, rows):
    rows = [[str(c) for c in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]
    for r in [headers] + rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())


def kv(pairs):
    width = max(len(k) for k, _ in pairs)
    for k, v in pairs:
        print(f"{k.ljust(width)}  {v}")


def pane_obj(p):
    ag = p["agent"]
    return {"pane_id": p["pane_id"], "pane_index": int(p["pane_index"]),
            "workspace": p["session_name"], "workspace_id": p["session_id"],
            "tab_id": p["window_id"], "tab_index": int(p["window_index"]),
            "tab": p["window_name"], "cwd": p["pane_current_path"],
            "command": p["pane_current_command"], "title": p["pane_title"],
            "active": p["pane_active"] == "1" and p["window_active"] == "1",
            "name": p["name"] or None,
            "agent": {"kind": ag["kind"], "pid": ag["pid"]} if ag else None}


def agent_obj(p, detail=False):
    ag = p["agent"]
    o = {"name": p["name"] or None, "kind": ag["kind"] if ag else (p["@ta_harness"] or None),
         "status": p.get("status") or agent_status(p), "workspace": p["session_name"],
         "workspace_id": p["session_id"], "tab_id": p["window_id"],
         "tab_index": int(p["window_index"]), "tab": p["window_name"], "pane_id": p["pane_id"]}
    if detail:
        o["cwd"] = p["pane_current_path"]
        o["pid"] = ag["pid"] if ag else None
        o["command"] = ag["args"] if ag else None
        o["session_id"] = (ag["session_id"] if ag else "") or None
        o["started"] = None
        if REGISTRY.exists():
            for ln in REGISTRY.read_text().splitlines():
                c = ln.split("\t")
                if len(c) >= 6 and c[:2] == [p["name"], p["session_name"]]:
                    o["started"] = c[5]
    return o


def shorten(path):
    home = str(Path.home())
    return "~" + path[len(home):] if path.startswith(home) else path


# --- keys and text sending --------------------------------------------------------

KEYS = {"enter": "Enter", "esc": "Escape", "tab": "Tab", "space": "Space",
        "backspace": "BSpace", "up": "Up", "down": "Down", "left": "Left", "right": "Right",
        "pageup": "PageUp", "pagedown": "PageDown", "home": "Home", "end": "End",
        "delete": "DC"}


def tmux_keys(keys):
    """Check ALL keys first. Return the tmux key names."""
    out, bad = [], []
    for k in keys:
        low = k.lower()
        m = re.fullmatch(r"ctrl\+([a-z])", low)
        if low in KEYS:
            out.append(KEYS[low])
        elif m:
            out.append("C-" + m.group(1))
        else:
            bad.append(k)
    if bad:
        fail(f"unknown key(s): {', '.join(bad)}. Allowed: {', '.join(KEYS)}, ctrl+<letter>")
    return out


def guard_blocked(p, what):
    """SAFETY: never type into a permission / question prompt. A person decides there.
    Opt out (you accept the risk): TA_ALLOW_BLOCKED=1."""
    if os.environ.get("TA_ALLOW_BLOCKED") == "1":
        return
    if p["agent"] and agent_status(p) == "blocked":
        fail(f"{p.get('name') or p['pane_id']} is blocked (permission or question). "
             f"Read it with 'ta agent read {p['pane_id']}' and ask the user. ({what} refused; opt out: TA_ALLOW_BLOCKED=1)")


def lines_of(text):
    lines = text.rstrip("\n").split("\n") if text else []
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def read_pane(pane_id, source, n):
    if source == "visible":
        out = run("capture-pane", "-p", "-J", "-t", pane_id)
    else:
        out = run("capture-pane", "-p", "-J", "-t", pane_id, "-S", f"-{n}")
        if source == "recent":
            return "\n".join(lines_of(out)[-n:])
    return "\n".join(lines_of(out))


# --- wait ------------------------------------------------------------------------

STATUSES = ("working", "idle", "done", "blocked", "exited")


def wait_for(pane_id, until, timeout_ms):
    """Same rule as `ta wait` (herdr wait). Returns the status that ended the wait."""
    seen, t0 = False, time.monotonic()
    while True:
        p = refresh(pane_id)
        st = agent_status(p)
        now = {st}
        if st == "working":
            seen = True
        if st == "idle" and seen:
            now.add("done")
        elapsed = (time.monotonic() - t0) * 1000
        if until:
            hit = [u for u in until if u in now]
            if hit:
                return hit[0]
            if st == "exited":
                fail(f"agent exited before reaching: {', '.join(until)}")
        else:
            if st in ("blocked", "exited"):
                return st
            if st == "idle":
                if seen:
                    return "done"
                if elapsed >= 10000:
                    return "idle"
        if timeout_ms is not None and elapsed >= timeout_ms:
            fail(f"timeout after {timeout_ms} ms (status: {st})")
        time.sleep(0.5)


# --- agent commands ---------------------------------------------------------------

def a_list(args):
    rows = []
    for p in all_panes():
        if p["agent"]:
            p["status"] = agent_status(p)
            rows.append(agent_obj(p))

    def text(rows):
        table(["NAME", "KIND", "STATUS", "WORKSPACE", "TAB", "PANE"],
              [[r["name"] or "-", r["kind"], r["status"], r["workspace"],
                f"{r['tab_index']}:{r['tab']}", r["pane_id"]] for r in rows])
    emit(args, rows, text)


def a_get(args):
    p = resolve_agent(args.target)
    o = agent_obj(p, detail=True)

    def text(o):
        kv([("name", o["name"] or "-"), ("kind", o["kind"] or "-"), ("status", o["status"]),
            ("workspace", f"{o['workspace']} ({o['workspace_id']})"),
            ("tab", f"{o['tab_index']}:{o['tab']} ({o['tab_id']})"), ("pane", o["pane_id"]),
            ("cwd", shorten(o["cwd"])), ("pid", o["pid"] or "-"),
            ("session id", o["session_id"] or "-"), ("started", o["started"] or "-")])
    emit(args, o, text)


def a_read(args):
    p = resolve_pane(args.target)
    out = read_pane(p["pane_id"], args.source, args.lines)
    emit(args, {"target": args.target, "pane_id": p["pane_id"], "source": args.source, "text": out},
         lambda d: print(d["text"]))


def a_send_keys(args):
    p = resolve_agent(args.target)
    keys = tmux_keys(args.keys)
    guard_blocked(p, "send-keys")
    if p["status"] == "exited":
        fail(f"{args.target} has exited")
    run("send-keys", "-t", p["pane_id"], *keys)
    emit(args, {"pane_id": p["pane_id"], "keys": keys}, lambda d: print(f"sent {len(keys)} key(s) to {d['pane_id']}"))


def send_prompt(p, text):
    """Bracketed paste + 0.2 s + Enter, like `ta send`."""
    cp = current_pane()
    if cp:
        me = run("display", "-p", "-t", cp, "#{@ta_name}").strip() or os.environ.get("TA_NAME", "")
        text = (f'<ta from="{cp} {me}" reply="ta agent prompt {cp} ...">{text}</ta>')
    buf = f"ta-hd-{os.getpid()}"
    run("set-buffer", "-b", buf, "--", text)
    run("paste-buffer", "-p", "-d", "-b", buf, "-t", p["pane_id"])
    time.sleep(0.2)
    # The agent can ask a question while we type. Check again right before Enter.
    if os.environ.get("TA_ALLOW_BLOCKED") != "1" and status_of(p["pane_id"]) == "blocked":
        fail(f"{p['pane_id']} became blocked while the text was typed. Enter NOT pressed "
             f"(the text is still typed there). Read it with 'ta agent read {p['pane_id']}' and ask the user.")
    run("send-keys", "-t", p["pane_id"], "Enter")


def a_prompt(args):
    p = resolve_agent(args.target)
    if not args.text.strip():
        fail("prompt: empty text")
    guard_blocked(p, "prompt")
    if p["status"] == "exited":
        fail(f"{args.target} has exited")
    until = args.until or []
    for u in until:
        if u not in STATUSES:
            fail(f"bad --until '{u}' (use: {', '.join(STATUSES)})")
    send_prompt(p, args.text)
    result = {"pane_id": p["pane_id"], "sent": True}
    if args.wait or until:
        result["status"] = wait_for(p["pane_id"], until, args.timeout)
    emit(args, result, lambda d: print(f"sent to {d['pane_id']}" + (f"\n{d['status']}" if "status" in d else "")))


def a_wait(args):
    p = resolve_agent(args.target)
    until = args.until or []
    for u in until:
        if u not in STATUSES:
            fail(f"bad --until '{u}' (use: {', '.join(STATUSES)})")
    st = wait_for(p["pane_id"], until, args.timeout)
    emit(args, {"pane_id": p["pane_id"], "status": st}, lambda d: print(d["status"]))


def a_rename(args):
    p = resolve_pane(args.target)
    if args.clear == bool(args.new_name):
        fail("give a new name or --clear (not both)")
    old, session = p["name"], p["session_name"]
    if args.clear and not p["agent"]:
        # No agent here (a stale name): really clear the name and the registry line.
        run("set-option", "-p", "-u", "-t", p["pane_id"], "@ta_name")
        reg_del(old, session)
        new = None
    else:
        if args.clear:
            # A live agent must keep a name: `ta restore` finds its registry line
            # by name, and a line without a live name would start a 2nd copy.
            new = "pane-" + p["pane_id"].lstrip("%")
        else:
            new = args.new_name
            check_name(new)
        for o in all_panes():
            if o["name"] == new and o["pane_id"] != p["pane_id"] and o["agent"]:
                fail(f"name '{new}' is used by {o['pane_id']}")
        run("set-option", "-p", "-t", p["pane_id"], "@ta_name", new)
        if old:   # keep the registry line (restore) in step with the name
            refind_session_id(rename_reg(old, session, new, session))
    emit(args, {"pane_id": p["pane_id"], "name": new},
         lambda d: print(f"{d['pane_id']} name: {d['name'] or '(cleared)'}"
                         + (" (a running agent keeps a name; --clear gave it the pane name)"
                            if args.clear and d["name"] else "")))


def rename_reg(old_name, old_session, new_name, new_session):
    """Change the name / session columns of a registry line. Return its columns, or None."""
    if not REGISTRY.exists():
        return None
    # An old line with the new key would make a duplicate.
    if (old_name, old_session) != (new_name, new_session):
        reg_del(new_name, new_session)
    out, found = [], None
    for ln in REGISTRY.read_text().splitlines():
        c = ln.split("\t")
        if c[:2] == [old_name, old_session]:
            c[0], c[1] = new_name, new_session
            found = c
        out.append("\t".join(c))
    REGISTRY.write_text("".join(x + "\n" for x in out))
    return found


def start_finder(kind, name, session, folder, mark):
    """Background job (`ta _find`) that records the session id of a codex / agy agent."""
    subprocess.Popen([TA_BIN, "_find", kind, name, session, folder, str(mark)],
                     start_new_session=True, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, env=dict(os.environ, TA_SOCKET=SOCKET))


def refind_session_id(cols):
    """After a rename: the running finder looks for the OLD name / session, so it stops.
    If the line still has no session id (codex / agy), start a new finder.
    The old mark file is deleted by the old finder, so the new mark gets the
    start time of the registry line (minute precision). A session file made in
    the minute before the start is missed; one of another agent is skipped,
    because its id is already in the registry."""
    if not cols or len(cols) < 6:
        return
    name, session, folder, kind, sid, started = cols[:6]
    if sid or kind not in ("codex", "agy"):
        return
    STATE.mkdir(parents=True, exist_ok=True)
    mark = STATE / f".mark-rename-{os.getpid()}-{name}"
    mark.touch()
    try:
        stamp = datetime.strptime(started, "%Y-%m-%d %H:%M").timestamp()
        os.utime(mark, (stamp, stamp))
    except ValueError:
        pass                                    # bad date: the mark keeps "now"
    start_finder(kind, name, session, folder, mark)


def check_name(name):
    if not NAME_RE.match(name):
        fail(f"bad name '{name}': use [a-z][a-z0-9_-]{{0,31}}")


def focus_pane(p, hint_target):
    """Select the pane. Switch the client when inside tmux. Return True if switched."""
    run("select-window", "-t", p["window_id"])
    run("select-pane", "-t", p["pane_id"])
    if inside_tmux():
        run("switch-client", "-t", p["session_id"])
        return True
    return False


def attach_hint(session):
    sock = f"-L {SOCKET} " if SOCKET else ""
    return f"tmux {sock}attach -t '{session}'"


def a_focus(args):
    p = resolve_pane(args.target)
    switched = focus_pane(p, args.target)
    res = {"pane_id": p["pane_id"], "workspace": p["session_name"], "switched": switched}
    if not switched:
        res["attach"] = attach_hint(p["session_name"])
    emit(args, res, lambda d: print(f"focus {d['pane_id']}" if d["switched"] else
                                    f"selected {d['pane_id']}. Not inside tmux. Attach with: {d['attach']}"))


def a_attach(args):
    p = resolve_pane(args.target)
    if inside_tmux():
        return a_focus(args)
    run("select-window", "-t", p["window_id"])
    run("select-pane", "-t", p["pane_id"])
    cmd = ["tmux"] + (["-L", SOCKET] if SOCKET else []) + ["attach"] + \
          (["-d"] if args.takeover else []) + ["-t", p["session_id"]]
    sys.stdout.flush()
    os.execvp("tmux", cmd)


def a_start(args):
    check_name(args.name)
    kind = args.kind
    panes = all_panes()
    p = resolve_pane(args.pane, panes) if args.pane.startswith("%") else fail("--pane needs a pane id like %3")
    if p["pane_dead"] == "1":
        fail(f"pane {p['pane_id']} is dead")
    if p["agent"]:
        fail(f"pane {p['pane_id']} is busy: it already runs {p['agent']['kind']}. Use a free shell pane.")
    if p["pane_current_command"] == "fish":
        fail(f"pane {p['pane_id']} runs fish: the typed line needs bash or zsh syntax.")
    if p["pane_current_command"] not in SHELLS:
        fail(f"pane {p['pane_id']} is busy: it runs '{p['pane_current_command']}'. Use a free shell pane.")
    for o in panes:
        if o["name"] == args.name and o["pane_id"] != p["pane_id"]:
            if o["agent"]:
                fail(f"name '{args.name}' is already used by {o['pane_id']}")
            run("set-option", "-p", "-u", "-t", o["pane_id"], "@ta_name")   # stale name
    folder, session = p["pane_current_path"], p["session_name"]
    extra = [shlex.quote(a) for a in args.extra]
    sid = ""
    if kind == "claude":
        sid = str(uuid.uuid4())
        cmd = ["claude", "--session-id", sid, "-n", shlex.quote(args.name)] + extra
    else:
        cmd = [kind] + extra
    forget = f"{shlex.quote(TA_BIN)} forget {shlex.quote(args.name)} {shlex.quote(session)}"
    env = []
    if SOCKET:
        env.append(f"TA_SOCKET={shlex.quote(SOCKET)}")
    if os.environ.get("TA_STATE"):
        env.append(f"TA_STATE={shlex.quote(os.environ['TA_STATE'])}")
    if env:
        forget = "env " + " ".join(env) + " " + forget
    # The line typed into the shell pane. Valid in bash and zsh. After the agent
    # exits (any exit code) it clears the pane options. Without that, the bare
    # shell would still look like an agent, and a later `ta send` would type a
    # command into it and RUN it. Only a normal exit (code 0) forgets the registry line.
    tmux_cmd = "tmux " + (f"-L {shlex.quote(SOCKET)} " if SOCKET else "")
    pane_arg = shlex.quote(p["pane_id"])
    clear = " ".join(f"{tmux_cmd}set -pu -t {pane_arg} {opt};" for opt in ("@ta_name", "@ta_harness"))
    line = " ".join(cmd) + f"; rc=$?; {clear} [ $rc -eq 0 ] && " + forget

    mark = None
    if kind != "claude":
        STATE.mkdir(parents=True, exist_ok=True)
        mark = STATE / f".mark-{os.getpid()}"
        mark.touch()
    try:
        run("set-option", "-p", "-t", p["pane_id"], "@ta_name", args.name)
        run("set-option", "-p", "-t", p["pane_id"], "@ta_harness", kind)
        if inside_tmux():
            run("set-option", "-p", "-t", p["pane_id"], "@ta_parent", current_pane())
        STATE.mkdir(parents=True, exist_ok=True)
        REGISTRY.touch()
        reg_add(args.name, session, folder, kind, sid)
        run("send-keys", "-t", p["pane_id"], "-l", line)
        run("send-keys", "-t", p["pane_id"], "Enter")
        if mark:
            start_finder(kind, args.name, session, folder, mark)
        t0 = time.monotonic()
        while True:
            q = refresh(p["pane_id"])
            if q["agent"]:
                break
            if (time.monotonic() - t0) * 1000 >= args.timeout:
                fail(f"{kind} did not start in {args.timeout} ms. Read the pane: ta agent read {p['pane_id']}")
            time.sleep(0.3)
    except Fail:
        # A failed start (timeout, agent not found) must leave nothing behind:
        # no registry line (`ta restore` would start it) and no name on the pane.
        # The finder stops by itself when the registry line is gone.
        reg_del(args.name, session)
        for opt in ("@ta_name", "@ta_harness", "@ta_parent"):
            ta_ws.tmux("set-option", "-p", "-u", "-t", p["pane_id"], opt, check=False)
        if mark:
            mark.unlink(missing_ok=True)
        raise
    q["status"] = agent_status(q)
    emit(args, agent_obj(q), lambda d: print(f"started {d['name']} in {d['pane_id']}: {d['status']}"))


# --- workspace commands -----------------------------------------------------------

def counts_by_session():
    counts = {}
    for p in all_panes():
        c = counts.setdefault(p["session_id"], {"panes": 0, "working": 0, "blocked": 0, "idle": 0})
        c["panes"] += 1
        if p["agent"]:
            st = agent_status(p)
            if st in c:
                c[st] += 1
    return counts


def ws_obj(s, counts):
    c = counts.get(s["session_id"], {"panes": 0, "working": 0, "blocked": 0, "idle": 0})
    return {"id": s["session_id"], "number": s["num"], "label": s["session_name"],
            "tabs": int(s["session_windows"]), "panes": c["panes"],
            "agents": {"working": c["working"], "blocked": c["blocked"], "idle": c["idle"]},
            "attached": s["session_attached"] != "0", "cwd": s["session_path"]}


def w_list(args):
    counts = counts_by_session()
    rows = [ws_obj(s, counts) for s in workspaces()]

    def text(rows):
        table(["", "ID", "#", "LABEL", "TABS", "PANES", "AGENTS(W/B/I)"],
              [["*" if r["attached"] else "", r["id"], r["number"], r["label"], r["tabs"], r["panes"],
                f"{r['agents']['working']}/{r['agents']['blocked']}/{r['agents']['idle']}"] for r in rows])
    emit(args, rows, text)


def clean_label(label):
    return label.replace(":", " ").replace(".", " ").strip()


def check_envs(envs):
    out = []
    for e in envs or []:
        if "=" not in e or e.startswith("="):
            fail(f"bad --env '{e}': use KEY=VALUE")
        out += ["-e", e]
    return out


def check_cwd(cwd):
    path = os.path.abspath(os.path.expanduser(cwd or os.getcwd()))
    if not os.path.isdir(path):
        fail(f"no folder {path}")
    return path


def w_create(args):
    folder = check_cwd(args.cwd)
    label = clean_label(args.label or os.path.basename(folder.rstrip("/")) or "workspace")
    if not label:
        fail("empty label")
    need_server_or_start()
    if any(s["session_name"] == label for s in sessions_in_order(["session_name"])):
        fail(f"workspace '{label}' already exists")
    sid = run("new-session", "-d", "-P", "-F", "#{session_id}", "-s", label, "-c", folder,
              *check_envs(args.env)).strip()
    switched = False
    if args.focus:
        if inside_tmux():
            run("switch-client", "-t", sid)
            switched = True
    s = resolve_ws(sid)
    o = ws_obj(s, counts_by_session())
    o["focused"] = switched

    def text(o):
        print(f"created workspace {o['label']} ({o['id']}, #{o['number']})")
        if args.focus and not switched:
            print(f"Not inside tmux. Attach with: {attach_hint(o['label'])}")
    emit(args, o, text)


def need_server_or_start():
    """new-session can start the server, so no check here."""
    return True


def w_get(args):
    s = resolve_ws(args.workspace)
    o = ws_obj(s, counts_by_session())
    panes = all_panes()
    o["tab_list"] = [{"id": w["window_id"], "index": int(w["window_index"]), "label": w["window_name"],
                      "panes": len(window_panes(w["window_id"], panes)),
                      "active": w["window_active"] == "1"}
                     for w in windows() if w["session_id"] == s["session_id"]]

    def text(o):
        kv([("id", o["id"]), ("number", o["number"]), ("label", o["label"]), ("cwd", shorten(o["cwd"])),
            ("tabs", o["tabs"]), ("panes", o["panes"]),
            ("agents", f"working {o['agents']['working']}, blocked {o['agents']['blocked']}, idle {o['agents']['idle']}"),
            ("attached", "yes" if o["attached"] else "no")])
        print()
        table(["", "TAB", "ID", "LABEL", "PANES"],
              [["*" if t["active"] else "", f"{o['label']}:{t['index']}", t["id"], t["label"], t["panes"]]
               for t in o["tab_list"]])
    emit(args, o, text)


def w_focus(args):
    s = resolve_ws(args.workspace)
    switched = False
    if inside_tmux():
        run("switch-client", "-t", s["session_id"])
        switched = True
    res = {"id": s["session_id"], "label": s["session_name"], "switched": switched}
    if not switched:
        res["attach"] = attach_hint(s["session_name"])
    emit(args, res, lambda d: print(f"focus {d['label']}" if d["switched"] else
                                    f"Not inside tmux. Attach with: {d['attach']}"))


def w_rename(args):
    s = resolve_ws(args.workspace)
    label = clean_label(args.label)
    if not label:
        fail("empty label")
    for o in workspaces():
        if o["session_name"] == label and o["session_id"] != s["session_id"]:
            fail(f"workspace '{label}' already exists")
    old = s["session_name"]
    run("rename-session", "-t", s["session_id"], label)
    for n in {p["name"] for p in all_panes() if p["session_id"] == s["session_id"] and p["name"]}:
        refind_session_id(rename_reg(n, old, n, label))
    emit(args, {"id": s["session_id"], "label": label}, lambda d: print(f"{d['id']} label: {d['label']}"))


def w_close(args):
    s = resolve_ws(args.workspace)
    forget_panes([p for p in all_panes() if p["session_id"] == s["session_id"]])
    run("kill-session", "-t", s["session_id"])
    emit(args, {"id": s["session_id"], "label": s["session_name"], "closed": True},
         lambda d: print(f"closed workspace {d['label']} ({d['id']})"))


# --- tab commands -----------------------------------------------------------------

def tab_obj(w, panes):
    ps = window_panes(w["window_id"], panes)
    c = {"working": 0, "blocked": 0, "idle": 0}
    for p in ps:
        if p["agent"]:
            st = agent_status(p)
            if st in c:
                c[st] += 1
    return {"id": w["window_id"], "workspace": w["session_name"], "workspace_id": w["session_id"],
            "index": int(w["window_index"]), "ref": f"{w['session_name']}:{w['window_index']}",
            "label": w["window_name"], "panes": len(ps), "agents": c, "active": w["window_active"] == "1"}


def t_list(args):
    panes = all_panes()
    wins = windows()
    if args.workspace:
        s = resolve_ws(args.workspace)
        wins = [w for w in wins if w["session_id"] == s["session_id"]]
    rows = [tab_obj(w, panes) for w in wins]

    def text(rows):
        table(["", "ID", "TAB", "LABEL", "PANES", "AGENTS(W/B/I)"],
              [["*" if r["active"] else "", r["id"], r["ref"], r["label"], r["panes"],
                f"{r['agents']['working']}/{r['agents']['blocked']}/{r['agents']['idle']}"] for r in rows])
    emit(args, rows, text)


def t_create(args):
    s = current_ws(args.workspace)
    folder = check_cwd(args.cwd)
    cmd = ["new-window", "-d", "-P", "-F", "#{window_id}", "-t", s["session_id"] + ":", "-c", folder,
           *check_envs(args.env)]
    if args.label:
        cmd += ["-n", args.label]
    wid = run(*cmd).strip()
    if args.label:
        run("set-option", "-w", "-t", wid, "automatic-rename", "off")
    if args.focus:
        run("select-window", "-t", wid)
        if inside_tmux():
            run("switch-client", "-t", s["session_id"])
    w = resolve_tab(wid)
    o = tab_obj(w, all_panes())
    emit(args, o, lambda d: print(f"created tab {d['ref']} ({d['id']})"))


def t_get(args):
    w = resolve_tab(args.tab)
    panes = all_panes()
    o = tab_obj(w, panes)
    o["pane_list"] = [pane_obj(p) for p in window_panes(w["window_id"], panes)]

    def text(o):
        kv([("id", o["id"]), ("tab", o["ref"]), ("label", o["label"]), ("workspace", o["workspace"]),
            ("panes", o["panes"]), ("active", "yes" if o["active"] else "no")])
        print()
        table(["PANE", "CMD", "AGENT", "CWD"],
              [[p["pane_id"], p["command"], (p["agent"] or {}).get("kind", "-"), shorten(p["cwd"])]
               for p in o["pane_list"]])
    emit(args, o, text)


def t_focus(args):
    w = resolve_tab(args.tab)
    run("select-window", "-t", w["window_id"])
    switched = False
    if inside_tmux():
        run("switch-client", "-t", w["session_id"])
        switched = True
    res = {"id": w["window_id"], "switched": switched}
    if not switched:
        res["attach"] = attach_hint(w["session_name"])
    emit(args, res, lambda d: print(f"focus {d['id']}" if d["switched"] else
                                    f"selected {d['id']}. Not inside tmux. Attach with: {d['attach']}"))


def t_rename(args):
    w = resolve_tab(args.tab)
    run("rename-window", "-t", w["window_id"], args.label)
    run("set-option", "-w", "-t", w["window_id"], "automatic-rename", "off")
    emit(args, {"id": w["window_id"], "label": args.label}, lambda d: print(f"{d['id']} label: {d['label']}"))


def t_close(args):
    w = resolve_tab(args.tab)
    forget_panes(window_panes(w["window_id"]))
    run("kill-window", "-t", w["window_id"])
    emit(args, {"id": w["window_id"], "closed": True}, lambda d: print(f"closed tab {d['id']}"))


# --- pane commands ----------------------------------------------------------------

def pane_target(args, panes=None):
    """<pane> positional, else --pane, else the current pane."""
    ref = getattr(args, "pane", None) or getattr(args, "target", None)
    if ref:
        return resolve_pane(ref, panes)
    cp = current_pane()
    if not cp:
        fail("no pane given and not inside tmux")
    return resolve_pane(cp, panes)


def p_list(args):
    panes = all_panes()
    if args.workspace:
        s = resolve_ws(args.workspace)
        panes = [p for p in panes if p["session_id"] == s["session_id"]]
    rows = [pane_obj(p) for p in panes]

    def text(rows):
        table(["", "PANE", "WORKSPACE", "TAB", "CMD", "AGENT", "CWD"],
              [["*" if r["active"] else "", r["pane_id"], r["workspace"], f"{r['tab_index']}:{r['tab']}",
                r["command"], (r["agent"] or {}).get("kind", "-"), shorten(r["cwd"])] for r in rows])
    emit(args, rows, text)


def pane_text(o):
    kv([("pane", o["pane_id"]), ("workspace", f"{o['workspace']} ({o['workspace_id']})"),
        ("tab", f"{o['tab_index']}:{o['tab']} ({o['tab_id']})"), ("index", o["pane_index"]),
        ("command", o["command"]), ("cwd", shorten(o["cwd"])), ("title", o["title"] or "-"),
        ("agent", (o["agent"] or {}).get("kind", "-")), ("name", o["name"] or "-"),
        ("active", "yes" if o["active"] else "no")])


def p_current(args):
    cp = current_pane()
    if not cp:
        fail("not inside tmux")
    emit(args, pane_obj(resolve_pane(cp)), pane_text)


def p_get(args):
    emit(args, pane_obj(resolve_pane(args.pane)), pane_text)


def p_split(args):
    panes = all_panes()
    p = pane_target(args, panes)
    cmd = ["split-window", "-P", "-F", "#{pane_id}", "-t", p["pane_id"],
           "-h" if args.direction == "right" else "-v"]
    if not args.focus:
        cmd.append("-d")
    if args.ratio is not None:
        if not 0 < args.ratio < 1:
            fail("--ratio must be between 0 and 1 (share of the new pane)")
        cmd += ["-l", f"{round(args.ratio * 100)}%"]
    if args.cwd:
        cmd += ["-c", check_cwd(args.cwd)]
    cmd += check_envs(args.env)
    new = run(*cmd).strip()
    o = pane_obj(resolve_pane(new))
    emit(args, o, lambda d: print(f"split {p['pane_id']} {args.direction}: new pane {d['pane_id']}"))


def p_focus(args):
    p = pane_target(args)
    flag = {"left": "-L", "right": "-R", "up": "-U", "down": "-D"}[args.direction]
    run("select-pane", flag, "-t", p["pane_id"])
    now = run("display", "-p", "-t", p["window_id"], "#{pane_id}").strip()
    emit(args, {"pane_id": now}, lambda d: print(f"focus {d['pane_id']}"))


def p_zoom(args):
    p = pane_target(args)
    zoomed = p["window_zoomed_flag"] == "1"
    want = True if args.on else False if args.off else (not zoomed)
    if want != zoomed:
        run("resize-pane", "-Z", "-t", p["pane_id"])
    emit(args, {"pane_id": p["pane_id"], "zoomed": want},
         lambda d: print(f"{d['pane_id']} zoom: {'on' if d['zoomed'] else 'off'}"))


def p_rename(args):
    p = resolve_pane(args.pane)
    if args.clear == (args.label is not None):
        fail("give a label or --clear (not both)")
    title = "" if args.clear else args.label
    run("select-pane", "-t", p["pane_id"], "-T", title)
    emit(args, {"pane_id": p["pane_id"], "title": title},
         lambda d: print(f"{d['pane_id']} title: {d['title'] or '(cleared)'}"))


def p_close(args):
    need_server()
    for row in tmux_rows("list-panes", ["pane_id", "@ta_side"]):
        if row["pane_id"] == args.pane and row["@ta_side"] == "1":
            fail(f"{args.pane} is a sidebar pane. Hide it with the sidebar key instead.")
    p = resolve_pane(args.pane)
    forget_panes([p])
    run("kill-pane", "-t", p["pane_id"])
    emit(args, {"pane_id": p["pane_id"], "closed": True}, lambda d: print(f"closed {d['pane_id']}"))


def p_read(args):
    p = resolve_pane(args.pane)
    out = read_pane(p["pane_id"], args.source, args.lines)
    emit(args, {"pane_id": p["pane_id"], "source": args.source, "text": out}, lambda d: print(d["text"]))


def p_send_text(args):
    p = resolve_pane(args.pane)
    guard_blocked(p, "send-text")
    run("send-keys", "-t", p["pane_id"], "-l", "--", args.text)
    emit(args, {"pane_id": p["pane_id"], "sent": True}, lambda d: print(f"sent text to {d['pane_id']}"))


def p_send_keys(args):
    p = resolve_pane(args.pane)
    keys = tmux_keys(args.keys)
    guard_blocked(p, "send-keys")
    run("send-keys", "-t", p["pane_id"], *keys)
    emit(args, {"pane_id": p["pane_id"], "keys": keys}, lambda d: print(f"sent {len(keys)} key(s) to {d['pane_id']}"))


def p_wait_output(args):
    p = resolve_pane(args.pane)
    try:
        rx = re.compile(args.regex if args.regex else re.escape(args.match))
    except re.error as e:
        fail(f"bad regex: {e}")
    t0 = time.monotonic()
    while True:
        text = read_pane(p["pane_id"], "recent", args.lines)
        for line in text.split("\n"):
            if rx.search(line):
                emit(args, {"pane_id": p["pane_id"], "matched": True, "line": line}, lambda d: print(d["line"]))
                return
        if args.timeout is not None and (time.monotonic() - t0) * 1000 >= args.timeout:
            fail(f"timeout after {args.timeout} ms: no output matched")
        time.sleep(0.5)


# --- argparse ---------------------------------------------------------------------

class Fmt(argparse.RawDescriptionHelpFormatter):
    pass


def positive(s):
    v = int(s)
    if v < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return v


def ms(s):
    v = int(s)
    if v < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return v


def build(group):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print JSON (for scripts and agents)")
    ap = argparse.ArgumentParser(prog=f"ta {group}", description=GROUPS[group][0], formatter_class=Fmt)
    sub = ap.add_subparsers(dest="cmd", metavar="<command>", help=argparse.SUPPRESS)
    ap.usage = f"ta {group} <command> [options]"
    summaries = []

    def add(name, summary, example, func):
        summaries.append((name, summary))
        sp = sub.add_parser(name, parents=[common], description=summary,
                            formatter_class=Fmt, epilog="Example:\n  " + example)
        sp.set_defaults(func=func)
        return sp

    def wait_flags(sp):
        sp.add_argument("--until", action="append", metavar="STATUS",
                        help="stop when the status is working|idle|done|blocked|exited (repeat for more). "
                             "done = idle after working was seen. Default: done, blocked, exited, or "
                             "idle after 10 s with no work")
        sp.add_argument("--timeout", type=ms, metavar="MS", help="give up after MS milliseconds (default: wait forever)")

    def src_flags(sp):
        sp.add_argument("--source", choices=["visible", "recent"], default="visible",
                        help="visible = the screen; recent = screen + history (default: visible)")
        sp.add_argument("--lines", type=positive, default=80, metavar="N",
                        help="how many lines for --source recent (default: 80)")

    def pos_or_flag(sp, what="pane"):
        sp.add_argument("pane", nargs="?", help=f"{what} id or @ta_name")
        sp.add_argument("--pane", dest="pane_flag", metavar="ID", help=f"same as the {what} argument")

    def new_flags(sp):
        sp.add_argument("--cwd", metavar="PATH", help="start folder (default: current folder)")
        sp.add_argument("--env", action="append", metavar="K=V", help="set an environment variable (repeat)")
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--focus", dest="focus", action="store_true", help="switch to it")
        g.add_argument("--no-focus", dest="focus", action="store_false", help="stay where you are (default)")
        sp.set_defaults(focus=False)

    if group == "agent":
        add("list", "List every agent (claude, codex, agy) in tmux panes.", "ta agent list --json", a_list)
        add("get", "Show 1 agent: status, place, folder, session id.", "ta agent get reviewer", a_get) \
            .add_argument("target", help="@ta_name or pane id (%%N)")
        sp = add("read", "Read the text of an agent's pane.", "ta agent read reviewer --source recent --lines 120", a_read)
        sp.add_argument("target", help="@ta_name or pane id (%%N)")
        src_flags(sp)
        sp = add("send-keys", "Press keys in an agent pane. Refused when it is blocked.",
                 "ta agent send-keys reviewer esc", a_send_keys)
        sp.add_argument("target", help="@ta_name or pane id (%%N)")
        sp.add_argument("keys", nargs="+", metavar="key",
                        help="enter esc tab space backspace up down left right pageup pagedown home end delete ctrl+<letter>")
        sp = add("prompt", "Type a message + Enter into an agent. Refused when it is blocked.",
                 'ta agent prompt reviewer "Check the diff" --wait', a_prompt)
        sp.add_argument("target", help="@ta_name or pane id (%%N)")
        sp.add_argument("text", help="the message")
        sp.add_argument("--wait", action="store_true", help="then wait until it is done")
        wait_flags(sp)
        sp = add("wait", "Wait until an agent reaches a status.", "ta agent wait reviewer --until done --timeout 600000", a_wait)
        sp.add_argument("target", help="@ta_name or pane id (%%N)")
        wait_flags(sp)
        sp = add("rename", "Set or clear the name (@ta_name) of an agent pane.", "ta agent rename %3 reviewer", a_rename)
        sp.add_argument("target", help="@ta_name or pane id (%%N)")
        sp.add_argument("new_name", nargs="?", help="new name: [a-z][a-z0-9_-]{0,31}")
        sp.add_argument("--clear", action="store_true", help="remove the name")
        add("focus", "Go to the agent's workspace, tab and pane.", "ta agent focus reviewer", a_focus) \
            .add_argument("target", help="@ta_name or pane id (%%N)")
        sp = add("attach", "Open the agent in this terminal (tmux attach if you are outside tmux).",
                 "ta agent attach reviewer --takeover", a_attach)
        sp.add_argument("target", help="@ta_name or pane id (%%N)")
        sp.add_argument("--takeover", action="store_true", help="detach other clients first (attach -d)")
        sp = add("start", "Start claude, codex or agy in a free shell pane (typed into the pane).",
                 "ta agent start reviewer --kind claude --pane %3 -- --model opus", a_start)
        sp.usage = "ta agent start <name> --kind claude|codex|agy --pane ID [--timeout MS] [--json] [-- extra args]"
        sp.add_argument("name", help="[a-z][a-z0-9_-]{0,31}, unique among live agents")
        sp.add_argument("--kind", required=True, choices=list(HARNESSES), help="which agent to run")
        sp.add_argument("--pane", required=True, metavar="ID", help="a free shell pane, like %%3")
        sp.add_argument("--timeout", type=ms, default=15000, metavar="MS", help="wait for the agent process (default: 15000)")
        sp.set_defaults(extra=[])
    elif group == "workspace":
        add("list", "List workspaces (tmux sessions): #, tabs, panes, agents.", "ta workspace list --json", w_list)
        sp = add("create", "Make a workspace.", "ta workspace create --cwd ~/code/api --label api", w_create)
        sp.add_argument("--label", help="name (default: folder name)")
        new_flags(sp)
        add("get", "Show 1 workspace and its tabs.", "ta workspace get 2", w_get).add_argument("workspace", help=WS_REF)
        add("focus", "Switch the tmux client to a workspace.", "ta workspace focus api", w_focus).add_argument("workspace", help=WS_REF)
        sp = add("rename", "Change the label of a workspace.", "ta workspace rename api api-v2", w_rename)
        sp.add_argument("workspace", help=WS_REF)
        sp.add_argument("label")
        add("close", "Close a workspace and everything in it.", "ta workspace close api", w_close).add_argument("workspace", help=WS_REF)
    elif group == "tab":
        sp = add("list", "List tabs (tmux windows).", "ta tab list --workspace api", t_list)
        sp.add_argument("--workspace", metavar="WS", help=WS_REF + " (default: all)")
        sp = add("create", "Make a tab in a workspace.", "ta tab create --workspace api --label tests", t_create)
        sp.add_argument("--workspace", metavar="WS", help=WS_REF + " (default: the current one)")
        sp.add_argument("--label", help="tab name")
        new_flags(sp)
        add("get", "Show 1 tab and its panes.", "ta tab get api:1", t_get).add_argument("tab", help=TAB_REF)
        add("focus", "Go to a tab.", "ta tab focus @4", t_focus).add_argument("tab", help=TAB_REF)
        sp = add("rename", "Change the name of a tab.", "ta tab rename api:1 build", t_rename)
        sp.add_argument("tab", help=TAB_REF)
        sp.add_argument("label")
        add("close", "Close a tab and its panes.", "ta tab close api:2", t_close).add_argument("tab", help=TAB_REF)
    elif group == "pane":
        sp = add("list", "List panes (sidebar panes are hidden).", "ta pane list --workspace api", p_list)
        sp.add_argument("--workspace", metavar="WS", help=WS_REF + " (default: all)")
        add("current", "Show the pane you are in ($TMUX_PANE).", "ta pane current --json", p_current)
        add("get", "Show 1 pane.", "ta pane get %3", p_get).add_argument("pane", help="pane id (%%N) or @ta_name")
        sp = add("split", "Split a pane. The new pane gets --ratio of the space.",
                 "ta pane split %3 --direction right --ratio 0.4 --cwd ~/code", p_split)
        pos_or_flag(sp)
        sp.add_argument("--current", action="store_true", help="split the pane you are in (the default)")
        sp.add_argument("--direction", required=True, choices=["right", "down"], help="where the new pane goes")
        sp.add_argument("--ratio", type=float, metavar="FLOAT", help="share of the new pane, 0 to 1 (default: half)")
        new_flags(sp)
        sp = add("focus", "Move focus to the pane left, right, up or down.", "ta pane focus --direction left", p_focus)
        pos_or_flag(sp)
        sp.add_argument("--direction", required=True, choices=["left", "right", "up", "down"])
        sp = add("zoom", "Zoom a pane to the full tab, or back.", "ta pane zoom %3 --on", p_zoom)
        pos_or_flag(sp)
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--toggle", action="store_true", help="switch (default)")
        g.add_argument("--on", action="store_true")
        g.add_argument("--off", action="store_true")
        sp = add("rename", "Set or clear the title of a pane.", "ta pane rename %3 server", p_rename)
        sp.add_argument("pane", help="pane id (%%N) or @ta_name")
        sp.add_argument("label", nargs="?")
        sp.add_argument("--clear", action="store_true", help="remove the title")
        add("close", "Close a pane (not a sidebar pane).", "ta pane close %3", p_close).add_argument("pane", help="pane id (%%N) or @ta_name")
        sp = add("read", "Read the text of a pane.", "ta pane read %3 --source recent --lines 200", p_read)
        sp.add_argument("pane", help="pane id (%%N) or @ta_name")
        src_flags(sp)
        sp = add("send-text", "Type text into a pane (no Enter). Refused when an agent there is blocked.",
                 'ta pane send-text %3 "npm test"', p_send_text)
        sp.add_argument("pane", help="pane id (%%N) or @ta_name")
        sp.add_argument("text")
        sp = add("send-keys", "Press keys in a pane. Refused when an agent there is blocked.",
                 "ta pane send-keys %3 ctrl+c enter", p_send_keys)
        sp.add_argument("pane", help="pane id (%%N) or @ta_name")
        sp.add_argument("keys", nargs="+", metavar="key",
                        help="enter esc tab space backspace up down left right pageup pagedown home end delete ctrl+<letter>")
        sp = add("wait-output", "Wait until the pane shows some text.",
                 'ta pane wait-output %3 --match "listening on" --timeout 30000', p_wait_output)
        sp.add_argument("pane", help="pane id (%%N) or @ta_name")
        g = sp.add_mutually_exclusive_group(required=True)
        g.add_argument("--match", metavar="TEXT", help="plain text to find")
        g.add_argument("--regex", metavar="RE", help="Python regular expression to find")
        sp.add_argument("--lines", type=positive, default=200, metavar="N", help="look at the last N lines (default: 200)")
        sp.add_argument("--timeout", type=ms, metavar="MS", help="give up after MS milliseconds (default: wait forever)")
    wid = max(len(n) for n, _ in summaries)
    ap.epilog = ("commands:\n" + "\n".join(f"  {n.ljust(wid)}  {t}" for n, t in summaries) +
                 f"\n\nRun 'ta {group} <command> --help' for the flags and an example.\n"
                 "Every command takes --json. Errors: 1 line on stderr, exit 1.")
    return ap


WS_REF = ("workspace: $id, order number or label (see `ta workspace list`). "
          "A pure number is the order number first, then a label.")
TAB_REF = "tab: @id or workspace:index"
GROUPS = {
    "agent": ("Agents: claude, codex and agy running in tmux panes.\n"
              "Targets are a pane id (%%N) or an @ta_name. Status is guessed from the screen.\n"
              "Safety: prompt and send-keys refuse a blocked agent. A person answers permission prompts."
              .replace("%%", "%"), ),
    "workspace": ("Workspaces = tmux sessions.", ),
    "tab": ("Tabs = tmux windows (inside a workspace).", ),
    "pane": ("Panes = tmux panes.", ),
}


def main(argv):
    if not argv or argv[0] not in GROUPS:
        sys.exit("usage: ta_hd.py agent|workspace|tab|pane ...")
    group, rest = argv[0], argv[1:]
    extra = []
    if group == "agent" and rest[:1] == ["start"] and "--" in rest:
        i = rest.index("--")
        rest, extra = rest[:i], rest[i + 1:]
    ap = build(group)
    args = ap.parse_args(rest)
    if not args.cmd:
        ap.print_help()
        return 0
    if group == "agent" and args.cmd == "start":
        args.extra = extra
    # --pane flag and positional pane are the same thing
    if hasattr(args, "pane_flag"):
        if args.pane and args.pane_flag:
            ap.error("give the pane once (argument or --pane)")
        args.pane = args.pane or args.pane_flag
    if args.cmd == "zoom" and not args.on and not args.off:
        args.toggle = True
    try:
        args.func(args)
    except Fail as e:
        print(f"ta: {e}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
