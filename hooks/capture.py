#!/usr/bin/env python3
"""capture -- UserPromptSubmit: hand your prompt and Claude's previous reply to the judge, in the BACKGROUND.

Returns at once; the prompt never waits on a model call. judge.py decides whether you were correcting Claude and,
if so, leaves the correction in this session's inbox, where reflex.py announces it at Claude's next tool call.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import store  # noqa: E402

TAIL = 1500  # characters of Claude's previous reply the judge sees
MAX_PROMPT = 2000  # characters of your prompt the judge sees
# Turns the harness writes into the prompt slot rather than the user: these are never judged.
HARNESS = ("<task-notification", "<system-reminder", "<command-", "<local-command", "Stop hook feedback:",
           "Your response above was stopped", "[Image:", "[Request interrupted")


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _reply(content):
    """What Claude said AND did in one assistant row: its text, plus each tool call as [tool NAME: input]. A turn
    that was only tool calls (then interrupted) is still something the user can be correcting."""
    if not isinstance(content, list):
        return _text(content)
    out = []
    for p in content:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text":
            out.append(p.get("text", ""))
        elif p.get("type") == "tool_use":
            out.append(f"[tool {p.get('name', '?')}: {json.dumps(p.get('input', {}))}]")  # whole: masked, then cut
    return "\n".join(x for x in out if x.strip())


INTERRUPT = "[Request interrupted"


def _is_boundary(row):
    """Does this user row end the previous exchange? Any prompt the user sent -- typed text, an image with no text,
    a slash command -- is a boundary. Tool results, the interrupt marker, harness meta rows and background-task
    notifications are not."""
    if row.get("isMeta"):
        return False
    origin = row.get("origin")
    if isinstance(origin, dict) and origin.get("kind") == "task-notification":
        return False  # a background task finishing is not the user speaking
    content = (row.get("message") or {}).get("content")
    if isinstance(content, list) and any(isinstance(p, dict) and p.get("type") == "tool_result" for p in content):
        return False
    text = _text(content).strip()
    return not text.startswith((INTERRUPT, "<task-notification"))  # the prefix covers transcripts without `origin`


def last_reply(transcript_path, tail=TAIL):
    """Claude's newest reply since the last typed prompt -- text and tool calls, newest rows last -- the last TAIL
    characters ('' if none). Walking back stops at the previous typed prompt: a reply from an earlier exchange is not
    what is being answered."""
    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 400_000))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except (OSError, TypeError, ValueError):
        return ""
    parts = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if row.get("type") == "user" and _is_boundary(row):
            break
        if row.get("type") == "assistant":
            said = _reply((row.get("message") or {}).get("content"))
            if said.strip():
                parts.append(said)
                if sum(len(x) for x in parts) >= tail:
                    break
    return "\n".join(reversed(parts))  # whole: the judge masks, then keeps the last TAIL characters


def previous_and_follow(transcript_path):
    """(previous reply, prompt, follow-up) for hindsight. The reply that ended the PREVIOUS turn (its text and tool
    calls); the user's message that started this turn; and what followed: that message, then this turn's tool calls,
    their RESULTS and its reply. Whole texts -- the judge masks credentials, THEN cuts. ('', '', '') when there is no
    previous turn, when this turn was not started by something the user typed (a harness message, an image only), or
    when the newest 400 KB of the transcript does not reach back to the previous turn."""
    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 400_000))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except (OSError, TypeError, ValueError):
        return "", "", ""
    current: list[str] = []
    previous: list[str] = []
    prompt = None
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        content = (row.get("message") or {}).get("content")
        if row.get("type") == "user" and _is_boundary(row):
            if prompt is None:
                prompt = _text(content)
                continue
            break
        if prompt is None and row.get("type") == "user" and isinstance(content, list):
            for part in content:  # this turn's tool results: often the very thing that shows the reply was wrong
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    body = part.get("content")
                    body = body if isinstance(body, str) else _text(body)
                    if body.strip():
                        current.append(f"[result: {body}]")
        elif row.get("type") == "assistant":
            said = _reply(content)
            if said.strip():
                (current if prompt is None else previous).append(said)
    if prompt is None or not previous or not prompt.strip() or prompt.strip().startswith(HARNESS):
        return "", "", ""
    return "\n".join(reversed(previous)), prompt, "\n".join(reversed(current))


def payload(ev):
    """The judge's input for this event, or None when there is nothing to judge."""
    if not isinstance(ev, dict):
        return None
    prompt = str(ev.get("prompt") or "").strip()
    if not prompt or prompt.startswith(HARNESS):
        return None
    previous = last_reply(ev.get("transcript_path"))
    if not previous:
        return None
    return {"session": str(ev.get("session_id") or ""), "prompt": prompt, "previous": previous,
            "cwd": str(ev.get("cwd") or os.getcwd())}


def main():
    if os.environ.get("LESSON_REFLEX_JUDGING"):  # the judge's own `claude -p` must not judge itself
        return 0
    try:
        job = payload(json.load(sys.stdin))
        if job is None:
            return 0
        store.spawn_judge(job)
    except Exception:  # noqa: BLE001 -- never block or break a prompt
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
