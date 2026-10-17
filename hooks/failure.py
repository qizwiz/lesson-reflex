#!/usr/bin/env python3
"""failure -- PostToolUseFailure: was this failed tool call Claude's own mistake? Asked in the background.

Most failures are not mistakes (a test failing on purpose, a search finding nothing, the network), so the judge
(judge.py, kind=failure) answers mistake / expected / environment. A mistake lands in the session's inbox and is
announced at the next tool call: find the fix, then record the lesson. At most one judgement per program per session:
a second failure of the same command is the same lesson. An interrupted call is skipped -- that is the user
correcting Claude, which the correction capture reads.
"""
import json
import os
import shlex
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import store  # noqa: E402


def program(tool, tool_input):
    """PURE: what failed, for once-per-session gating: the first program word of a Bash command, else the tool name."""
    if tool != "Bash":
        return tool
    cmd = str((tool_input or {}).get("command") or "")
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        words = list(lex)
    except ValueError:
        words = cmd.split()
    skip = False
    for w in words:
        if skip:
            skip = False
            continue
        if w in ("cd", "export", "time", "env", "sudo", "nohup"):
            skip = w == "cd"
            continue
        if not w or w[0] in ";&|()<>" or "=" in w.split("/")[0]:
            continue
        return os.path.basename(w)
    return "Bash"


def job_for(ev):
    """The judge's input for a PostToolUseFailure event, or None (interrupted, or this program already judged)."""
    if not isinstance(ev, dict) or ev.get("is_interrupt"):
        return None
    tool = str(ev.get("tool_name") or "")
    ti = ev.get("tool_input") if isinstance(ev.get("tool_input"), dict) else {}
    session = str(ev.get("session_id") or "")
    prog = program(tool, ti)
    key = store.session_file(session) + ":failure:" + prog
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
        proc = subprocess.Popen([sys.executable, os.path.join(HERE, "judge.py")], stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        proc.stdin.write(json.dumps(job).encode())
        proc.stdin.close()
    except Exception:  # noqa: BLE001 -- never break the turn
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
