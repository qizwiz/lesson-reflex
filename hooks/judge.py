#!/usr/bin/env python3
"""judge -- was the user correcting Claude? One verdict per prompt, written to corrections.jsonl.

Runs detached (capture.py starts it). Input on stdin: {"session", "prompt", "previous"}.
The default judge is a headless `claude -p`, sealed: --safe-mode (no hooks, plugins, skills, CLAUDE.md or memory;
admin-managed policy settings still apply), no tools, no MCP servers, --no-session-persistence (no transcript is
saved), run from a fresh empty directory. The texts go to it on stdin, never on the command line, where any local
user could read them with `ps`.
Replace it with any command: set LESSON_REFLEX_JUDGE to a program that reads the same JSON on stdin and prints one
word: correction, request or other.
At most MAX_JUDGES run at once; a prompt that finds every slot busy is recorded as verdict "busy", not judged.
"""
import fcntl
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402

VERDICTS = ("correction", "request", "other")
HINDSIGHT_VERDICTS = ("error", "fine", "unclear")
FAILURE_VERDICTS = ("mistake", "expected", "environment")
MODEL = os.environ.get("LESSON_REFLEX_MODEL", "sonnet")
MAX_JUDGES = 3
TIMEOUT = 60
PROMPT = """You label one message. The USER just sent NEW_MESSAGE in reply to the assistant's PREVIOUS_REPLY.

correction = the user says something the assistant did, claimed or assumed was wrong, pushes back, or redirects it
             because it went the wrong way
request    = a new request, a question, or a go-ahead that does not say the previous work was wrong
other      = acknowledgement, chat, anything else

Reply with exactly one word and nothing else: correction, request or other.

<PREVIOUS_REPLY>
{previous}
</PREVIOUS_REPLY>

<NEW_MESSAGE>
{prompt}
</NEW_MESSAGE>"""


HINDSIGHT_PROMPT = """You label one past reply. REPLY is what an assistant told the user. FOLLOW_UP is what happened next:
the user's next message, then the assistant's next turn (its tool calls, their results, and its reply).

error   = FOLLOW_UP shows REPLY was wrong, unsupported or overstated, or claimed work that was not done or did not work
fine    = FOLLOW_UP shows no problem with REPLY: the user went on, asked for more, or confirmed it
unclear = FOLLOW_UP does not show either way

A user simply asking for more, or changing topic, is not an error. Reply with exactly one word: error, fine or unclear.

<REPLY>
{reply}
</REPLY>

<FOLLOW_UP>
{follow}
</FOLLOW_UP>"""


FAILURE_PROMPT = """You label one failed tool call. An assistant ran ACTION ({tool}); it failed with ERROR.

mistake     = the assistant's own wrong usage, flag, path, interpreter, pattern or broken script -- a lesson would
              stop it repeating
expected    = a test, check, gate or search doing its job (a deliberately failing test, a search that found nothing)
environment = something outside the assistant's control: a missing resource, the network, a timeout

Reply with exactly one word: mistake, expected or environment.

<DESCRIPTION>
{description}
</DESCRIPTION>

<ACTION>
{action}
</ACTION>

<ERROR>
{error}
</ERROR>"""


def parse(out, verdicts=VERDICTS):
    """PURE: the verdict, read from the FIRST word of the output only (markdown and punctuation trimmed), or ''.

    Only the leading word counts, so "Not a correction" is not read as one; "correction—" and "**correction**" are.
    """
    text = (out or "").strip().lstrip("*_`'\"([").lower()
    word = ""
    for ch in text:
        if not ("a" <= ch <= "z"):
            break
        word += ch
    return word if word in verdicts else ""


def judge_argv():
    """The sealed default judge. The prompt itself goes on stdin."""
    return ["claude", "-p", "--model", MODEL, "--no-session-persistence", "--safe-mode", "--tools", "",
            "--strict-mcp-config"]


def custom_argv(spec, base):
    """LESSON_REFLEX_JUDGE split into argv; a relative program path is resolved against BASE (the judge runs from an
    empty temp directory, where ./judge would not exist)."""
    argv = shlex.split(spec)
    if argv and os.sep in argv[0] and not os.path.isabs(argv[0]):
        argv[0] = os.path.abspath(os.path.join(base, argv[0]))
    return argv


