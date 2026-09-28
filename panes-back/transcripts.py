#!/usr/bin/env python3
"""Read-only transcript readers for the coding-agent CLIs panes-back knows about.

Each reader takes a transcript path (or session folder) and returns one summary dict:
who asked what first and last, what the agent last said, and whether the session stopped
mid-turn or idle at its prompt.

A file this process can read is opened directly. A file it may not read (another unix
user's home) is read through `sudo -n cat` only when the caller has turned that on with
allow_sudo(True), which needs passwordless sudo; nothing is ever written.

  transcripts.py [--sudo] claude <session.jsonl> [...]     (--sudo: read another user's files via sudo -n cat)
  transcripts.py codex  <rollout-*.jsonl> [...]
  transcripts.py grok   <session-dir> [...]
  transcripts.py agy    <transcript.jsonl> [...]
  transcripts.py kimi   <session-dir> [...]
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, tzinfo

LOCAL_TZ: tzinfo = timezone.utc
USE_SUDO = False
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def set_timezone(tz: tzinfo) -> None:
    """Every timestamp this module prints is rendered in this zone (default: the machine's)."""
    global LOCAL_TZ
    LOCAL_TZ = tz


def allow_sudo(on: bool) -> None:
    """Let read_text fall back to `sudo -n cat` for a file this process may not read."""
    global USE_SUDO
    USE_SUDO = on


def read_text(path):
    """The file's text, or None when it is missing or unreadable."""
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", errors="replace")
    except PermissionError:
        pass  # another user's file: sudo below, if allowed
    except OSError:
        return None
    if not USE_SUDO:
        return None
    try:
        r = subprocess.run(["sudo", "-n", "cat", path], capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", errors="replace")


def to_local(ts):
    """ISO-8601 (Z or offset), epoch seconds, or epoch millis -> 'YYYY-MM-DD HH:MM:SS' in LOCAL_TZ."""
    if ts is None:
        return None
    try:
        if isinstance(ts, (int, float)):
            if ts > 1e12:
                ts = ts / 1000.0
            d = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            s = str(ts).strip()
            if s.isdigit():
                return to_local(int(s))
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            d = datetime.fromisoformat(s)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def clip(s, n):
    if s is None:
        return None
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s[:n]


def jsonl(text):
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


# ---------------------------------------------------------------- Claude Code
SKIP_USER_PREFIX = ("<local-command-stdout>", "<local-command-caveat>", "<system-reminder>")


def claude_text(content):
    if isinstance(content, str):
        return content, False
    texts, has_tool_result = [], False
    for b in content or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            texts.append(b.get("text", ""))
        elif b.get("type") == "tool_result":
            has_tool_result = True
    return "\n".join(texts), has_tool_result


def extract_claude(path):
    text = read_text(path)
    out = {"provider": "claude", "path": path, "session_id": os.path.basename(path)[:-6]}
    if text is None:
        out["error"] = "unreadable"
        return out
    cwd = entrypoint = None
    first_user = last_user = last_asst = None
    last_kind = None
    last_ts = None
    n_user = n_asst = 0
    first_ts = None
    for d in jsonl(text):
        t = d.get("type")
        if cwd is None and d.get("cwd"):
            cwd = d["cwd"]
        if entrypoint is None and d.get("entrypoint"):
            entrypoint = d["entrypoint"]  # "cli" interactive · "sdk-cli" a headless `claude -p` run
        if t not in ("user", "assistant"):
            continue
        if d.get("isSidechain"):
            continue
        ts = d.get("timestamp")
        if ts:
            last_ts = ts
            if first_ts is None:
                first_ts = ts
        msg = d.get("message") or {}
        if t == "user":
            if d.get("isMeta"):
                continue
            txt, is_tool_result = claude_text(msg.get("content"))
            if is_tool_result and not txt.strip():
                last_kind = "tool_result"
                continue
            if not txt.strip() or txt.lstrip().startswith(SKIP_USER_PREFIX):
                continue
            n_user += 1
            if first_user is None:
                first_user = txt
            last_user = txt
            last_kind = "user_text"
        else:
            content = msg.get("content") or []
            has_tool_use = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)
            txt, _ = claude_text(content)
            if txt.strip():
                last_asst = txt
                n_asst += 1
            last_kind = "assistant_tool_use" if has_tool_use else "assistant_text"
    status = {
        "user_text": "mid-turn (user message unanswered)",
        "tool_result": "mid-turn (tool result returned, reply pending)",
        "assistant_tool_use": "mid-turn (tool call in flight)",
        "assistant_text": "idle at prompt",
        None: "unknown (no turns)",
    }[last_kind]
    out.update({
        "cwd": cwd,
        "entrypoint": entrypoint,
        "first_ts": to_local(first_ts),
        "last_ts": to_local(last_ts),
        "n_user": n_user,
        "n_assistant": n_asst,
        "first_user": clip(first_user, 120),
        "last_user": clip(last_user, 160),
        "last_assistant": clip(last_asst, 200),
        "last_kind": last_kind,
        "status": status,
    })
    return out


# ---------------------------------------------------------------- Codex
CODEX_SKIP = ("<environment_context>", "<user_instructions>", "<permissions", "# AGENTS.md",
              "<turn_aborted>", "<skill", "<system", "<app_context>")


def codex_msg_text(payload):
    parts = []
    for c in payload.get("content") or []:
        if isinstance(c, dict) and c.get("type") in ("input_text", "output_text", "text"):
            parts.append(c.get("text", ""))
    return "\n".join(parts)


def extract_codex(path):
    text = read_text(path)
    fname = os.path.basename(path)
    m = re.search(r"rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-([0-9a-f-]{36})\.jsonl$", fname)
    out = {"provider": "codex", "path": path,
           "file_thread_id": m.group(2) if m else None,
           "file_local_start": m.group(1) if m else None}
    if text is None:
        out["error"] = "unreadable"
        return out
    meta_id = cwd = source = None
    first_user = last_user = last_asst = None
    first_ts = last_ts = None
    n_user = n_asst = 0
    last_task_started = last_task_done = last_aborted = None
    last_item_kind = None
    pending_calls = 0
    for d in jsonl(text):
        ts = d.get("timestamp")
        if ts:
            last_ts = ts
            first_ts = first_ts or ts
        t = d.get("type")
        p = d.get("payload") or {}
        if t == "session_meta":
            meta_id = p.get("id")
            cwd = p.get("cwd")
            src = p.get("source")
            if isinstance(src, str):
                source = src
            elif isinstance(src, dict) and "subagent" in src:
                source = "subagent"
                spawn = (src.get("subagent") or {}).get("thread_spawn") or {}
                out["spawn_parent_thread_id"] = spawn.get("parent_thread_id")
            else:
                source = json.dumps(src)[:80]
            continue
        if t == "turn_context" and p.get("cwd"):
            cwd = p.get("cwd")
            continue
        if t == "event_msg":
            et = p.get("type")
            if et == "task_started":
                last_task_started = ts
            elif et == "task_complete":
                last_task_done = ts
            elif et == "turn_aborted":
                last_aborted = ts
            continue
        if t == "response_item":
            pt = p.get("type")
            if pt == "message":
                role = p.get("role")
                txt = codex_msg_text(p)
                if role == "user":
                    if not txt.strip() or txt.lstrip().startswith(CODEX_SKIP):
                        continue
                    n_user += 1
                    if first_user is None:
                        first_user = txt
                    last_user = txt
                    last_item_kind = "user_text"
                elif role == "assistant":
                    if txt.strip():
                        last_asst = txt
                        n_asst += 1
                    last_item_kind = "assistant_text"
            elif pt in ("function_call", "custom_tool_call", "local_shell_call", "web_search_call"):
                pending_calls += 1
                last_item_kind = "tool_call"
            elif pt in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
                pending_calls = max(0, pending_calls - 1)
                last_item_kind = "tool_output"

    def _key(x):
        return x or ""
    in_flight = last_task_started and _key(last_task_started) > max(_key(last_task_done), _key(last_aborted))
    if in_flight:
        if last_item_kind == "user_text":
            status = "mid-turn (user message unanswered)"
        elif last_item_kind in ("tool_call", "tool_output"):
            status = "mid-turn (tool call in flight)"
        else:
            status = "mid-turn (turn started, not completed)"
    elif last_item_kind == "user_text":
        status = "mid-turn (user message unanswered)"
    elif last_aborted and _key(last_aborted) >= _key(last_task_done):
        status = "idle at prompt (last turn aborted)"
    elif last_item_kind is None:
        status = "unknown (no turns)"
    else:
        status = "idle at prompt"
    out.update({
        "session_meta_id": meta_id,
        "is_child": bool(meta_id and out["file_thread_id"] and meta_id != out["file_thread_id"]) or source == "subagent",
        "parent_id": out.get("spawn_parent_thread_id") or (meta_id if meta_id != out["file_thread_id"] else None),
        "source": source,
        "cwd": cwd,
        "first_ts": to_local(first_ts),
        "last_ts": to_local(last_ts),
        "n_user": n_user,
        "n_assistant": n_asst,
        "first_user": clip(first_user, 120),
        "last_user": clip(last_user, 160),
        "last_assistant": clip(last_asst, 200),
        "last_kind": last_item_kind,
        "status": status,
    })
    return out


# ---------------------------------------------------------------- Grok
def extract_grok(sdir):
    out = {"provider": "grok", "path": sdir, "session_id": os.path.basename(sdir.rstrip("/"))}
    summ = read_text(os.path.join(sdir, "summary.json"))
    chat = read_text(os.path.join(sdir, "chat_history.jsonl"))
    if summ is None and chat is None:
        out["error"] = "unreadable"
        return out
    if summ:
        try:
            s = json.loads(summ)
            out["cwd"] = (s.get("info") or {}).get("cwd")
            out["title"] = s.get("generated_title") or s.get("session_summary")
            out["agent_name"] = s.get("agent_name")
            out["created"] = to_local(s.get("created_at"))
            out["updated"] = to_local(s.get("updated_at"))
            out["last_active"] = to_local(s.get("last_active_at"))
            out["parent"] = s.get("parent_session_id")
            out["n_chat_messages"] = s.get("num_chat_messages")
        except Exception as e:
            out["summary_error"] = str(e)
    first_user = last_user = last_asst = None
    last_kind = None
    if chat:
        for d in jsonl(chat):
            t = d.get("type")
            if t == "user":
                c = d.get("content")
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                if not c:
                    continue
                if first_user is None:
                    first_user = c
                last_user = c
                last_kind = "user_text"
            elif t == "assistant":
                c = d.get("content")
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                if c:
                    last_asst = c
                last_kind = "assistant_text"
            elif t in ("tool_call", "function_call"):
                last_kind = "tool_call"
            elif t in ("tool_result", "tool", "function_call_output"):
                last_kind = "tool_output"
    status = {
        "user_text": "mid-turn (user message unanswered)",
        "tool_call": "mid-turn (tool call in flight)",
        "tool_output": "mid-turn (tool result returned, reply pending)",
        "assistant_text": "idle at prompt",
        None: "unknown (no turns)",
    }[last_kind]
    out.update({"first_user": clip(first_user, 120), "last_user": clip(last_user, 160),
                "last_assistant": clip(last_asst, 200), "last_kind": last_kind, "status": status})
    return out


# ---------------------------------------------------------------- Antigravity (agy)
def extract_agy(path):
    text = read_text(path)
    m = re.search(r"/brain/([0-9a-f-]{36})/", path)
    out = {"provider": "agy", "path": path, "conversation_id": m.group(1) if m else None}
    if text is None:
        out["error"] = "unreadable"
        return out
    first_user = last_user = last_model = None
    last_type = last_status = last_ts = None
    n_steps = 0
    for d in jsonl(text):
        n_steps += 1
        last_type, last_status, last_ts = d.get("type"), d.get("status"), d.get("created_at")
        c = d.get("content") or ""
        if d.get("type") == "USER_INPUT":
            if first_user is None:
                first_user = c
            last_user = c
        elif d.get("source") == "MODEL" and c:
            last_model = c
    if last_type == "USER_INPUT":
        status = "mid-turn (user message unanswered)"
    elif last_status and last_status != "DONE":
        status = f"mid-turn (last step {last_type} status {last_status})"
    elif last_type is None:
        status = "unknown (no steps)"
    else:
        status = "idle (last step done)"
    out.update({"n_steps": n_steps, "last_ts": to_local(last_ts), "last_type": last_type,
                "last_status": last_status, "first_user": clip(first_user, 120),
                "last_user": clip(last_user, 160), "last_model": clip(last_model, 200), "status": status})
    return out


# ---------------------------------------------------------------- Kimi
def extract_kimi(sdir):
    out = {"provider": "kimi", "path": sdir, "session_id": os.path.basename(sdir.rstrip("/"))}
    st = read_text(os.path.join(sdir, "state.json"))
    wire = read_text(os.path.join(sdir, "agents", "main", "wire.jsonl"))
    if st is None and wire is None:
        out["error"] = "unreadable"
        return out
    if st:
        try:
            s = json.loads(st)
            out.update({"cwd": s.get("cwd"), "title": s.get("title"), "created": to_local(s.get("createdAt")),
                        "updated": to_local(s.get("updatedAt")), "last_prompt_state": clip(s.get("lastPrompt"), 160),
                        "last_turn_reason": s.get("lastTurnReason")})
        except Exception as e:
            out["state_error"] = str(e)
    types = {}
    first_user = last_user = last_asst = None
    last_kind = last_ts = None
    if wire:
        for d in jsonl(wire):
            t = d.get("type", "?")
            types[t] = types.get(t, 0) + 1
            if d.get("time"):
                last_ts = d["time"]
            # best-effort text discovery: the wire format is not documented
            txt = None
            for k in ("text", "content", "message", "prompt"):
                v = d.get(k)
                if isinstance(v, str) and v.strip():
                    txt = v
                    break
                if isinstance(v, list):
                    parts = [x.get("text", "") for x in v if isinstance(x, dict)]
                    if any(parts):
                        txt = "\n".join(parts)
                        break
            tl = t.lower()
            if "user" in tl and "message" in tl or tl in ("user", "prompt", "input"):
                if txt:
                    if first_user is None:
                        first_user = txt
                    last_user = txt
                last_kind = "user_text"
            elif ("assistant" in tl or "agent" in tl or "model" in tl) and ("message" in tl or "text" in tl or tl in ("assistant", "output")):
                if txt:
                    last_asst = txt
                last_kind = "assistant_text"
            elif "tool" in tl and ("call" in tl or "start" in tl):
                last_kind = "tool_call"
            elif "tool" in tl and ("result" in tl or "end" in tl or "output" in tl):
                last_kind = "tool_output"
            elif "turn" in tl and ("end" in tl or "complete" in tl or "done" in tl):
                last_kind = "turn_end"
    out.update({"wire_types": types, "last_ts": to_local(last_ts), "first_user": clip(first_user, 120),
                "last_user": clip(last_user, 160), "last_assistant": clip(last_asst, 200), "last_kind": last_kind})
    return out


HANDLERS = {"claude": extract_claude, "codex": extract_codex, "grok": extract_grok,
            "agy": extract_agy, "kimi": extract_kimi}

if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] == "--sudo":
        allow_sudo(True)
        argv = argv[1:]
    kind = argv[0]
    for target in argv[1:]:
        try:
            print(json.dumps(HANDLERS[kind](target), ensure_ascii=False))
        except Exception as e:
            print(json.dumps({"provider": kind, "path": target, "error": f"{type(e).__name__}: {e}"}))
