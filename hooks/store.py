"""store -- the files lesson-reflex keeps, and the rules over them. Standard library only.

Everything lives under $LESSON_REFLEX_HOME (default ~/.claude/lesson-reflex), created private (0700 / 0600):
  lessons.jsonl      one lesson per line: tool, needle, lesson, source, at
  corrections.jsonl  one row per judgement: id, session, kind, verdict, at -- text (masked, then cut) only when it is
                     announced: a correction, a hindsight error or a tool-failure mistake
  dismissed.txt      correction ids marked "the judge misread this"
  inbox/<session>    correction ids waiting to be announced to that session (consumed when announced)
  fired.json         {session/agent: [keys already injected]} -- each lesson fires once per session and agent
  failures-seen.jsonl  hashed (session, program) keys already sent to the failure judge
  hindsight-seen.jsonl replies already sent to the hindsight judge
  ast-fired.json     {session: [structural findings already shown]}, written under ast.lock
"""
import bisect
import contextlib
import fcntl
import hashlib
import json
import os
import tempfile
import time

TOOLS = ("Bash", "Edit", "MultiEdit", "Write")
KEEP_SESSIONS = 256  # entries: a session's main thread and each of its subagents count separately
# Credential shapes, found by shape alone wherever they start (see key_spans). A lesson is read back into every
# matching session, so it must never carry one. Facts about shape, not a guarantee: describe secrets, never paste them.
KEY = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
UPPER_DIGITS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
ALNUM = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
ALNUM_U = ALNUM | {"_"}
# (prefix, allowed characters, minimum length, exact length or 0). A body needs a digit or mixed case
# ("sk-learn-compatible-estimator" is a name: lowercase, and its run stops at the hyphen); AWS ids, one case only, don't.
SECRET_SHAPES = (
    ("ghp_", ALNUM, 36, 0), ("gho_", ALNUM, 36, 0), ("ghu_", ALNUM, 36, 0), ("ghs_", ALNUM, 36, 0),  # GitHub
    ("ghr_", ALNUM, 36, 0), ("github_pat_", ALNUM_U, 22, 0),
    ("AKIA", UPPER_DIGITS, 16, 0), ("ASIA", UPPER_DIGITS, 16, 0),  # AWS access key ids (16 or more)
    ("xoxb-", KEY, 10, 0), ("xoxa-", KEY, 10, 0), ("xoxp-", KEY, 10, 0), ("xoxr-", KEY, 10, 0),  # Slack
    ("xoxs-", KEY, 10, 0), ("xapp-", KEY, 10, 0),
    ("sk-proj-", KEY, 32, 0), ("sk-svcacct-", KEY, 32, 0), ("sk-admin-", KEY, 32, 0),  # OpenAI: _ and - in body
    ("sk-None-", KEY, 32, 0), ("sk-or-v1-", ALNUM, 64, 0), ("sk-", ALNUM, 32, 0),  # OpenRouter; generic sk-
    ("sk-", KEY, 22, 22),  # LiteLLM virtual keys: sk- + token_urlsafe(16), exactly 22, digit AND mixed case
    ("sk_test_", ALNUM, 20, 0), ("rk_test_", ALNUM, 20, 0),  # Stripe test
    ("glpat-", KEY, 20, 0), ("shpat_", ALNUM, 32, 0), ("AIza", KEY, 35, 0), ("sk-ant-", KEY, 24, 0),  # GitLab etc.
    ("gsk_", ALNUM, 40, 0), ("sk_live_", ALNUM, 20, 0), ("rk_live_", ALNUM, 20, 0), ("hf_", ALNUM, 30, 0),
    ("npm_", ALNUM, 36, 0), ("ya29.", KEY, 20, 0),  # Groq, Stripe live, Hugging Face, npm, Google OAuth
)


def home():
    return os.environ.get("LESSON_REFLEX_HOME") or os.path.expanduser("~/.claude/lesson-reflex")


def path(name):
    return os.path.join(home(), name)


def ensure_home():
    """Create the home private, and tighten one left by an older version."""
    os.makedirs(home(), mode=0o700, exist_ok=True)
    try:
        os.chmod(home(), 0o700)
    except OSError:
        pass


def read_jsonl(p):
    """Dict rows of a JSONL file; a corrupt or non-object line costs only itself, a missing file is empty."""
    rows: list[dict] = []
    try:
        with open(p, errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line) if line.strip() else None
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def append_jsonl(p, row):
    ensure_home()
    os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as fh:
        fh.write(json.dumps(row) + "\n")


def write_atomic(p, text):
    """Replace P with TEXT via a unique temp file in the same directory (never a shared .tmp name)."""
    ensure_home()
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


