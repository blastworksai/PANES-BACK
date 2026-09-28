#!/usr/bin/env python3
"""PANES-BACK: after a VS Code reset, find every coding-agent session that died and write a resume sheet.

A VS Code server restart (an update, a failed Remote-SSH reconnect, a crash) kills every integrated
terminal at once, and every agent CLI running in them. The transcripts all survive on disk. This script
finds them, says what each session was doing when it died, and writes one resume line per session.

Read-only apart from the sheet it writes (created 0600 in a 0700 folder).

  panes_back.py cut                      # print the cut moment: the newest VS Code server start on this machine
  panes_back.py scan [--cut "YYYY-MM-DD HH:MM"] [--before-min 45] [--after-min 5] [--hours 8]
                     [--config FILE] [--sheet-dir DIR] [--no-sheet]

What `scan` does, in order:
  1. cut       : newest VS Code `code-server` start (ps etimes) unless --cut is given.
  2. live      : every running agent CLI process, per unix user, and Claude Code's `claude agents --json --all`
                 (session id -> pid). A process is tied to a session by the id on its command line, by holding
                 the transcript open, or (Claude) by `claude agents`; a live process that can't be tied to one
                 session makes that user's matching sessions "liveness unresolved" rather than dead.
  3. files     : per home, transcripts touched in the last --hours: Claude Code projects/*.jsonl,
                 Codex sessions/rollout-*.jsonl, Antigravity conversations/*.db, Kimi sessions/*/session_*/state.json,
                 Grok sessions/*/*/chat_history.jsonl.
  4. summary   : first real ask (slash commands rendered), last ask, last reply, status, via transcripts.py.
  5. class     : alive · cut (last write inside [cut - before, cut + after], not live) · older (still resumable)
                 · headless-finished (a sub-agent / headless run that finished AFTER the cut: its result sits only
                   in its file) · headless-cut (one cut mid-run: resume or re-run its parent, never it on its own).
  6. resume    : a configured pane's launch line (in the one supported shape, see README) rebuilt with the CLI's
                 resume verb and the session's folder (claude --resume <id> · codex resume <id> ·
                 agy --conversation <id> · kimi -S <id> · grok -r <id>), or a plain line when no pane matches.
  7. sheet     : markdown at <sheet-dir>/resume-sheet-<cut>.md; stdout ends with one JSON line.

Users. By default only your own home is scanned and nothing uses sudo. To scan several unix accounts
(one per agent, say), set "users" in the config to a list or to "all"; other homes are then read through
`sudo -n` and the resume lines start with `sudo -u <user>`.

Config (optional, JSON): --config, else $PANES_BACK_CONFIG, else ~/.config/panes-back/config.json.
See config.example.json for every key.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import pwd
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone, tzinfo
from urllib.parse import unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.dont_write_bytecode = True  # read-only means no __pycache__ beside the script either
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import transcripts  # noqa: E402  (the per-CLI transcript readers)

ME = getpass.getuser()
AGY_TMP_HEADLESS = True  # set from the config

# The CLIs this knows: where each keeps its transcripts, and how it resumes one.
FINDS = {
    "claude": (".claude/projects", ["-maxdepth", "2", "-name", "*.jsonl"]),
    "codex": (".codex/sessions", ["-name", "rollout-*.jsonl"]),
    "agy": (".gemini/antigravity-cli/conversations", ["-maxdepth", "1", "-name", "*.db"]),
    "kimi": (".kimi-code/sessions", ["-maxdepth", "3", "-name", "state.json", "-path", "*/session_*/state.json"]),
    "grok": (".grok/sessions", ["-maxdepth", "3", "-name", "chat_history.jsonl"]),
}
CLIS = tuple(FINDS)
RESUME_VERB = {"claude": ["--resume"], "codex": ["resume"], "agy": ["--conversation"], "kimi": ["-S"], "grok": ["-r"]}
SAFE_ID = re.compile(r"[A-Za-z0-9._:-]+")
# Session-selector options per CLI: the value of one of these (a separate argument) names the session a process runs.
SELECTORS = {"claude": ("--resume", "-r", "--session-id"), "codex": (), "agy": ("--conversation",),
             "kimi": ("-S",), "grok": ("-r", "--resume")}
RUNTIMES = {"node", "bun", "deno", "python", "python3"}
SHELLS = {"bash", "sh", "zsh", "dash"}
SKIP_BARE_COMMANDS = {"/clear", "/model", "/effort", "/resume", "/context", "/cost", "/status", "/help"}
CMD_RE = re.compile(r"<command-name>(.*?)</command-name>", re.S)
ARG_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
DEFAULT_HINT = "Open a terminal (one per session), paste the line."


def local_zone() -> tzinfo:
    """The machine's zone with its full rules, so a past cut gets that date's offset (not today's)."""
    name = os.environ.get("TZ", "").lstrip(":")
    candidates = []
    if name.startswith("/"):
        candidates.append(("file", name))
    elif name:
        candidates.append(("key", name))
    real = os.path.realpath("/etc/localtime")
    if "/zoneinfo/" in real:
        candidates.append(("key", real.split("/zoneinfo/", 1)[1]))
    try:
        with open("/etc/timezone", encoding="utf-8") as fh:
            candidates.append(("key", fh.read().strip()))
    except OSError:
        pass
    candidates.append(("file", "/etc/localtime"))  # a copied (not linked) zone file still carries its rules
    for kind, value in candidates:
        try:
            if kind == "key":
                return ZoneInfo(value)
            with open(value, "rb") as fh:
                return ZoneInfo.from_file(fh)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            continue
    return timezone.utc


TZ: tzinfo = local_zone()  # replaced by the config's "timezone", if set


def now_local() -> datetime:
    return datetime.now(TZ)


def run(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        return 1, f"{e}"
    return r.returncode, r.stdout


def as_owner(user: str, cmd: list[str]) -> list[str]:
    """The command as-is for your own files; through `sudo -n` for another user's."""
    return cmd if user == ME else ["sudo", "-n", *cmd]


def parse_local(s: str | None) -> datetime | None:
    """transcripts.to_local() prints 'YYYY-MM-DD HH:MM:SS' in TZ; bring it back as a datetime."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
    except ValueError:
        return None


def hm(dt: datetime | None) -> str:
    return dt.strftime("%H:%M") if dt else "?"


# ------------------------------------------------------------------ config
def load_config(path: str | None) -> dict:
    path = path or os.environ.get("PANES_BACK_CONFIG") or os.path.expanduser("~/.config/panes-back/config.json")
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def home_of(user: str) -> str | None:
    try:
        return pwd.getpwnam(user).pw_dir
    except KeyError:
        return None


def users_to_scan(cfg: dict) -> list[tuple[str, str]]:
    """(user, home) pairs. "self" (default) · "all" (every home under /home, through sudo) · a list of users."""
    want = cfg.get("users", "self")
    if want == "self":
        return [(ME, os.path.expanduser("~"))]
    if want == "all":
        rc, out = run(["sudo", "-n", "find", "/home", "-mindepth", "1", "-maxdepth", "1", "-type", "d"])
        pairs = []
        for p in sorted(x for x in out.split("\n") if x):
            u = os.path.basename(p)
            pairs.append((u, home_of(u) or p))
        return pairs
    return [(u, h) for u in want if (h := home_of(u))]


# ------------------------------------------------------------------ 1. the cut
def detect_cut() -> tuple[datetime | None, str | None]:
    rc, out = run(["ps", "-eo", "etimes,user:20,args"])
    best = None
    for line in out.splitlines()[1:]:
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        et, user, args = parts
        if ".vscode-server/" in args and "server/bin/code-server" in args:
            et = int(et)
            if best is None or et < best[0]:
                best = (et, user)
    if best is None:
        return None, None
    return now_local() - timedelta(seconds=best[0]), best[1]


# ------------------------------------------------------------------ 2. what is alive
def proc_argv(pid: int) -> list[str] | None:
    """The process's real argument list (ps joins it with spaces and loses the boundaries)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a] or None


def argv_cli(argv: list[str]) -> tuple[str | None, list[str]]:
    """Which CLI this process runs, judged from its program (or the script a runtime runs), and its own args."""
    for i in range(min(2, len(argv))):
        name = os.path.basename(argv[i])
        if name in CLIS:
            return name, argv[i + 1:]
        if name not in RUNTIMES:
            break
    return None, []


def selected_ids(cli: str, args: list[str]) -> set[str]:
    """Session ids this process was started on: codex `resume <id>`, or a selector option's separate value."""
    ids = set()
    if cli == "codex":
        pos = [a for a in args if not a.startswith("-")]
        if len(pos) >= 2 and pos[0] == "resume":
            ids.add(pos[1])
        return ids
    for i, a in enumerate(args):
        if a in SELECTORS.get(cli, ()) and i + 1 < len(args):
            ids.add(args[i + 1])
        elif "=" in a and a.split("=", 1)[0] in SELECTORS.get(cli, ()):
            ids.add(a.split("=", 1)[1])
    return ids


def live_processes() -> list[dict]:
    rc, out = run(["ps", "-eo", "user:20,pid,etimes"])
    procs = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 3:
            continue
        user, pid, et = parts[0], int(parts[1]), int(parts[2])
        argv = proc_argv(pid)
        if not argv:
            continue
        cli, args = argv_cli(argv)
        if not cli or "app-server" in args and "daemon" in args:
            continue
        procs.append({"user": user, "pid": pid, "etimes": et, "cli": cli,
                      "mode": "app-server" if "app-server" in args else "tui", "args": " ".join(argv)[:140],
                      "ids": selected_ids(cli, args), "open": open_files(pid)})
    return procs


def open_files(pid: int) -> set[str]:
    """Paths the process holds open, where this user may look (its own processes, or any as root)."""
    fd_dir = f"/proc/{pid}/fd"
    try:
        return {os.readlink(os.path.join(fd_dir, fd)) for fd in os.listdir(fd_dir)}
    except OSError:
        return set()


def claude_agents(user: str, warnings: list[str]) -> dict:
    cmd = ["claude", "agents", "--json", "--all"]
    rc, out = run(cmd if user == ME else ["sudo", "-n", "-u", user, "-H", *cmd], timeout=40)
    try:
        data = json.loads(out)
    except Exception:
        warnings.append(f"`claude agents` gave no answer for {user}: a live Claude session of theirs is matched only "
                        f"by its command line or open transcript")
        return {}
    res = {}
    for a in data if isinstance(data, list) else []:
        sid, pid = a.get("sessionId"), a.get("pid")
        res[sid] = {"pid": pid, "alive": bool(pid) and os.path.isdir(f"/proc/{pid}"),
                    "name": a.get("name"), "kind": a.get("kind"), "state": a.get("status") or a.get("state")}
    return res


# ------------------------------------------------------------------ 3. the files
def find_transcripts(since: datetime, pairs: list[tuple[str, str]]) -> list[dict]:
    stamp = f"@{int(since.timestamp())}"  # absolute: find would read a bare date in its own process zone
    found = []
    for user, home in pairs:
        for cli, (sub, args) in FINDS.items():
            base = os.path.join(home, sub)
            rc, _ = run(as_owner(user, ["test", "-d", base]))
            if rc != 0:
                continue
            rc, out = run(as_owner(user, ["find", base, "-type", "f", *args, "-newermt", stamp,
                                          "-printf", "%T@\t%s\t%p\n"]), timeout=120)
            for line in out.splitlines():
                t, s, p = line.split("\t", 2)
                found.append({"user": user, "home": home, "cli": cli, "path": p, "size": int(s),
                              "mtime": datetime.fromtimestamp(float(t), TZ)})
    return found


# ------------------------------------------------------------------ 4. summaries
def claude_asks(text: str) -> list[tuple[str | None, str]]:
    """Every real user ask, with slash commands rendered as '/name args' and bare housekeeping ones dropped."""
    asks = []
    for d in transcripts.jsonl(text):
        if d.get("type") != "user" or d.get("isMeta") or d.get("isSidechain"):
            continue
        txt, _ = transcripts.claude_text((d.get("message") or {}).get("content"))
        txt = txt.strip()
        if not txt or txt.startswith("[Request interrupted"):
            continue
        m = CMD_RE.search(txt)
        if m:
            a = ARG_RE.search(txt)
            txt = (m.group(1).strip() + " " + (a.group(1).strip() if a else "")).strip()
            if txt in SKIP_BARE_COMMANDS:
                continue
        elif txt.startswith("<"):
            continue
        asks.append((d.get("timestamp"), re.sub(r"\s+", " ", txt)))
    return asks


def codex_last_assistant_full(text: str, limit: int = 3500) -> str | None:
    last = None
    for d in transcripts.jsonl(text):
        p = d.get("payload") or {}
        if d.get("type") == "response_item" and p.get("type") == "message" and p.get("role") == "assistant":
            t = transcripts.codex_msg_text(p).strip()
            if t:
                last = t
    return last[:limit] if last else None


def claude_last_assistant_full(text: str, limit: int = 3500) -> str | None:
    last = None
    for d in transcripts.jsonl(text):
        if d.get("type") == "assistant" and not d.get("isSidechain"):
            t, _ = transcripts.claude_text((d.get("message") or {}).get("content"))
            if t.strip():
                last = t.strip()
    return last[:limit] if last else None


def agy_cwd_map(home: str) -> dict:
    text = transcripts.read_text(os.path.join(home, ".gemini/antigravity-cli/cache/last_conversations.json"))
    try:
        return {v: k for k, v in json.loads(text or "{}").items()}
    except Exception:
        return {}


def summarise(f: dict) -> dict:
    cli, path = f["cli"], f["path"]
    s = {"user": f["user"], "cli": cli, "path": path, "mtime": f["mtime"], "size": f["size"],
         "id": None, "cwd": None, "first_ask": None, "last_ask": None, "last_said": None, "parent_id": None,
         "status": "unknown", "first_ts": None, "last_ts": None, "headless": False, "subagents": 0,
         "full_last": None, "liveness": None}
    if cli == "claude":
        e = transcripts.extract_claude(path)
        text = transcripts.read_text(path) or ""
        asks = claude_asks(text)
        s.update(id=e.get("session_id"), cwd=e.get("cwd"), status=e.get("status", "unknown"),
                 first_ts=parse_local(e.get("first_ts")), last_ts=parse_local(e.get("last_ts")),
                 last_said=e.get("last_assistant"))
        s["first_ask"] = asks[0][1][:220] if asks else e.get("first_user")
        s["last_ask"] = asks[-1][1][:220] if asks else e.get("last_user")
        # Claude Code records how it was started: "cli" is interactive, "sdk-*" is a headless `claude -p` run.
        s["headless"] = (e.get("entrypoint") or "").startswith("sdk")
        rc, out = run(as_owner(f["user"], ["find", path[:-6] + "/subagents", "-maxdepth", "1", "-name", "*.jsonl"]))
        s["subagents"] = len([x for x in out.split("\n") if x]) if rc == 0 else 0
        s["full_last"] = claude_last_assistant_full(text)
    elif cli == "codex":
        e = transcripts.extract_codex(path)
        s.update(id=e.get("file_thread_id"), cwd=e.get("cwd"), status=e.get("status", "unknown"),
                 first_ts=parse_local(e.get("first_ts")), last_ts=parse_local(e.get("last_ts")),
                 first_ask=e.get("first_user"), last_ask=e.get("last_user"),
                 last_said=e.get("last_assistant"), parent_id=e.get("parent_id"))
        # `codex exec` runs and sub-agent threads: Codex refuses to resume a sub-agent on its own.
        s["headless"] = e.get("source") in ("exec", "subagent") or bool(e.get("is_child"))
        s["full_last"] = codex_last_assistant_full(transcripts.read_text(path) or "")
    elif cli == "agy":
        cid = os.path.basename(path)[:-3]
        cwd = agy_cwd_map(f["home"]).get(cid)
        s.update(id=cid, cwd=cwd, status="not parsed (sqlite conversation)", last_ts=f["mtime"])
        # Antigravity records no launch mode; a conversation rooted in a temp folder is taken as a headless run
        # (a known limit; "agy_tmp_is_headless": false in the config turns it off).
        s["headless"] = AGY_TMP_HEADLESS and bool(cwd) and any(cwd == t or cwd.startswith(t + "/")
                                                               for t in ("/tmp", "/var/tmp"))
        s["first_ask"] = f"Antigravity conversation in {cwd or '(cwd unknown)'}"
    elif cli == "kimi":
        sdir = os.path.dirname(path)
        e = transcripts.extract_kimi(sdir)
        s.update(id=os.path.basename(sdir), cwd=e.get("cwd"), first_ask=e.get("title") or e.get("first_user"),
                 last_ask=e.get("last_user"), last_said=e.get("last_assistant"),
                 last_ts=parse_local(e.get("last_ts") or e.get("updated")) or f["mtime"], status="see kimi")
    elif cli == "grok":
        sdir = os.path.dirname(path)
        e = transcripts.extract_grok(sdir)
        cwd = e.get("cwd") or unquote(os.path.basename(os.path.dirname(sdir)))
        s.update(id=os.path.basename(sdir), cwd=cwd, first_ask=e.get("title") or e.get("first_user"),
                 last_ask=e.get("last_user"), last_said=e.get("last_assistant"), status=e.get("status", "unknown"),
                 last_ts=parse_local(e.get("last_active") or e.get("updated")) or f["mtime"],
                 parent_id=e.get("parent"))
    if s["last_ts"] is None:
        s["last_ts"] = f["mtime"]
    return s


# ------------------------------------------------------------------ 5. classify
def live_owner(s: dict, agents: dict, procs: list[dict]) -> str | None:
    """Why this exact session is known to be running, or None."""
    a = agents.get(s["user"], {}).get(s["id"]) if s["cli"] == "claude" else None
    if a and a["alive"]:
        return f"pid {a['pid']} ({a.get('kind')}, {a.get('state')})"
    for p in procs:
        if p["user"] == s["user"] and p["cli"] == s["cli"] and (s["id"] in p["ids"] or s["path"] in p["open"]):
            return f"{p['mode']} pid {p['pid']}"
    return None


def classify(s: dict, cut: datetime, before: timedelta, after: timedelta, agents: dict, procs: list[dict],
             tied_pids: set[int]) -> None:
    lt = s["last_ts"]
    why = live_owner(s, agents, procs)
    if why:
        s["klass"], s["alive_why"] = "alive", why
        return
    in_window = cut - before <= lt <= cut + after
    # A live process of this user+CLI that nothing could tie to a session may be this one: say so, don't guess.
    loose = [p for p in procs if p["user"] == s["user"] and p["cli"] == s["cli"] and p["pid"] not in tied_pids]
    if loose:
        s["liveness"] = (f"unresolved: {len(loose)} running {s['cli']} process(es) of {s['user']} could not be tied "
                         f"to a session (pid {', '.join(str(p['pid']) for p in loose)}); check it isn't one of "
                         f"them before resuming or re-running")
    if s["headless"]:
        finished = s["status"].startswith("idle") and "aborted" not in s["status"]
        if not s["first_ts"]:
            s["start_unknown"] = True  # no start time recorded: it may have started after the reset
        if s["first_ts"] and s["first_ts"] > cut:
            s["klass"], s["alive_why"] = "written-after-cut", "a headless run started after the cut"
        elif finished:
            s["klass"] = "headless-finished" if lt > cut else "older"  # ended before the cut: its parent had it
        elif in_window:
            s["klass"] = "headless-cut"
        elif lt > cut + after:
            s["klass"], s["alive_why"] = "written-after-cut", "a headless run still writing after the cut"
        else:
            s["klass"] = "older"
        return
    if lt > cut + after:
        how = f"{len(loose)} running {s['cli']} process(es) of {s['user']} not tied to any session" if loose \
            else "no running process seen"
        s["klass"], s["alive_why"] = "written-after-cut", f"written after the cut; {how}"
        return
    s["klass"] = "cut" if in_window else "older"


def tied_pids(sessions: list[dict], agents: dict, procs: list[dict]) -> set[int]:
    """Every live process some transcript can be tied to."""
    ids = {(s["user"], s["cli"], s["id"]) for s in sessions}
    paths = {(s["user"], s["cli"], s["path"]) for s in sessions}
    tied = {a["pid"] for u in agents.values() for a in u.values() if a.get("alive")}
    for p in procs:
        if any((p["user"], p["cli"], i) in ids for i in p["ids"]) or \
                any((p["user"], p["cli"], o) in paths for o in p["open"]):
            tied.add(p["pid"])
    return tied


# ------------------------------------------------------------------ 5b. keep only pane work
def keep_pane_work(sessions: list[dict], panes: list[dict], panes_complete: bool) -> list[dict]:
    """Drop what is not interactive work of yours. A headless run is kept only when it finished after the cut or
    was cut mid-run. When panes are configured, a user+CLI with no pane is background work (a scheduled worker,
    a bot) and is dropped too, unless it is alive or written after the cut: a session driven from another
    machine is still yours. If a pane that runs a supported CLI has a launch line that could not be read, the
    filter is off: that pane might be the one a session belongs to."""
    pane_keys = {(p["user"], p["cli"]) for p in panes} if panes_complete else set()
    kept = []
    for s in sessions:
        if s["headless"] and s["klass"] not in ("headless-finished", "headless-cut"):
            continue
        if pane_keys and (s["user"], s["cli"]) not in pane_keys and s["klass"] not in ("alive", "written-after-cut"):
            continue
        kept.append(s)
    return kept


# ------------------------------------------------------------------ 6. resume lines
# A launch line is understood only in one bounded shape, and every resume line is rebuilt from its parts:
#   [sudo [-n] [-H] -u USER]  [bash|sh -c|-lc '<inner>']  where <inner> (or the line itself) is
#   [exec] [env [-C DIR] [VAR=value ...]] <cli> [args ...]
# Anything else (shell operators, redirections, other wrappers) is not guessed at: the session gets a plain line.
SHELL_META = set(";&|<>`$()\n")
ENV_VAR = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")


def parse_launch(launch: str) -> dict | None:
    try:
        toks = shlex.split(launch)
    except ValueError:
        return None
    if not toks:
        return None
    lp = {"sudo": [], "user": None, "shell": None, "exec": False, "env_vars": [], "binary": None, "cli": None,
          "args": []}
    i = 0
    if os.path.basename(toks[0]) == "sudo":
        i = 1
        while i < len(toks) and toks[i].startswith("-"):
            t = toks[i]
            if t in ("-u", "--user") and i + 1 < len(toks):
                lp["user"] = toks[i + 1]
                lp["sudo"] += [t, toks[i + 1]]
                i += 2
            elif t.startswith("--user="):
                lp["user"] = t.split("=", 1)[1]
                lp["sudo"].append(t)
                i += 1
            elif t in ("-n", "-H", "--non-interactive", "--set-home"):
                lp["sudo"].append(t)
                i += 1
            else:
                return None
        if lp["user"] is None:
            return None
    body = toks[i:]
    if body and os.path.basename(body[0]) in SHELLS:
        if len(body) != 3 or not re.fullmatch(r"-[a-z]*c", body[1]):
            return None
        lp["shell"] = [body[0], body[1]]
        try:
            body = shlex.split(body[2])
        except ValueError:
            return None
    if any(ch in SHELL_META for t in body for ch in t):
        return None  # can't tell quoted data from shell syntax once parsed: don't rebuild it
    if body and body[0] == "exec":
        lp["exec"], body = True, body[1:]
    if body and os.path.basename(body[0]) == "env":
        j = 1
        while j < len(body):
            t = body[j]
            if t in ("-C", "--chdir") and j + 1 < len(body):
                j += 2
            elif t.startswith("--chdir="):
                j += 1
            elif ENV_VAR.fullmatch(t):
                lp["env_vars"].append(t)
                j += 1
            else:
                break
        body = body[j:]
    if not body or os.path.basename(body[0]) not in CLIS:
        return None
    lp["binary"], lp["cli"], lp["args"] = body[0], os.path.basename(body[0]), body[1:]
    return lp


def strip_selectors(cli: str, args: list[str]) -> list[str]:
    """A launch that already resumes or continues something: drop that, the recovered session replaces it."""
    args = list(args)
    if cli == "codex":
        pos = [i for i, a in enumerate(args) if not a.startswith("-")]
        if pos and args[pos[0]] == "resume":
            k = pos[0]
            drop = {k}
            if k + 1 < len(args) and (args[k + 1] == "--last" or not args[k + 1].startswith("-")):
                drop.add(k + 1)
            args = [a for i, a in enumerate(args) if i not in drop]
        return args
    flags = set(SELECTORS.get(cli, ())) | set(RESUME_VERB[cli])
    out, j = [], 0
    while j < len(args):
        a = args[j]
        if a in flags:
            j += 2 if j + 1 < len(args) and not args[j + 1].startswith("-") else 1
            continue
        if "=" in a and a.split("=", 1)[0] in flags or (cli == "claude" and a in ("-c", "--continue")):
            j += 1
            continue
        out.append(a)
        j += 1
    return out


def build_line(lp: dict, sid: str, cwd: str) -> str:
    """The launch rebuilt with the resume verb, in the session's folder (set inside the target user's scope)."""
    cli, args = lp["cli"], strip_selectors(lp["cli"], lp["args"])
    if cli == "codex":  # its own -C/--cd would win over env -C: point every form of it at the session's folder
        out, j = [], 0
        while j < len(args):
            t = args[j]
            if t in ("-C", "--cd") and j + 1 < len(args):
                out += [t, cwd]
                j += 2
                continue
            if t.startswith("--cd="):
                t = "--cd=" + cwd
            elif t.startswith("-C") and len(t) > 2:
                t = "-C" + cwd
            out.append(t)
            j += 1
        cmd = [lp["binary"], *RESUME_VERB[cli], sid, *out]
    else:
        cmd = [lp["binary"], *args, *RESUME_VERB[cli], sid]
    inner = (["exec"] if lp["exec"] or lp["shell"] else []) + ["env", "-C", cwd, *lp["env_vars"], *cmd]
    if lp["shell"]:
        run_it = [*lp["shell"], shlex.join(inner)]
    else:
        run_it = inner[1:] if inner[0] == "exec" and lp["sudo"] else inner
    return shlex.join((["sudo", *lp["sudo"]] if lp["sudo"] else []) + run_it)


def load_panes(cfg: dict, warnings: list[str]) -> tuple[list[dict], bool]:
    """Panes from the config's "panes" list, plus any read from a VS Code Terminals Manager terminals.json."""
    raw = [(p.get("name", ""), p.get("launch", ""), p.get("purpose", ""), p.get("user"))
           for p in cfg.get("panes", [])]
    vt = cfg.get("vscode_terminals")
    if vt:
        try:
            with open(os.path.expanduser(vt["path"]), encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            d = {}
            warnings.append(f"could not read {vt['path']}")
        for t in d.get("terminals", []):
            env = t.get("env") or {}
            launch = env.get(vt["launch_env"], "") if vt.get("launch_env") else " && ".join(t.get("commands") or [])
            purpose = env.get(vt["purpose_env"], "") if vt.get("purpose_env") else ""
            if launch:
                raw.append((t.get("name", ""), launch, purpose, None))
    panes, complete = [], True
    for name, launch, purpose, user in raw:
        lp = parse_launch(launch)
        if lp is None:
            # Only a refused line that runs a supported CLI could own one of the sessions found.
            if any(os.path.basename(w) in CLIS for w in re.split(r"[\s'\"]+", launch)):
                complete = False
            warnings.append(f"pane {name or '(unnamed)'}: its launch line is not in the supported shape "
                            f"(see README), so its sessions get plain resume lines")
            continue
        if user and lp["user"] and user != lp["user"]:
            warnings.append(f"pane {name}: \"user\" says {user} but its launch runs as {lp['user']}; using {lp['user']}")
        elif user and user != ME and not lp["user"]:  # run the pane's command as the user the config names
            lp.update(sudo=["-u", user, "-H"], user=user, shell=lp["shell"] or ["bash", "-lc"])
        who = lp["user"] or ME
        home = home_of(who) or os.path.expanduser("~")
        # `~/…` expands in the target user's home: once quoted, the shell would no longer expand it.
        lp["binary"] = home + lp["binary"][1:] if lp["binary"].startswith("~/") else lp["binary"]
        lp["args"] = [home + a[1:] if a.startswith("~/") else a for a in lp["args"]]
        panes.append({"name": name, "user": who, "cli": lp["cli"], "purpose": purpose, "lp": lp})
    return panes, complete


def resume_line(s: dict, panes: list[dict], cfg: dict) -> tuple[str | None, str | None]:
    cli, user, sid = s["cli"], s["user"], s["id"]
    if not sid or cli not in RESUME_VERB or not SAFE_ID.fullmatch(sid):
        return None, None
    cands = [p for p in panes if p["user"] == user and p["cli"] == cli]
    fa = s.get("first_ask") or ""
    want = next((r["purpose"] for r in cfg.get("purpose_rules", []) if fa.startswith(r["first_ask_prefix"])),
                cfg.get("default_purpose"))
    pane = next((p for p in cands if want and p["purpose"] == want), cands[0] if cands else None)
    cwd = s.get("cwd") or home_of(user) or os.path.expanduser("~")
    if pane:
        return build_line(pane["lp"], sid, cwd), pane["name"]
    plain = {"sudo": ["-u", user, "-H"] if user != ME else [], "user": user, "shell": ["bash", "-lc"] if user != ME
             else None, "exec": False, "env_vars": [], "binary": cli, "cli": cli, "args": []}
    return build_line(plain, sid, cwd), None


# ------------------------------------------------------------------ 7. the sheet
def sheet_dir_default() -> str:
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(state, "panes-back")


def one_liner(s: dict) -> str:
    fa = (s.get("first_ask") or "").strip()
    return (fa[:150] + "…") if len(fa) > 150 else (fa or "(no ask recorded)")


def label(s: dict, names: dict) -> str:
    n = names.get(s["user"])
    return f"{n} ({s['user']}, {s['cli']})" if n else f"{s['user']}, {s['cli']}"


def write_sheet(path: str, cut: datetime, cut_user: str | None, sessions: list[dict], panes: list[dict],
                names: dict, hint: str, warnings: list[str]) -> None:
    by = lambda k: [s for s in sessions if s["klass"] == k]  # noqa: E731
    L = [f"# Resume sheet — cut at {cut.strftime('%d %b %Y %H:%M')} (VS Code server restart as {cut_user or '?'})", "",
         "Every terminal under VS Code died together; every transcript is intact. " + hint, ""]
    for w in warnings:
        L.append(f"> Note: {w}")
    if warnings:
        L.append("")
    L.append("## Cut sessions")
    L.append("")
    cut_s = sorted(by("cut"), key=lambda s: (s["user"], s["cli"], s["last_ts"]))
    if not cut_s:
        L.append("_none_")
    for s in cut_s:
        sub = f"; {s['subagents']} sub-agent transcript(s) under its subagents/ folder, all dead" if s["subagents"] else ""
        L += [f"### {label(s, names)} — {one_liner(s)}",
              f"- last write {s['last_ts'].strftime('%H:%M')}, {s['status']}{sub}",
              f"- last ask: {(s.get('last_ask') or '')[:200]}",
              f"- last said: {(s.get('last_said') or '')[:240]}"]
        if s.get("liveness"):
            L.append(f"- **liveness {s['liveness']}**")
        if s.get("resume"):
            L += [f"- pane: {s.get('pane') or '(no matching pane; plain line)'}", "```", s["resume"], "```"]
        else:
            L.append("- no resume line (no resume verb for this CLI, or an id that is not safe to paste); "
                     "start it fresh and point it at the transcript path below")
        L += [f"- transcript: `{s['path']}`", ""]
    fin = by("headless-finished")
    hcut = by("headless-cut")
    if fin or hcut:
        L += ["## Fell in the gap", ""]
        for s in fin:
            parent = f" (parent `{s['parent_id']}`)" if s.get("parent_id") else ""
            L += [f"### FINISHED after the cut — {label(s, names)} — {one_liner(s)}",
                  f"- ended {s['last_ts'].strftime('%H:%M')}; its parent{parent} never received this"
                  + (" (its start time is not recorded, so it may have started after the reset)"
                     if s.get("start_unknown") else "") + ". Full last message:",
                  "", "> " + (s.get("full_last") or "(none)").replace("\n", "\n> "), "",
                  f"- file: `{s['path']}`", ""]
        for s in hcut:
            parent = f"resume its parent `{s['parent_id']}` and re-run it from there" if s.get("parent_id") \
                else "its parent must re-run it"
            unknown = " Its start time is not recorded, so it may have started after the reset." \
                if s.get("start_unknown") else ""
            L += [f"- CUT mid-run — {label(s, names)} — {one_liner(s)} (last write "
                  f"{s['last_ts'].strftime('%H:%M')}); never resume this one on its own: {parent}. `{s['path']}`"
                  + unknown
                  + (f" **Liveness {s['liveness']}.**" if s.get("liveness") else "")]
        L.append("")
    alive = by("alive") + by("written-after-cut")
    if alive:
        L += ["## Still alive, untouched", ""]
        for s in alive:
            L.append(f"- {label(s, names)} — {one_liner(s)} — {s.get('alive_why')}; "
                     f"last write {s['last_ts'].strftime('%H:%M')}")
        L.append("")
    older = by("older")
    if older:
        L += ["## Older, still resumable (idle before the cut)", ""]
        for s in sorted(older, key=lambda s: s["last_ts"], reverse=True):
            L.append(f"- {label(s, names)} — {one_liner(s)} — last write "
                     f"{s['last_ts'].strftime('%d %b %H:%M')} — id `{s['id']}`"
                     + (f" — **liveness {s['liveness']}**" if s.get("liveness") else ""))
        L.append("")
    seen = {(s["user"], s["cli"]) for s in sessions if s["klass"] != "older"}
    quiet = sorted({p["name"] for p in panes if (p["user"], p["cli"]) not in seen})
    if quiet:
        L += ["## Nothing to bring back", "", "No live conversation in the window for: " + ", ".join(quiet) + ".", ""]
    # Sheets quote transcripts: private to you (mkstemp makes it 0600), and written to a fresh file that then
    # replaces the name, so a planted symlink at that name is replaced, never followed.
    fd, tmp = tempfile.mkstemp(prefix=".resume-sheet-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(L))
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


# ------------------------------------------------------------------ main
def apply_config(cfg: dict) -> None:
    global TZ, AGY_TMP_HEADLESS
    AGY_TMP_HEADLESS = cfg.get("agy_tmp_is_headless", True)
    if cfg.get("timezone"):
        TZ = ZoneInfo(cfg["timezone"])
    transcripts.set_timezone(TZ)
    transcripts.allow_sudo(cfg.get("users", "self") != "self")


def cmd_cut(args) -> int:
    apply_config(load_config(args.config))
    cut, user = detect_cut()
    if not cut:
        print("no VS Code server process found; pass --cut to scan")
        return 1
    print(f"cut {cut.strftime('%Y-%m-%d %H:%M:%S')} (newest code-server start, user {user})")
    return 0


def cmd_scan(args) -> int:
    cfg = load_config(args.config)
    apply_config(cfg)
    if args.cut:
        cut = datetime.strptime(args.cut, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        cut_user = None
    else:
        cut, cut_user = detect_cut()
        if not cut:
            print("no VS Code server process found; pass --cut 'YYYY-MM-DD HH:MM'")
            return 1
    before, after = timedelta(minutes=args.before_min), timedelta(minutes=args.after_min)
    since = min(cut - before, now_local() - timedelta(hours=args.hours))
    warnings: list[str] = []
    procs = live_processes()
    files = find_transcripts(since, users_to_scan(cfg))
    sessions = [summarise(f) for f in files]
    agents = {u: claude_agents(u, warnings) for u in sorted({s["user"] for s in sessions if s["cli"] == "claude"})}
    panes, panes_complete = load_panes(cfg, warnings)
    names = cfg.get("names", {})
    tied = tied_pids(sessions, agents, procs)
    for s in sessions:
        classify(s, cut, before, after, agents, procs, tied)
    sessions = keep_pane_work(sessions, panes, panes_complete)
    for s in sessions:
        s["resume"], s["pane"] = resume_line(s, panes, cfg) if s["klass"] in ("cut", "older") else (None, None)
    sheet = None
    if not args.no_sheet:
        d = os.path.expanduser(args.sheet_dir or cfg.get("sheet_dir") or sheet_dir_default())
        os.makedirs(d, mode=0o700, exist_ok=True)
        sheet = os.path.join(d, f"resume-sheet-{cut.strftime('%Y-%m-%d-%H%M')}.md")
        write_sheet(sheet, cut, cut_user, sessions, panes, names, cfg.get("resume_hint", DEFAULT_HINT), warnings)
    order = {"cut": 0, "headless-finished": 1, "headless-cut": 2, "alive": 3, "written-after-cut": 4, "older": 5}
    print(f"cut {cut.strftime('%Y-%m-%d %H:%M')}  window -{args.before_min}m/+{args.after_min}m  "
          f"transcripts touched since {since.strftime('%H:%M')}: {len(sessions)}")
    for w in warnings:
        print(f"  note: {w}")
    for s in sorted(sessions, key=lambda s: (order.get(s["klass"], 9), s["user"], s["last_ts"])):
        print(f"  {s['klass']:<17} {hm(s['last_ts'])}  {names.get(s['user'], s['user']):<10} {s['cli']:<6} "
              f"{(s.get('pane') or ''):<17} {one_liner(s)[:90]}")
    if sheet:
        print(f"sheet: {sheet}")
    counts = {}
    for s in sessions:
        counts[s["klass"]] = counts.get(s["klass"], 0) + 1
    out = {"cut": cut.isoformat(), "cut_user": cut_user, "sheet": sheet, "counts": counts, "warnings": warnings,
           "sessions": [{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in s.items()
                         if k not in ("full_last",)} for s in sessions]}
    print(json.dumps(out, default=str))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="verb", required=True)
    ct = sub.add_parser("cut")
    ct.add_argument("--config", help="config JSON (default: $PANES_BACK_CONFIG, else ~/.config/panes-back/config.json)")
    ct.set_defaults(fn=cmd_cut)
    sc = sub.add_parser("scan")
    sc.add_argument("--cut", help="the reset moment, 'YYYY-MM-DD HH:MM' local; default: newest code-server start")
    sc.add_argument("--before-min", type=int, default=45, help="a last write this long before the cut still counts as cut")
    sc.add_argument("--after-min", type=int, default=5, help="a last write this long after the cut still counts as cut")
    sc.add_argument("--hours", type=int, default=8, help="how far back to look for transcripts at all")
    sc.add_argument("--config", help="config JSON (default: $PANES_BACK_CONFIG, else ~/.config/panes-back/config.json)")
    sc.add_argument("--sheet-dir", help="where the sheet goes (default: config sheet_dir, else ~/.local/state/panes-back)")
    sc.add_argument("--no-sheet", action="store_true")
    sc.set_defaults(fn=cmd_scan)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
