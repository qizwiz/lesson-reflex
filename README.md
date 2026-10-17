# lesson-reflex

A Claude Code plugin that turns your corrections into lessons that fire **at the action**, not in a rules file.

You tell Claude it got something wrong. A background judge decides whether that was a correction. If it was, the
next time Claude calls a tool it is told to record a lesson. A lesson has three parts: a tool, a piece of text from
the action where the mistake started (a command, a function name, a flag), and what to do instead. From then on,
whenever Claude is about to run a Bash command or write Edit/Write text containing that text, the lesson is shown
to it, once per session (a subagent counts separately).

It also catches mistakes nobody pointed out. At the end of each turn, the judge looks back at Claude's previous reply
and what followed it: your next message, then the tools Claude ran and what it said. If that shows the reply was
wrong, Claude is told at its next tool call, the same way as a correction. Hindsight works where foresight does not:
asked *before* the fact whether a reply was about to be wrong, the same kind of judge did worse than chance.

```
you: "no, that moved my cursor"
        │  UserPromptSubmit: capture.py hands your message + Claude's last reply to the judge, in the background
        ▼
judge (claude -p, sealed) ─► "correction"  ─► corrections.jsonl + this session's inbox
        │
        ▼  PreToolUse: Claude's next tool call after the verdict lands
"[lesson-reflex] The user CORRECTED you ... python3 .../bin/lesson add --tool Edit --needle 'pop-to-buffer' ..."
        │
        ▼  any later session, any Edit containing "pop-to-buffer"
"[lesson-reflex] LESSON ('pop-to-buffer'): use display-buffer for calls made on the user's behalf ..."
```

## Why at the action

Rules in CLAUDE.md load at the start of every session and compete with everything else there. A lesson that
arrives exactly when Claude is about to repeat the mistake is harder to miss, and costs nothing the rest of the
time.

## Install

From GitHub, as a one-plugin marketplace:

```
/plugin marketplace add qizwiz/lesson-reflex
/plugin install lesson-reflex@lesson-reflex
```

Or try it from a local checkout without installing: `claude --plugin-dir /path/to/lesson-reflex`.

Requires Python 3.9+ on macOS or Linux (it uses `fcntl`), and the `claude` CLI on PATH for the default judge. No
other dependencies. The correction announcement gives Claude the CLI's full path. To use the CLI yourself, link
`bin/lesson` onto your PATH.

## The CLI

```
lesson add --tool Bash|Edit|MultiEdit|Write --needle TEXT --lesson TEXT [--source correction:ID]
lesson list
lesson pending          corrections the judge flagged that no lesson cites yet, plus recent judge failures
lesson dismiss ID       the judge misread it
lesson rm TOOL NEEDLE
```

`lesson add` refuses:
- a needle under 4 characters
- a lesson under 20 characters
- a duplicate
- any field containing a token that starts with a known credential prefix (GitHub, Anthropic/OpenAI, Stripe, Slack,
  GitLab, AWS key ids, Google keys) and continues like a generated key (long enough, with digits)

That check catches common key shapes, not every secret. A lesson is read back into later sessions, so describe a
secret's shape, never its value.

## The judge

The judge answers two questions, each in the background so nothing waits on it.

**Correction** (UserPromptSubmit): each prompt you type, once Claude has said or done something since your previous prompt, starts one judge call
**in the background**, so your prompt never waits on it. At most 3 run at once; a prompt that arrives while all 3 are busy is recorded as `busy`
and not judged.

The default judge is `claude -p --model sonnet`, run sealed:
- `--safe-mode`: no hooks, plugins, skills, CLAUDE.md or memory (admin-managed policy settings still apply)
- `--tools ""` and `--strict-mcp-config`: no tools and no MCP servers
- `--no-session-persistence`: no transcript of the judge call is saved
- a fresh empty working directory

It receives, on stdin rather than the command line, your message (up to 2000 characters) and the end of what Claude
said and did since your previous message, tool calls included (up to 1500 characters), so a correction typed after
interrupting a tool call is judged too. It also receives what Claude Code always attaches to a session, such as its system prompt and
your account email. All of it goes to the same provider your session already uses.

