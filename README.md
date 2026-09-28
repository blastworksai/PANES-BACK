# PANES-BACK

Bring your coding-agent sessions back after VS Code resets.

A VS Code server restart (an update, a failed Remote-SSH reconnect, a crash) kills every integrated terminal at once, and every agent CLI running in one of them.
The transcripts all survive on disk.
PANES-BACK finds them, says what each session was doing when it died, and writes a resume sheet with one paste-ready line per session.

Supported CLIs: **Claude Code**, **OpenAI Codex**, **Antigravity** (`agy`), **Kimi**, **Grok**.
Linux, Python 3.9+, standard library only.

## What you get

```text
$ python3 panes-back/panes_back.py scan
cut 2026-01-15 18:03  window -45m/+5m  transcripts touched since 10:15: 6
  cut               18:02  alice      claude                   /plan the billing export
  cut               18:03  alice      codex                    fix the flaky upload test
  headless-finished 18:05  alice      codex                    You are a code reviewer (read-only)…
  headless-cut      18:03  alice      codex                    background memory pass
  alive             18:40  alice      codex                    (driven from another machine)
sheet: /home/alice/.local/state/panes-back/resume-sheet-2026-01-15-1803.md
{"cut": "...", "sessions": [...]}
```

The sheet sorts every session it found into one class:

| class | meaning |
|---|---|
| `cut` | was in the window around the reset and has no live process: resume it |
| `headless-finished` | a sub-agent or reviewer that finished *after* its parent died: its result sits only in its file |
| `headless-cut` | a sub-agent cut mid-run: never resume it on its own, resume its parent and re-run it |
| `alive` / `written-after-cut` | still running or written since: left alone |
| `older` | idle well before the reset, still resumable by id |

## Install

As a Claude Code skill (the agent then runs it when you say "VS Code reset" or "bring my sessions back"):

```bash
mkdir -p ~/.claude/skills && cp -r panes-back ~/.claude/skills/
```

Or run the script directly: `python3 panes-back/panes_back.py scan`.

## Configure (optional)

With no config it scans your own home only, uses your machine's time zone, and writes plain resume lines (`cd <dir> && exec claude --resume <id>`).
Copy `config.example.json` to `~/.config/panes-back/config.json` (or pass `--config`, or set `PANES_BACK_CONFIG`) to change that:

| key | what it does |
|---|---|
| `timezone` | IANA zone for every time printed (default: the machine's) |
| `users` | `"self"` (default), `"all"` (every home under `/home`), or a list of unix users. Other users' files are read through `sudo -n`, so this needs passwordless sudo for `cat`, `find` and `test`, plus `sudo -u <user> claude agents --json --all` to see which of their Claude sessions are running (without it the sheet says so and falls back to command lines) |
| `sheet_dir` | where sheets go (default `~/.local/state/panes-back`) |
| `names` | display names per unix user |
| `panes` | your terminals: `name`, `launch` (the exact command the terminal runs), optional `purpose` and `user`. The resume line is the pane's own launch line with the resume verb spliced in, so model and flags survive |
| `vscode_terminals` | read panes from a [Terminals Manager](https://marketplace.visualstudio.com/items?itemName=fabiospampinato.vscode-terminals) `terminals.json`: `path`, and optionally `launch_env` / `purpose_env` if the launch command lives in an env var rather than `commands` |
| `purpose_rules` | pick among several panes for one user+CLI by the session's first ask (`first_ask_prefix` → `purpose`) |
| `default_purpose` | the pane purpose to use when no rule matches |
| `resume_hint` | the one-line instruction at the top of the sheet |
| `agy_tmp_is_headless` | `true` (default): an Antigravity conversation rooted in `/tmp` or `/var/tmp` is taken as a headless run |

When panes are configured, sessions on a user+CLI with no pane are treated as background work and left out, unless they are alive or were written after the reset.

A pane's launch line is understood in one shape only, and the resume line is rebuilt from its parts:

```text
[sudo [-n] [-H] -u USER]  [bash|sh -c|-lc '<command>']
<command> = [exec] [env [-C DIR] [VAR=value ...]] <cli> [args ...]
```

The rebuilt line adds the resume verb, sets the folder with `env -C` (inside the other user's `sudo`, so it needs no access of yours), points Codex's own `-C` at it too, and quotes every argument. A launch line with anything else in it (`;`, `&&`, pipes, redirections, `$`, other wrappers) is not guessed at: the sheet says so and gives that pane's sessions a plain line instead.

## How it decides

- **Alive** means a running process is tied to that exact session: its id right after a resume flag on the command line, the transcript held open, or (Claude Code) `claude agents`. A running process it can't tie to any session is reported next to each session it might be ("liveness unresolved"), never silently ignored.
- **Headless** runs are known from the transcript: Claude Code's `entrypoint` (`sdk-cli` for `claude -p`), Codex's `source` (`exec`, or a sub-agent thread). Antigravity records neither, so an Antigravity conversation rooted in `/tmp` or `/var/tmp` is taken as headless: a known limit, switched off with `agy_tmp_is_headless: false`. Antigravity also keeps a working folder only for the latest conversation in each folder, so an older conversation's folder can read as unknown.
- A headless run counts as **finished after the cut** only if it was already running at the cut and ended idle afterwards; one that ended before the cut, or started after it, is not listed.

## Safety

- Read-only apart from the sheet it writes (mode 0600, in a 0700 folder when it creates one). It never resumes, kills or deletes a session, and writes no `__pycache__`.
- Other users' files are read through `sudo -n cat` / `find` and never written; nothing needs sudo in the default single-user mode.
- Transcripts can contain anything you or your agents typed. The sheet quotes first asks and last replies, so treat it like the transcripts themselves.

## Files

- `panes-back/panes_back.py`: the scanner and sheet writer.
- `panes-back/transcripts.py`: per-CLI transcript readers (also a CLI: `transcripts.py [--sudo] claude <session.jsonl>`, `codex <rollout.jsonl>`, `grok <session-dir>`, `kimi <session-dir>`, `agy <brain/<id>/.system_generated/logs/transcript.jsonl>`).
- `panes-back/SKILL.md`: the Claude Code skill.
- `panes-back/handover-brief.md`: the brief for an optional fresh-context reader per cut session.

## License

MIT.
