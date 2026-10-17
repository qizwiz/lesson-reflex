#!/usr/bin/env python3
"""failure -- PostToolUseFailure: was this failed tool call Claude's own mistake? Asked in the background.

Most failures are not mistakes (a test failing on purpose, a search finding nothing, the network), so the judge
(judge.py, kind=failure) answers mistake / expected / environment. A mistake lands in the session's inbox and is
announced at the next tool call: find the fix, then record the lesson. At most one judgement per program per session:
a second failure of the same command is the same lesson. An interrupted call is skipped -- that is the user
correcting Claude, which the correction capture reads.
"""
import hashlib
import json
import os
import shlex
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import store  # noqa: E402

WRAPPERS = ("cd", "export", "time", "env", "sudo", "nohup", "nice", "timeout", "exec", "command", "xargs")
FLAG_TAKES_VALUE = ("-u", "-g", "-n", "-C")  # sudo -u USER, sudo -g GROUP, nice -n N, git -C DIR
SHELL_WORDS = ("if", "then", "else", "elif", "fi", "do", "done", "while", "until", "for", "case", "esac", "!", "{",
               "}", "(", ")")


def program(tool, tool_input):
    """PURE: what failed, for once-per-session gating: the first program word of a Bash command, else the tool name.
    Wrappers (sudo, env, time, timeout, ...), their flags and values, VAR=value, shell keywords and operators are
    skipped, so `sudo -u root ls` is `ls`, not `-u`."""
    if tool != "Bash":
        return tool
    cmd = str((tool_input or {}).get("command") or "")
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        words = list(lex)
    except ValueError:
        words = cmd.split()
    skip_value = False
    after_wrapper = False
    for w in words:
        if skip_value:
            skip_value = False
            continue
        if not w or w[0] in ";&|<>" or w in SHELL_WORDS or ("=" in w and not w.startswith("-") and "/" not in w.split("=")[0]):
            continue
        if w in WRAPPERS:
            after_wrapper = True
            skip_value = w == "cd"
            continue
        if w.startswith("-"):
            skip_value = after_wrapper and w in FLAG_TAKES_VALUE
            continue
        if after_wrapper and w.isascii() and w.isdigit():  # timeout 10 CMD
            continue
        return os.path.basename(w)
    return "Bash"


def job_for(ev):
    """The judge's input for a PostToolUseFailure event, or None (interrupted, or this program already judged)."""
    if not isinstance(ev, dict) or ev.get("is_interrupt"):
        return None
    tool = str(ev.get("tool_name") or "")
    ti: dict = ev["tool_input"] if isinstance(ev.get("tool_input"), dict) else {}
    session = str(ev.get("session_id") or "")
    prog = program(tool, ti)
    # the program name is hashed in the key and masked in the row: a "program" can be a pasted credential
    key = store.session_file(session) + ":failure:" + hashlib.sha256(prog.encode()).hexdigest()[:24]
    with store.locked("failure.lock"):
        seen = {r.get("key") for r in store.read_jsonl(store.path("failures-seen.jsonl"))[-2000:]}
        if key in seen:
            return None
        store.append_jsonl(store.path("failures-seen.jsonl"), {"key": key})
    action = str(ti.get("command") or "") if tool == "Bash" else json.dumps(ti)
    # whole texts: the judge masks, THEN cuts (cutting here could split a credential below the length mask() sees)
    return {"kind": "failure", "session": session, "tool": tool, "program": prog, "action": action,
            "description": str(ti.get("description") or ""), "error": str(ev.get("error") or ""),
            "cwd": str(ev.get("cwd") or os.getcwd())}


def main():
    if os.environ.get("LESSON_REFLEX_JUDGING") or os.environ.get("LESSON_REFLEX_FAILURES") == "0":
        return 0
    try:
        job = job_for(json.load(sys.stdin))
        if job is None:
            return 0
        store.spawn_judge(job)
    except Exception:  # noqa: BLE001 -- never break the turn
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