def run_judge(job, run=subprocess.run, base=None):
    """(verdict, error): the verdict word, or ('', why) when the judge failed or answered off-format.

    The error never quotes the judge's output: a judge that echoes the prompt would otherwise store it."""
    env = dict(os.environ, LESSON_REFLEX_JUDGING="1", CLAUDE_CODE_DISABLE_AUTO_MEMORY="1")
    custom = os.environ.get("LESSON_REFLEX_JUDGE")
    kind = job.get("kind", "correction")
    try:
        argv = custom_argv(custom, base or os.getcwd()) if custom else judge_argv()
        if custom:
            stdin = json.dumps(job)
        elif kind == "hindsight":
            stdin = HINDSIGHT_PROMPT.format(reply=job["reply"], follow=job["follow"])
        elif kind == "failure":
            stdin = FAILURE_PROMPT.format(tool=job.get("tool", ""),
                                          description=store.mask(str(job.get("description", "")))[:300],
                                          action=store.mask(str(job["action"]))[-4000:],
                                          error=store.mask(str(job["error"]))[:2000])
        else:
            stdin = PROMPT.format(previous=job["previous"], prompt=job["prompt"])
        with tempfile.TemporaryDirectory() as cwd:
            r = run(argv, input=stdin, capture_output=True, text=True, errors="replace", timeout=TIMEOUT,
                    env=env, cwd=cwd)
    except ValueError:
        return "", "LESSON_REFLEX_JUDGE could not be parsed (unbalanced quotes?)"
    except FileNotFoundError:
        return "", "judge program not found" + ("" if custom else " (is the claude CLI on PATH?)")
    except subprocess.TimeoutExpired:
        return "", f"judge timed out after {TIMEOUT}s"
    except (OSError, subprocess.SubprocessError) as e:
        return "", f"judge failed: {type(e).__name__}"
    if r.returncode != 0:
        return "", f"judge exited {r.returncode}"
    verdicts = {"hindsight": HINDSIGHT_VERDICTS, "failure": FAILURE_VERDICTS}.get(kind, VERDICTS)
    verdict = parse(r.stdout, verdicts)
    return (verdict, "") if verdict else ("", f"judge answered off-format ({len(r.stdout or '')} characters)")


def record(job, verdict, error=""):
    """Ledger the verdict. Only a correction keeps text; it also goes to the session's inbox. A hindsight "error"
    is recorded as a correction of kind hindsight, keeping the end of the reply that was wrong, so the inbox,
    `lesson pending` and `lesson dismiss` work the same for both."""
    kind = job.get("kind", "correction")
    if (kind, verdict) in (("hindsight", "error"), ("failure", "mistake")):
        verdict = "correction"
    row = {"id": store.new_id(), "session": job.get("session", ""), "kind": kind, "verdict": verdict or "error",
           "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if error:
        row["error"] = error
    if verdict == "correction":
        if kind == "hindsight":
            text = store.mask(str(job.get("reply", "")))[-600:]
        elif kind == "failure":
            text = store.mask(str(job.get("action", "")))[:600]
        else:
            text = store.mask(str(job.get("prompt", "")))[:600]
        row["prompt"] = text
        if kind == "failure":
            row["program"] = str(job.get("program", ""))[:80]
    store.append_jsonl(store.path("corrections.jsonl"), row)
    if verdict == "correction":
        store.add_to_inbox(row["session"], row["id"])
    return row


def take_slot():
    """An open fd holding one of MAX_JUDGES slot locks, or None when all are busy."""
    store.ensure_home()
    for i in range(MAX_JUDGES):
        fd = os.open(store.path(f"judge-slot-{i}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            os.close(fd)
    return None


def main():
    try:
        job = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(job, dict):
        return 0
    needs = {"hindsight": ("reply", "follow"), "failure": ("action", "error")}.get(job.get("kind"), ("prompt", "previous"))
    if any(k not in job for k in needs):
        return 0
    try:
        slot = take_slot()
    except OSError as e:
        record(job, "", f"could not take a judge slot: {type(e).__name__}")
        return 0
    if slot is None:
        record(job, "busy")
        return 0
    try:
        try:
            verdict, error = run_judge(job, base=job.get("cwd") or None)
        except Exception as e:  # noqa: BLE001 -- any failure still leaves a row, so `lesson pending` can report it
            verdict, error = "", f"judge crashed: {type(e).__name__}"
        record(job, verdict, error)
    finally:
        os.close(slot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