@contextlib.contextmanager
def locked(name="state.lock"):
    """An exclusive lock for a read-modify-write across concurrent sessions."""
    ensure_home()
    fd = os.open(path(name), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# prefixes ordinary words END in (risk-, disk_test_, task_live_, work_live_) -> the minimum run after it; these need
# a DIGIT in the body (mixed case is not enough: disk_test_UsesTheTemporaryDirectory is a name). A real key of these
# families has no digit about 1.5% of the time (24 random letters and digits), a name almost always.
GENERIC = {"sk-": 32, "sk_test_": 20, "sk_live_": 20, "rk_test_": 20, "rk_live_": 20}


def _runs(text, allowed):
    """PURE: run[i] = how many characters from i on are in ALLOWED -- one pass from the end, so every lookup is O(1)."""
    run = [0] * (len(text) + 1)
    for i in range(len(text) - 1, -1, -1):
        run[i] = run[i + 1] + 1 if text[i] in allowed else 0
    return run


def _next(text, pred):
    """PURE: nxt[i] = the first index >= i whose character satisfies PRED (len(text) if none)."""
    nxt = [len(text)] * (len(text) + 1)
    for i in range(len(text) - 1, -1, -1):
        nxt[i] = i if pred(text[i]) else nxt[i + 1]
    return nxt


def key_spans(text):
    """PURE: (start, end) of every credential-shaped token in TEXT, merged, in order -- found by SHAPE ALONE, whatever
    comes before it. A known prefix followed by enough key characters, with a digit or mixed case among them, is a
    key; the generic `sk-` (ordinary words end in it: risk-, task-) needs 32+ letters and digits after it, one of them
    a digit (a DeepSeek-style key is lowercase hex; a CamelCase name has no digit), or -- a LiteLLM key -- exactly 22
    letters, digits, _ and - with a digit and mixed case. An AWS id is exactly 16 after
    the prefix, or a longer run whose first 16 hold a digit.

    WHY no boundary: requiring one ("the character before is not a key character") was beaten four audit rounds
    running by one more encoding in front of the key -- \\n, then \\a and \\033, then \\xAKIA, then %20, ANSI
    colour codes, ^M, &#32. A list of encodings never ends; a shape does not need one. Linear in TEXT."""
    text = text or ""
    if not text:
        return []
    runs = {}
    digit, upper, lower = _next(text, str.isdigit), _next(text, str.isupper), _next(text, str.islower)
    spans: list = []

    def judge(prefix, allowed, minimum, exact, start, end):
        """The span of the key at START if the run from its body to END (or the end of the run) has the row's shape."""
        p = start + len(prefix)
        if id(allowed) not in runs:  # built once per alphabet (setdefault would build it on every call)
            runs[id(allowed)] = _runs(text, allowed)
        n = min(runs[id(allowed)][p], end - p)
        varied = digit[p] < p + n or (upper[p] < p + n and lower[p] < p + n)
        if allowed is UPPER_DIGITS:  # AWS: exactly 16, or a longer run whose first 16 hold a digit
            ok = n == minimum or (n > minimum and digit[p] < p + minimum)  # a word like ASIA+PACIFIC... is not a key
        else:  # a digit, or mixed case, in the body (a digit-free key is rare, not impossible)
            ok = (n == exact if exact else n >= minimum) and varied
        if ok and prefix in GENERIC:  # a word-ending prefix: a digit (and, for an exact shape, mixed case)
            ok = (digit[p] < p + n and upper[p] < p + n and lower[p] < p + n) if exact else (
                n >= GENERIC[prefix] and digit[p] < p + n)
        return (start, p + n) if ok else None

    # rows that only need a minimum length are judged on the whole run
    for row in SECRET_SHAPES:
        if row[1] is UPPER_DIGITS or row[3]:
            continue
        start = text.find(row[0])
        while start >= 0:
            span = judge(*row, start, len(text))
            if span:
                spans.append(span)
            start = text.find(row[0], start + 1)
    # rows that need an EXACT length (AWS, LiteLLM) are judged right to left, each run cut where the nearest key found
    # to its right starts -- of any family, these included -- which is what a second pass over the masked text would
    # see (the seventh and eighth audits: a digit-free AKIA id or a LiteLLM key glued to another key read as one long
    # run and was missed). One pass, linear but for the sorted insert.
    starts = sorted(a for a, _b in spans)
    candidates = []
    for row in SECRET_SHAPES:
        if row[1] is UPPER_DIGITS or row[3]:
            start = text.find(row[0])
            while start >= 0:
                candidates.append((start, row))
                start = text.find(row[0], start + 1)
    for start, row in sorted(candidates, key=lambda c: -c[0]):
        p = start + len(row[0])
        k = bisect.bisect_left(starts, p)
        span = judge(*row, start, starts[k] if k < len(starts) else len(text))
        if span:
            spans.append(span)
            bisect.insort(starts, start)
    merged: list = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def carries_secret(text):
    """PURE: does TEXT contain a credential-shaped token (see key_spans)?"""
    return bool(key_spans(text))


def mask_values(obj):
    """PURE: OBJ with every string VALUE inside it masked (dictionary keys are left as they are) -- for structured input (tool arguments) that is about to become
    JSON. Masking after json.dumps misses a key on its own line: the newline becomes the two characters \\n, and the
    escape could hide the key from a reader that needs a boundary before it (this masker no longer does; see key_spans)."""
    if isinstance(obj, str):
        return mask(obj)
    if isinstance(obj, dict):
        return {k: mask_values(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [mask_values(v) for v in obj]
    return obj


def mask(text):
    """PURE: TEXT with every key key_spans finds replaced by <secret> -- ONE pass. Repeating until nothing changed
    peeled a run of digit-free AKIA blocks one block per pass (the sixth audit: 4 KB took 3.6 s). key_spans settles in
    one pass instead: it judges the exact-length shapes (AWS, LiteLLM) right to left, each run cut where the nearest
    key found to its right starts -- of any family, those included -- which is what a second pass over the masked
    text would have seen. The tests assert that masked text carries no key, glued pairs included."""
    return mask_once(text)


def mask_once(text):
    """PURE: TEXT with every credential-shaped token (key_spans) replaced by <secret>. Callers mask the WHOLE text and
    only then cut it -- cutting first can split a token below the length that is recognised."""
    text = text or ""
    out, i = [], 0
    for a, b in key_spans(text):
        out.append(text[i:a])
        out.append("<secret>")
        i = b
    out.append(text[i:])
    return "".join(out)


def validate(row, existing):
    """PURE: '' if ROW may be added as a lesson, else why not. ROW's needle is already stripped."""
    if row.get("tool") not in TOOLS:
        return f"--tool must be one of {', '.join(TOOLS)}"
    if len(row.get("needle") or "") < 4:
        return "--needle must be at least 4 characters: a short needle fires on everything and teaches nothing"
    if len((row.get("lesson") or "").strip()) < 20:
        return "--lesson must say what to do instead and what the mistake cost"
    if any(carries_secret(str(row.get(k) or "")) for k in ("needle", "lesson", "source")):
        return "a field carries a credential-shaped token -- describe its shape, never the value"
    if any(e.get("tool") == row["tool"] and e.get("needle") == row["needle"] for e in existing):
        return "a lesson with that tool and needle already exists -- `lesson list`"
    return ""


def matching(tool, text, lessons):
    """PURE: the lessons whose tool matches and whose needle occurs in TEXT, in file order."""
    if not text:
        return []
    return [r for r in lessons if r.get("tool") == tool and isinstance(r.get("needle"), str) and r["needle"]
            and r["needle"] in text and isinstance(r.get("lesson"), str)]


def fired_for(state, session):
    """PURE: keys already injected in SESSION."""
    got = (state or {}).get(session)
    return set(got) if isinstance(got, list) else set()


def merged(state, session, fired, keep=KEEP_SESSIONS):
    """PURE: STATE with SESSION's fired set replaced, keeping only the KEEP most recently written sessions."""
    out = {k: v for k, v in (state or {}).items() if k != session}
    out[session] = sorted(fired)
    return dict(list(out.items())[-keep:])


def load_state():
    try:
        with open(path("fired.json")) as fh:
            got = json.load(fh)
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def cited_ids(lessons):
    return {str(r.get("source", "")).removeprefix("correction:") for r in lessons}


def pending(corrections, lessons, dismissed):
    """PURE: corrections the judge flagged that no lesson cites and nobody dismissed."""
    cited = cited_ids(lessons)
    return [c for c in corrections if c.get("verdict") == "correction" and c.get("id")
            and c["id"] not in cited and c["id"] not in dismissed]


def dismissed_ids():
    try:
        with open(path("dismissed.txt")) as fh:
            return {line.strip() for line in fh if line.strip()}
    except OSError:
        return set()


def new_id():
    return time.strftime("%Y%m%d-%H%M%S-") + os.urandom(3).hex()


def session_file(session):
    """PURE: a collision-free file name for a session id (a hash; distinct ids never share an inbox)."""
    return hashlib.sha256(("s:" + str(session)).encode()).hexdigest()[:32]


def add_to_inbox(session, cid):
    """Queue correction CID for SESSION, under the same lock reflex.py takes to read and consume the inbox."""
    with locked():
        p = inbox_path(session)
        os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(cid + "\n")


def spawn_judge(job):
    """Hand JOB to judge.py in a detached background process, so no hook ever waits on a model call."""
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "judge.py")],
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    if proc.stdin is not None:
        proc.stdin.write(json.dumps(job).encode())
        proc.stdin.close()


def inbox_path(session):
    return path(os.path.join("inbox", session_file(session)))
