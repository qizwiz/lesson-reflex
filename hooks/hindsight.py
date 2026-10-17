#!/usr/bin/env python3
"""hindsight -- Stop: was my PREVIOUS reply wrong, judging by what happened next? Asked in the background.

Asking before the fact ("is this reply about to be wrong?") does not work: measured against outcome labels it ranked
worse than chance. Asking afterwards does: what follows a wrong reply -- the user's correction, a failed check, the
assistant's own "I had that wrong" -- usually shows it. So at the end of each turn this hands the previous reply and
what followed to the judge (judge.py, kind=hindsight). An "error" lands in this session's inbox and is announced at
the next tool call, like a correction. It never blocks the stop.
"""
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import capture  # noqa: E402
import store  # noqa: E402


def job_for(ev):
    """The judge's input for this Stop event, or None. Each reply is judged once: a background-task notification
    starts no new turn, so the next Stop would otherwise hand the same reply to the judge again."""
    if not isinstance(ev, dict) or ev.get("stop_hook_active"):
        return None
    reply, prompt, follow = capture.previous_and_follow(ev.get("transcript_path"))
    if not reply.strip() or not prompt.strip():
        return None
    session = str(ev.get("session_id") or "")
    key = store.session_file(session) + ":" + hashlib.sha256(reply.encode()).hexdigest()[:24]
    with store.locked("hindsight.lock"):
        if key in {r.get("key") for r in store.read_jsonl(store.path("hindsight-seen.jsonl"))[-2000:]}:
            return None
        store.append_jsonl(store.path("hindsight-seen.jsonl"), {"key": key})
    return {"kind": "hindsight", "session": session, "reply": reply, "prompt": prompt, "follow": follow,
            "cwd": str(ev.get("cwd") or os.getcwd())}


def main():
    if os.environ.get("LESSON_REFLEX_JUDGING") or os.environ.get("LESSON_REFLEX_HINDSIGHT") == "0":
        return 0
    try:
        job = job_for(json.load(sys.stdin))
        if job is None:
            return 0
        store.spawn_judge(job)
    except Exception:  # noqa: BLE001 -- never block or break a stop
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
