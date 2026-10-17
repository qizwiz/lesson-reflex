# lesson-reflex

A Claude Code plugin that turns your corrections into lessons that fire **at the action**, not in a rules file.

You tell Claude it got something wrong. A background judge decides whether that was a correction. If it was, the
next time Claude calls a tool it is told to record a lesson. A lesson has three parts: a tool, a piece of text from
the action where the mistake started (a command, a function name, a flag), and what to do instead. From then on,
whenever Claude is about to run a Bash command or write Edit/Write text containing that text, the lesson is shown
to it, once per session (a subagent counts separately).

It also catches mistakes nobody pointed out, in three ways:

- **Tool failures.** When a tool call fails, the judge decides whether it was Claude's own mistake (a wrong flag,
  path, interpreter or pattern) rather than a check doing its job or the environment. A mistake is announced at the
  next tool call. At most one judgement per program per session.
- **Structural lessons.** Before an Edit or Write to a Python file lands, the file as it would be after the edit is
  parsed and checked for known mistake *shapes* that live in data flow, where no text match can see them (see
  `hooks/ast_lessons.py`). It warns; it never blocks.
- **Hindsight.** At the end of each turn, the judge looks back at Claude's previous reply
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

The judge answers three questions, each in the background so nothing waits on it. Everything the default judge is
sent is masked for credential-shaped tokens first and cut after; a custom judge (`LESSON_REFLEX_JUDGE`, below)
receives the job exactly as built, unmasked and uncut.

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

