---
name: panes-back
description: Bring your agent sessions back after VS Code resets — find every coding-agent session (Claude Code, Codex, Antigravity, Kimi, Grok) that died with its terminal, say what each one was doing, hand you one resume line per session, and read the cut transcripts for a handover on request. Use when the user says "VS Code reset", "I lost my sessions", "my terminals died", "my panes are gone", "bring my sessions back" or "what was I running".
argument-hint: (nothing; or the reset time as "YYYY-MM-DD HH:MM" if it wasn't the latest VS Code server restart)
---

# PANES-BACK

A VS Code reset (a server update, a Remote-SSH reconnect that fails, a crash) kills every integrated terminal in the same second, and every agent CLI running in one.
Nothing under VS Code survives it; every transcript does.
This skill turns "I lost everything" into one sheet: what each session was doing, and the exact line that brings it back.

One deterministic script beside this file does the finding and the writing (`<skill dir>` is the folder holding this SKILL.md, for example `~/.claude/skills/panes-back`):

```bash
python3 <skill dir>/panes_back.py scan                               # the whole thing; sheet + one JSON line last
python3 <skill dir>/panes_back.py cut                                # just the cut moment it will use
python3 <skill dir>/panes_back.py scan --cut "2026-01-15 18:03"      # an earlier reset than the newest
```

It lists the live CLIs first, so a session with a live process is never called dead.
When panes are configured (see the README), each resume line is that pane's own launch line with the resume verb spliced in, so the user, model and flags are the pane's, not improvised; otherwise it is a plain line on the session's working directory.
With several unix users configured, other users' files are read through `sudo -n` and never written.

## The walk

1. **Run `scan`.** Read the JSON on the last stdout line. Do not re-derive anything it already measured.
2. **Say the cut in one line** (time, and that everything under VS Code died together while the transcripts are intact), then the table: one row per `cut` session, its pane or user, what it was doing (the first real ask, in plain words, no ids), the state at the cut (mid-turn, idle, waiting on N sub-agents).
3. **Name what fell in the gap.** A `headless-finished` entry is a sub-agent or reviewer whose result landed after its parent died: say which parent should read it and give the file path. A `headless-cut` entry was cut mid-run: it is never resumed on its own (Codex refuses to resume a sub-agent thread by itself); resume its parent (`parent_id` in the JSON, when known) and re-run it from there.
4. **Name what is still alive** (`alive`, `written-after-cut`): a session driven from another machine, a desktop app's remote session. Leave them alone.
5. **Hand over the sheet path** and its one instruction. Do not paste every line into the chat; the sheet holds them.
6. **Never resume a session yourself.** The terminals are the user's; a resume started from inside this session would run under the wrong terminal. Never kill a process, never delete a session.

## The deep read — on request only

When the user wants to know where each cut session actually stood ("go through the transcripts", "where was each one"), spawn one fresh-context reader per `cut` session, in parallel, with the brief in `handover-brief.md` beside this file.
Fill its placeholders from the JSON (`path`, `user`, `cli`, `cut`, and the plan or task file the first ask names, if any) and `{skill_dir}` with this skill's folder.
Each returns a fact-only handover of at most 300 words; append them under a `## Handovers` heading at the end of the sheet, one per session, and relay the two or three things the user must act on.
Readers are read-only: no edits, no git writes, nothing killed.

## What it does not do

- It does not wrap terminals in tmux. A reset will happen again; preventing the loss (a `tmux new -A` in each terminal's launch line) is a separate decision, never made by this skill.
- It does not touch plans, tickets or memory. A handover that changes a plan's progress notes is the resumed session's job.
