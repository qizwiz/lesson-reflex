#!/usr/bin/env python3
"""ast_reflex -- PreToolUse: structural lessons fire BEFORE the edit lands, on the file as it would be after it.

A text lesson matches a substring of the action; a structural mistake lives in the data flow, often across two
functions, where no substring sees it. This hook builds the post-edit source (Write's content, or the current file
with Edit/MultiEdit's replacements applied), runs ast_lessons' checks, and shows each NEW finding -- one the file did
not already have -- once per session. It never blocks. Python files only: *.py, or an extensionless script whose
first line is a python shebang.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import store  # noqa: E402
KEEP = 16


def is_python(path, text):
    """PURE: a .py file, or an extensionless script whose first line is a python shebang."""
    if path.endswith(".py"):
        return True
    first = text.split("\n", 1)[0]
    return first.startswith("#!") and "python" in first


def after_edit(tool, ti, read=None):
    """(before, after) source for this edit, or (None, None) when it cannot be built."""
    path = str(ti.get("file_path") or "")
    before = read(path) if read else None
    if tool == "Write":
        after = str(ti.get("content") or "")
        return (before or "", after) if is_python(path, after) else (None, None)
    if before is None or not is_python(path, before):
        return None, None
    edits = ti.get("edits") if tool == "MultiEdit" else [ti]
    after = before
    for e in edits or []:
        old, new = str(e.get("old_string") or ""), str(e.get("new_string") or "")
        if not old or old not in after:
            return None, None
        after = after.replace(old, new) if e.get("replace_all") else after.replace(old, new, 1)
    return before, after


def new_findings(before, after, check):
    """Findings in AFTER whose (check, message, source line text) did not already exist in BEFORE."""
    def keyed(src):
        lines = src.splitlines()
        return {(c, m, lines[ln - 1].strip() if 0 < ln <= len(lines) else "") for c, ln, m in check(src)}

    old = keyed(before) if before else set()
    lines = after.splitlines()
    out = []
    for c, ln, m in check(after):
        text = lines[ln - 1].strip() if 0 < ln <= len(lines) else ""
        if (c, m, text) not in old:
            out.append((c, ln, m, text))
    return out


def _read(path):
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return None


def handle(ev, check=None, state_path=None, read=_read):
    tool = str(ev.get("tool_name") or "")
    ti = ev.get("tool_input") if isinstance(ev.get("tool_input"), dict) else {}
    if tool not in ("Write", "Edit", "MultiEdit"):
        return ""
    before, after = after_edit(tool, ti, read)
    if after is None:
        return ""
    if check is None:
        import ast_lessons

        check = ast_lessons.check_source
    found = new_findings(before, after, check)
    if not found:
        return ""
    session = str(ev.get("session_id") or "")
    state_path = state_path or store.path("ast-fired.json")
    store.ensure_home()
    try:
        with open(state_path) as fh:
            state = json.load(fh)
        state = state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        state = {}
    fired = set(state.get(session) or [])
    path = str(ti.get("file_path"))
    msgs = []
    for c, ln, m, text in found:
        key = f"{path}|{c}|{text}"
        if key in fired:
            continue
        fired.add(key)
        msgs.append(f"[ast-lesson {c}] {os.path.basename(path)}:{ln}: {m}\n    {text[:160]}")
    if not msgs:
        return ""
    state = {k: v for k, v in state.items() if k != session}
    state[session] = sorted(fired)
    state = dict(list(state.items())[-KEEP:])
    try:
        tmp = f"{state_path}.{os.getpid()}"
        with open(tmp, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, state_path)
    except OSError:
        pass
    return "\n".join(msgs) + "\n(A structural lesson from past mistakes; fix it in this edit if it applies.)"


def selftest():
    import tempfile

    import ast_lessons

    fails, ran = [], []

    def ck(name, cond):
        ran.append(name)
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        if not cond:
            fails.append(name)

    base = "def a(t):\n    return t\n\ndef h(x):\n    return redact(a(x))[:600]\n"
    with tempfile.TemporaryDirectory() as d:
        f, st = os.path.join(d, "m.py"), os.path.join(d, "s.json")
        with open(f, "w") as fh:
            fh.write(base)

        def run(tool, ti, session="S"):
            return handle({"tool_name": tool, "tool_input": dict(ti, file_path=ti.get("file_path", f)),
                           "session_id": session}, ast_lessons.check_source, st)

        bad_edit = {"old_string": "    return t\n", "new_string": "    return json.dumps(t)[:600]\n"}
        out = run("Edit", bad_edit)
        ck("edit-that-introduces-it-fires", "slice-into-redact" in out and "m.py:5" in out)
        ck("same-finding-once-per-session", run("Edit", bad_edit) == "")
        ck("other-session-fires", run("Edit", bad_edit, "T") != "")
        ck("clean-edit-silent", run("Edit", {"old_string": "return t", "new_string": "return t  # ok"}, "U") == "")
        ck("write-new-file-fires", "slice-into-redact" in run(
            "Write", {"file_path": os.path.join(d, "n.py"), "content": "y = redact(s[:9])\n"}, "V"))
        with open(f, "w") as fh:
            fh.write("y = redact(s[:9])\nz = 1\n")
        ck("unrelated-edit-in-a-file-with-an-old-finding-silent",
           run("Edit", {"old_string": "z = 1", "new_string": "z = 2"}, "W") == "")
        ck("extensionless-python-script-checked", "slice-into-redact" in run(
            "Write", {"file_path": os.path.join(d, "tool"), "content": "#!/usr/bin/env python3\ny = redact(s[:9])\n"},
            "SH"))
        ck("non-python-ignored", run("Write", {"file_path": os.path.join(d, "x.md"), "content": "redact(s[:9])"},
                                     "X") == "")
        ck("stale-anchor-silent", run("Edit", {"old_string": "NOT THERE", "new_string": "z"}, "Y") == "")
        ck("multiedit-through-a-name", "slice-into-redact" in run("MultiEdit", {"edits": [
            {"old_string": "y = redact(s[:9])", "new_string": "q = s[:4]\ny = redact(q)"}]}, "Z"))
    print(f"\n  RESULT: {'FAIL' if fails else 'PASS'} -- {len(ran) - len(fails)}/{len(ran)} cases")
    return 1 if fails else 0


def main():
    if "--selftest" in sys.argv:
        return selftest()
    try:
        out = handle(json.load(sys.stdin))
    except Exception:  # noqa: BLE001 -- a lesson must never block an edit
        return 0
    if out:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": out}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