**Tool failure** (PostToolUseFailure, for Bash, Edit, MultiEdit and Write only -- the tools a lesson can attach
to): the failed action, its description and the error (masked, then cut to 4000, 300 and 2000 characters) go to the
judge, which answers `mistake`, `expected` or `environment`. A call interrupted by the user is skipped; that is a
correction, which the correction capture reads. The "program" a Bash failure is counted under is its first command
word after wrappers such as `sudo`, `env`, `time`, `timeout` and `xargs`, with their flags and values (`timeout
-s KILL 5 ls` is `ls`).
Measured on 40 of the author's real failed Bash calls, hand-labelled before any model saw them (6 were mistakes):
the default sonnet judge caught all 6, and also called 11 of the 34 benign failures mistakes. (That sample kept only
each call's description and a cut error, not the command itself.) Expect most real mistakes to be caught and some
noise; each false alarm costs one `lesson dismiss`. Set `LESSON_REFLEX_FAILURES=0` to turn the failure capture off.

**Hindsight** (Stop): at the end of a turn you started, the judge gets what Claude said *and did* in the previous
turn -- its text and its tool calls, including their arguments (shell commands, file contents written), up to the
last 2000 characters -- and what followed: your message (up to 2000 characters), then this turn's tool calls, their
results and its reply (the last 2500 characters). Everything is masked for credential-shaped tokens first, then cut.
The judge answers `error`, `fine` or `unclear`. An `error` is announced at Claude's next tool call and listed by
`lesson pending` like a correction; if you already corrected that reply yourself and the correction judge has
answered by then (it usually has: it starts when you send the message, the hindsight judge when the turn ends), it is
recorded as a `duplicate` and not announced again. Each reply is judged once. A turn is skipped when it was not
started by a message you typed (a harness message, a slash command, an image alone) or when the newest 400 KB of the transcript does not reach back to the previous
turn. The hindsight hook never blocks the end of a turn.

Each judge call is a small model request. To change the judge:

- `LESSON_REFLEX_MODEL=haiku`: cheaper, noisier.
- `LESSON_REFLEX_FAILURES=0`: no tool-failure judgements.
- `LESSON_REFLEX_HINDSIGHT=0`: no hindsight judgements.
- `LESSON_REFLEX_JUDGE="your-command"`: any program that reads the job as JSON on stdin and prints one word. A
  correction job is `{"session","prompt","previous","cwd"}` and wants `correction`, `request` or `other`; a hindsight
  job is `{"kind":"hindsight","session","reply","prompt","follow","cwd"}` and wants `error`, `fine` or `unclear`; a failure
  job is `{"kind":"failure","session","tool","program","action","description","error","cwd"}` and wants `mistake`,
  `expected` or `environment`. Only the leading word of its output is read. A relative path
  such as `./judge` is resolved against the session's working directory when the prompt was sent; quote a path
  containing spaces.

If the judge fails (CLI missing, auth, timeout, an answer that isn't one of its three words), the job is
recorded as `judge-error` with the reason (never the judge's output), and `lesson pending` reports how many of the
recent judgements failed.

### How well it judges

The author hand-labelled 29 of their own prompts, 5 of them corrections, before any model saw them:
- sonnet caught 3 of the 5 corrections, with no false alarms
- haiku caught 4, and flagged 2 ordinary requests as corrections

For hindsight, the author had a different, stronger judge label 120 of their own past turns: it labelled 88 (26
error, 54 fine, 8 unclear) and declined 32 for privacy. The 5 most confident "error" labels, read by hand, were all
real. On 20 turns it labelled `error` and 20 it labelled `fine`, the default sonnet judge said `error` on 8 of the 20
errors (4 of the 5 clearest) and on 1 of the 20 fine turns. That measurement fed sonnet the reply's final text (no
tool calls) and a follow-up that included tool results, close to but not the same as what the plugin sends. Expect
its announcements to be mostly right, and expect it to miss more than half of the subtler errors.

These are small samples from one person, and the transcripts are private, so the data is not in this repository. To
score it on your own sessions: every verdict is in `corrections.jsonl`, and every `lesson dismiss` records a
misread.

## What it stores

Everything lives in `~/.claude/lesson-reflex/` (override with `LESSON_REFLEX_HOME`). The directory is created
private (0700) and its files 0600. Delete the directory to start over.

| file | what it holds |
|---|---|
| `lessons.jsonl` | your lessons |
| `corrections.jsonl` | one row per judgement (id, session id, kind, verdict, time). Text is kept **only** for a correction (your prompt's first 600 characters), a hindsight error (the last 600 characters of the reply that was wrong) or a tool-failure mistake (the action's first 600 characters, plus the program name's first 80), with
credential-shaped tokens masked before it is cut (in tool arguments, each value is masked before it is written as JSON). |
| `failures-seen.jsonl` | one row per program per session already sent to the failure judge: a hash of the session id and a hash of the program name. A program is marked when it is sent, so if every judge slot was busy it is not judged again that session. It grows by a row per new program per session and is not trimmed (only the last 2000 rows are read). |
| `hindsight-seen.jsonl` | one row per reply already sent to the hindsight judge (hashes only), so a reply is judged once |
| `ast-fired.json` | which structural findings each recent session has already been shown |
| `dismissed.txt` | ids of corrections you dismissed |
| `inbox/` | corrections waiting to be announced; each is removed once announced or closed. A session that ends before its next tool call leaves its file behind; `lesson pending` still lists the correction. |
| `fired.json` | which lessons have already fired, for the 256 most recent entries (a session's main thread and each subagent are separate entries) |
| `*.lock` | lock files that keep concurrent sessions and judges from overwriting each other |

`corrections.jsonl` grows by one row per judgement -- a correction and a hindsight judgement per turn at most, plus
one per newly failing program -- and nothing trims it.

## Structural lessons

`hooks/ast_lessons.py` ships one check, `slice-into-redact`: text cut (`x[:N]`) before it reaches a masking function
(`redact`, `mask`, `scrub`, `sanitize`, `anonymize`, `mask_secrets`), so a credential straddling the cut becomes a
fragment too short to recognise and is stored or sent. It follows the value through local names, lists it is
appended to, tuple unpacking, loop and comprehension variables, and functions that return cut text, within one
file. Each function, lambda and comprehension is its own scope: it sees what its enclosing scope had bound where it
is defined, and its own parameters shadow that. A use sees only what was bound before it, except inside a loop, where
a cut made later in the loop reaches the next pass. That bug recurred five times while this plugin was being built,
which is why it is the first.

It is a heuristic over one file, and it is wrong both ways:
- **False positives:** any function named like a masker counts, so a `mask()` that formats a card number for
  display, fed its last four digits, is flagged (the one finding across 548 of the author's Python files).
  `df.mask(df[:5] > 0)` is flagged too. It errs toward flagging: clean text assigned to a name inside an `if`, a loop
  or a `try` does not clear it, because another path may still carry the cut text.
- **False negatives:** cuts made in another module or by a library (`textwrap.shorten`), cuts that reach the masker
  through an object attribute or a dict entry, and walrus assignments are not followed. To add a check, write a function from source text to
`[(line, message)]` and add it to `CHECKS`. Run it directly with `python3 hooks/ast_lessons.py check FILE...`.

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
