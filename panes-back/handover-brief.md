# Handover brief — one fresh-context reader per cut transcript

Fill every `{…}` from the scan JSON before sending. Use an agent type that has a shell (it reads files and runs git). One transcript per reader.

---

You are reading ONE {cli} transcript that was cut off when VS Code reset at {cut} (local time; transcript timestamps are usually UTC, so add the local offset). Produce a fact-only handover so the owner knows exactly where this session stood. READ-ONLY: no edits, no git writes, no commits, never kill a process.

Transcript: `{path}` (owner: unix user `{user}`).
If you are not `{user}`, read it only through a pipe as root or the owner, never by copying it: `sudo -n cat {path} | python3 <your script reading stdin>`.
Claude Code sub-agent transcripts sit under `{path minus .jsonl}/subagents/`; Codex sub-agents are separate rollout files in the same folder whose first line's `session_meta.payload.source.subagent.thread_spawn.parent_thread_id` equals this thread id.

Context: first ask `{first_ask}`; last ask `{last_ask}`; last reply (clipped) `{last_said}`; status at the cut `{status}`.

Method (never load a multi-MB file whole into your context):
1. First pass: `python3 {skill_dir}/transcripts.py {cli} <target>` prints a JSON summary (add `--sudo` before `{cli}` when the file belongs to another user). The target is `{path}` for `claude` and `codex`, and the folder holding `{path}` for `grok` and `kimi`. For Antigravity (`agy`) the target is the conversation's log, `~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript.jsonl` (`agy_log` in the JSON).
2. Then parse with a short python script: every user text message with local time; the last 12 assistant text messages in full; every tool call with its name and the first 150 chars of its input (a shell command, a file path written, a sub-agent description; for Codex, `function_call` / `custom_tool_call` items and their outputs' exit codes); any tool result carrying a git commit sha. Same for each sub-agent transcript, shorter.
3. Cross-check against disk, because the transcript is a claim and the disk is the state: if the first ask names a plan or task file, read its progress notes; find the repository or worktree the transcript names and read `git -C <it> status --short` and `git -C <it> log --oneline -8` (as the owner when it is another user's tree). Note anything modified on disk but uncommitted.

Rules: use full paths or `git -C` rather than changing directory. Session ids, hashes and long encrypted blobs in the transcript are not secrets. Never read a file named `.env`, `credentials`, `auth.json` or anything holding a token.

Report back in at most 300 words, this exact shape, every material factual claim tagged [Certain] / [Likely] / [Guessing]:
**Goal:** one sentence.
**Done (evidence):** bullets with commit shas, files, tests or checks as measured on disk.
**State at the cut:** what was mid-flight and what it was waiting on (which sub-agent, which command).
**Uncommitted / at risk:** files modified on disk not committed; anything half-applied.
**Next action:** the single next step the resumed session should take, in one line.
