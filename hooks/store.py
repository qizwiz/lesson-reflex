"""store -- the files lesson-reflex keeps, and the rules over them. Standard library only.

Everything lives under $LESSON_REFLEX_HOME (default ~/.claude/lesson-reflex), created private (0700 / 0600):
  lessons.jsonl      one lesson per line: tool, needle, lesson, source, at
  corrections.jsonl  one row per judged prompt: id, session, verdict, at -- plus the prompt text ONLY for corrections
  dismissed.txt      correction ids marked "the judge misread this"
  inbox/<session>    correction ids waiting to be announced to that session (consumed when announced)
  fired.json         {session: [keys already injected]} -- each lesson fires once per session
"""
import contextlib
import fcntl
import hashlib
import json
import os
import tempfile
import time

TOOLS = ("Bash", "Edit", "MultiEdit", "Write")
KEEP_SESSIONS = 256  # entries: a session's main thread and each of its subagents count separately
# Credential shapes: (prefix, characters allowed after it, minimum count). The prefix must start a token (start of
# text or after a non-key character), so "flask-sqlalchemy" is not "sk-...". A lesson is read back into every matching
# session, so it must never carry one. Facts about shape, not a guarantee: describe secrets, never paste them.
KEY = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
UPPER_DIGITS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
# (prefix, allowed characters, minimum length, exact length or 0). Generated keys contain digits, so a tail with no
# digit ("sk-learn-compatible-estimator") is a name, not a key. AWS key ids are exactly 16 characters after the prefix.
SECRET_SHAPES = (
    ("ghp_", KEY, 20, 0), ("gho_", KEY, 20, 0), ("ghs_", KEY, 20, 0), ("github_pat_", KEY, 20, 0),  # GitHub
    ("sk-ant-", KEY, 20, 0), ("sk-proj-", KEY, 20, 0), ("sk-", KEY, 20, 0),  # Anthropic / OpenAI
    ("sk_live_", KEY, 16, 0), ("sk_test_", KEY, 16, 0), ("rk_live_", KEY, 16, 0),  # Stripe
    ("xoxb-", KEY, 16, 0), ("xoxp-", KEY, 16, 0), ("xoxa-", KEY, 16, 0), ("xoxr-", KEY, 16, 0),  # Slack
    ("glpat-", KEY, 16, 0),  # GitLab
    ("AKIA", UPPER_DIGITS, 16, 16), ("ASIA", UPPER_DIGITS, 16, 16),  # AWS access key ids
    ("AIza", KEY, 30, 0),  # Google API keys
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
    rows = []
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


def carries_secret(text):
    """PURE: does TEXT contain a token that starts with a known credential prefix and continues with enough key
    characters to be a real key?"""
    text = text or ""
    for prefix, allowed, minimum, exact in SECRET_SHAPES:
        start = text.find(prefix)
        while start >= 0:
            at_boundary = start == 0 or text[start - 1] not in KEY
            tail = ""
            for ch in text[start + len(prefix):]:
                if ch not in allowed:
                    break
                tail += ch
            long_enough = len(tail) == exact if exact else len(tail) >= minimum
            if at_boundary and long_enough and any(ch.isdigit() for ch in tail):
                return True
            start = text.find(prefix, start + 1)
    return False


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


def inbox_path(session):
    return path(os.path.join("inbox", session_file(session)))