The verdict takes several seconds (about 3 to 6 in the author's runs). It is announced at Claude's next tool call on the main thread after it lands, which
may be later in the same turn or in the next one; subagents are never told. A correction that is never announced
still shows up in `lesson pending`.

**Hindsight** (Stop): at the end of each turn, the reply that ended the *previous* turn (up to 1500 characters) and what
followed it (your message, then this turn's tool calls and reply, up to 1500 characters) go to the judge, which
answers `error`, `fine` or `unclear`. An `error` is announced at Claude's next tool call and listed by `lesson pending`
like a correction. The hindsight hook never blocks the end of a turn.

Each judge call is a small model request. To change the judge:

- `LESSON_REFLEX_MODEL=haiku`: cheaper, noisier.
- `LESSON_REFLEX_JUDGE="your-command"`: any program that reads the job as JSON on stdin and prints one word. A
  correction job is `{"session","prompt","previous","cwd"}` and wants `correction`, `request` or `other`; a hindsight
  job is `{"kind":"hindsight","session","reply","follow","cwd"}` and wants `error`, `fine` or `unclear`. Only the leading word of its output is read. A relative path
  such as `./judge` is resolved against the session's working directory when the prompt was sent; quote a path
  containing spaces.

If the judge fails (CLI missing, auth, timeout, an answer that isn't one of its three words), the job is
recorded as `error` with the reason (never the judge's output), and `lesson pending` reports how many of the recent
prompts went unjudged.

### How well it judges

The author hand-labelled 29 of their own prompts, 5 of them corrections, before any model saw them:
- sonnet caught 3 of the 5 corrections, with no false alarms
- haiku caught 4, and flagged 2 ordinary requests as corrections

For hindsight, the author labelled 120 of their own past turns with a stronger judge, Jev, and read the 5 most
confident "error" labels by hand; all 5 were real. On 20 turns Jev labelled `error` and 20 it labelled `fine`, the
default sonnet judge said `error` on 8 of the 20 errors (4 of the 5 clearest) and on 1 of the 20 fine turns. So
expect its announcements to be mostly right, and expect it to miss more than half of the subtler errors.

These are small samples from one person, and the transcripts are private, so the data is not in this repository. To
score it on your own sessions: every verdict is in `corrections.jsonl`, and every `lesson dismiss` records a
misread.

## What it stores

Everything lives in `~/.claude/lesson-reflex/` (override with `LESSON_REFLEX_HOME`). The directory is created
private (0700) and its files 0600. Delete the directory to start over.

| file | what it holds |
|---|---|
| `lessons.jsonl` | your lessons |
| `corrections.jsonl` | one row per judgement (id, session id, kind, verdict, time). Text is kept **only** for a correction (your prompt's first 600 characters) or a hindsight error (the last 600 characters of the reply that was wrong). |
| `dismissed.txt` | ids of corrections you dismissed |
| `inbox/` | corrections waiting to be announced; each is removed once announced or closed. A session that ends before its next tool call leaves its file behind; `lesson pending` still lists the correction. |
| `fired.json` | which lessons have already fired, for the 256 most recent entries (a session's main thread and each subagent are separate entries) |
| `*.lock` | lock files that keep concurrent sessions and judges from overwriting each other |

`corrections.jsonl` grows by one row per judgement (up to two per turn), and nothing trims it.

## Prior art

- [claude-reflect](https://github.com/BayramAnnakov/claude-reflect) captures corrections with a hook and turns
  them into CLAUDE.md rules. This plugin borrows that capture step.
- [claude-code-reflection-hook](https://github.com/dankefox/claude-code-reflection-hook) checks each edit against
  a library of past mistakes, after the edit.

lesson-reflex differs in where the lesson lands: a trigger matched before the action, on Bash commands as well as
edits, fired once per session.

## Tests

```
python3 -m unittest discover -s tests
```

Standard library only. The judge is stubbed, so no model is called.

## License

MIT
