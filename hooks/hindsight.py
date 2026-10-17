#!/usr/bin/env python3
"""hindsight -- Stop: was my PREVIOUS reply wrong, judging by what happened next? Asked in the background.

Asking before the fact ("is this reply about to be wrong?") does not work: measured against outcome labels it ranked
worse than chance. Asking afterwards does: what follows a wrong reply -- the user's correction, a failed check, the
assistant's own "I had that wrong" -- usually shows it. So at the end of each turn this hands the previous reply and
what followed to the judge (judge.py, kind=hindsight). An "error" lands in this session's inbox and is announced at
the next tool call, like a correction. It never blocks the stop.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import capture  # noqa: E402


def job_for(ev):
    """The judge's input for this Stop event, or None."""
    if not isinstance(ev, dict) or ev.get("stop_hook_active"):
        return None
    reply, follow = capture.previous_and_follow(ev.get("transcript_path"))
    if not reply.strip() or not follow.strip():
        return None
    return {"kind": "hindsight", "session": str(ev.get("session_id") or ""), "reply": reply, "follow": follow,
            "cwd": str(ev.get("cwd") or os.getcwd())}


def main():
    if os.environ.get("LESSON_REFLEX_JUDGING"):
        return 0
    try:
        job = job_for(json.load(sys.stdin))
        if job is None:
            return 0
        proc = subprocess.Popen([sys.executable, os.path.join(HERE, "judge.py")], stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        proc.stdin.write(json.dumps(job).encode())
        proc.stdin.close()
    except Exception:  # noqa: BLE001 -- never block or break a stop
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
