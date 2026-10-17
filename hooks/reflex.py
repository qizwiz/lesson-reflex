#!/usr/bin/env python3
"""reflex -- PreToolUse: put the right lesson in front of Claude at the moment it is about to repeat the mistake.

Two things are injected:
  1. on any tool call: a correction the judge flagged for this session -> "you were corrected; record the lesson".
     Announced once, on the main thread only (never inside a subagent); skipped if it was dismissed or a lesson
     already cites it; exactly the announced entries are removed from the inbox.
  2. on Bash / Edit / MultiEdit / Write: every lesson whose tool matches and whose needle occurs in the action text,
     once per session (per subagent, separately).
"""
import json
import os
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402

# The CLI by absolute path, so Claude never runs some other program called `lesson` from PATH.
CLI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "lesson")
MINT = (
    "[lesson-reflex] The user CORRECTED you (correction {cid}: {prompt!r}). In one line, say what was wrong and fix "
    "it. Then record the lesson before moving on, so it fires next time at the action where the mistake started.\n"
    "TOOL is one of Bash, Edit, MultiEdit, Write:\n"
    "  python3 {cli} add --tool TOOL --needle 'TEXT IN THAT ACTION' "
    "--lesson 'what to do instead, and what the mistake cost' --source correction:{cid}\n"
    "If it was not a correction: python3 {cli} dismiss {cid}"
)
HINDSIGHT_MINT = (
    "[lesson-reflex] What followed shows your PREVIOUS reply was wrong (hindsight {cid}; it ended: {prompt!r}). "
    "In one line, say what was wrong and fix it if it still matters. Then record the lesson so it fires next time at "
    "the action where the mistake started.\nTOOL is one of Bash, Edit, MultiEdit, Write:\n"
    "  python3 {cli} add --tool TOOL --needle 'TEXT IN THAT ACTION' "
    "--lesson 'what to do instead, and what the mistake cost' --source correction:{cid}\n"
    "If the reply was not wrong: python3 {cli} dismiss {cid}"
)


def action_text(tool, tool_input):
    """PURE: the text of the action about to run."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    if tool == "Bash":
        return str(ti.get("command") or "")
    if tool == "Edit":
        return str(ti.get("new_string") or "")
    if tool == "MultiEdit":
        return "\n".join(str(e.get("new_string") or "") for e in ti.get("edits") or [] if isinstance(e, dict))
    if tool == "Write":
        return str(ti.get("content") or "")
    return ""


def read_inbox(session):
    try:
        with open(store.inbox_path(session)) as fh:
            return [line.strip() for line in fh if line.strip()]
    except OSError:
        return []


def consume_inbox(session, handled):
    """Remove only the HANDLED ids; anything the judge queued since stays. Call under store.locked()."""
    left = [cid for cid in read_inbox(session) if cid not in handled]
    path = store.inbox_path(session)
    if left:
        store.write_atomic(path, "".join(cid + "\n" for cid in left))
    else:
        try:
            os.remove(path)
        except OSError:
            pass


def build(tool, text, lessons, corrections, fired, inbox, dismissed=frozenset()):
    """PURE: (messages to inject, keys now fired)."""
    out, now = [], set(fired)
    by_id = {c.get("id"): c for c in corrections}
    closed = store.cited_ids(lessons) | set(dismissed)
    for cid in inbox:
        key = "correction:" + cid
        if key in now or cid in closed or cid not in by_id:
            continue
        template = HINDSIGHT_MINT if by_id[cid].get("kind") == "hindsight" else MINT
        out.append(template.format(cli=shlex.quote(CLI), cid=cid, prompt=str(by_id[cid].get("prompt", ""))[-160:]
                                   if by_id[cid].get("kind") == "hindsight" else str(by_id[cid].get("prompt", ""))[:160]))
        now.add(key)
    for r in store.matching(tool, text, lessons):
        key = r["tool"] + ":" + r["needle"]
        if key in now:
            continue
        out.append(f"[lesson-reflex] LESSON ({r['needle']!r}): {r['lesson']}")
        now.add(key)
    return out, now


def main():
    try:
        ev = json.load(sys.stdin)
        tool = str(ev.get("tool_name") or "")
        session = str(ev.get("session_id") or "")
        agent = str(ev.get("agent_id") or "")
        # A subagent shares its parent's session id. Corrections are for the main thread only; lessons fire once per
        # (session, agent), so one firing inside a subagent does not use up the main thread's.
        key = session + ("/" + agent if agent else "")
        lessons = store.read_jsonl(store.path("lessons.jsonl"))
        text = action_text(tool, ev.get("tool_input"))
        may_announce = not agent and os.path.exists(store.inbox_path(session))
        if not may_announce and not store.matching(tool, text, lessons):
            return 0
        with store.locked():
            inbox = read_inbox(session) if may_announce else []
            out, now = build(tool, text, lessons, store.read_jsonl(store.path("corrections.jsonl")),
                             store.fired_for(store.load_state(), key), inbox, store.dismissed_ids())
            if now:
                store.write_atomic(store.path("fired.json"), json.dumps(store.merged(store.load_state(), key, now)))
            if inbox:  # each id read here was announced or is closed: remove exactly those
                consume_inbox(session, set(inbox))
        if out:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                     "additionalContext": "\n\n".join(out)}}))
    except Exception:  # noqa: BLE001 -- a lesson hook must never block a tool call
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
