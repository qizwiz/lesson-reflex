#!/usr/bin/env python3
"""ast_lessons -- lessons that match the STRUCTURE of code, not a substring of it.

Some mistakes live in data flow, often across two functions, where no text match can see them. The one shipped
check, slice-into-redact, is a bug that recurred five times while this plugin was being built: text is cut (x[:N])
and only then passed to a masking function, so a credential straddling the cut becomes a fragment too short to be
recognised, and that fragment is stored or sent. It is found whether the cut happens inline, through a local name
(plain, annotated, augmented or tuple-unpacking assignment, or a list it is appended to), or inside a function that
returns cut text (computed to a fixed point). Each function is its own scope. Deterministic: a fact about the parse
tree, not a judgement. A slice of a masking call's RESULT is safe (cut after masking).

To add a check: write a function source -> [(line, message)] and add it to CHECKS.

  python3 ast_lessons.py check FILE...   print findings, exit 1 if any
  python3 ast_lessons.py --selftest
"""
import ast
import sys


def _is_slice(node):
    return isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice)


def _contains(node, pred):
    return any(pred(n) for n in ast.walk(node))


def _call_name(call):
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
# the usual names for a function that masks secrets in text
REDACTORS = ("redact", "mask", "scrub", "sanitize", "anonymize", "mask_secrets")


def _own_nodes(scope):
    """Nodes of SCOPE, NOT descending into nested functions -- each function is its own scope, so a tainted name in
    one function never flags a same-named parameter in another."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        n = stack.pop()
        yield n
        if not isinstance(n, FUNCS):
            stack.extend(ast.iter_child_nodes(n))


def _tainted(expr, slicers, tainted_names):
    """Does EXPR carry text that was cut before it got here?"""
    for n in ast.walk(expr):
        # a slice of redact(...)'s RESULT is cut AFTER redaction -- safe, and it may well be redacted again later
        if (isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Slice)
                and not (isinstance(n.value, ast.Call) and _call_name(n.value) in REDACTORS)):
            return True
        if isinstance(n, ast.Call) and _call_name(n) in slicers:
            return True
        if isinstance(n, ast.Name) and n.id in tainted_names:
            return True
    return False


def _scope_taint(scope: ast.AST, slicers: set[str]) -> set[str]:
    """The names in SCOPE that hold cut text, in source order: plain, annotated (`t: str = p[:600]`) and augmented
    assignment, and a list that a cut value is appended or extended into."""
    tainted: set[str] = set()
    nodes = sorted((n for n in _own_nodes(scope) if hasattr(n, "lineno")),
                   key=lambda n: (getattr(n, "lineno", 0), getattr(n, "col_offset", 0)))
    for n in nodes:
        if isinstance(n, ast.Assign) and _tainted(n.value, slicers, tainted):
            # every name a tainted value lands in, including `a, b = f()` unpacking
            tainted |= {x.id for t in n.targets for x in ast.walk(t) if isinstance(x, ast.Name)}
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign)) and n.value is not None \
                and isinstance(n.target, ast.Name) and _tainted(n.value, slicers, tainted):
            tainted.add(n.target.id)
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("append", "extend", "insert")
              and isinstance(n.func.value, ast.Name) and any(_tainted(a, slicers, tainted) for a in n.args)):
            tainted.add(n.func.value.id)
    return tainted


def _slicers(tree: ast.AST) -> set[str]:
    """Functions in this file that RETURN cut text -- directly, through a tainted local, or through another such
    function -- computed to a fixed point."""
    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    out: set[str] = set()
    changed = True
    while changed:
        changed = False
        for fn in funcs:
            if fn.name in out:
                continue
            tainted = _scope_taint(fn, out)
            if any(isinstance(r, ast.Return) and r.value is not None and _tainted(r.value, out, tainted)
                   for r in _own_nodes(fn)):
                out.add(fn.name)
                changed = True
    return out


def slice_into_redact(src):
    """The cut happens BEFORE redaction: redact(x[:N]), redact(name holding cut text), or redact(f(...)) where f
    returns cut text. A credential straddling the cut is left as a fragment too short to match."""
    tree = ast.parse(src)
    slicers = _slicers(tree)
    found = set()
    # every function AND lambda is a scope of its own, so a lambda's body is inspected too (a lambda is not a place
    # a slicer can be defined, but redact(s[:5]) inside one was missed once scopes stopped at nested functions)
    scopes = [n for n in ast.walk(tree) if isinstance(n, FUNCS)] + [tree]
    for scope in scopes:
        tainted = _scope_taint(scope, slicers)
        for n in _own_nodes(scope):
            if (isinstance(n, ast.Call) and _call_name(n) in REDACTORS
                    and any(_tainted(a, slicers, tainted) for a in n.args)):
                found.add((n.lineno, "text is cut before redact(): redact the whole text, then cut"))
    return sorted(found)


CHECKS = {"slice-into-redact": slice_into_redact}


def check_source(src):
    """[(check, line, message)] for SRC, or [] if it does not parse (a fragment is not a file)."""
    try:
        return [(name, line, msg) for name, fn in CHECKS.items() for line, msg in fn(src)]
    except SyntaxError:
        return []


def selftest():
    fails, ran = [], []

    def ck(name, cond):
        ran.append(name)
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        if not cond:
            fails.append(name)

    def lines(src):
        return [ln for ln, _ in slice_into_redact(src)]

    ck("direct", lines("x = redact(prompt[:600])\n") == [1])
    ck("redact-then-cut-is-fine", lines("x = redact(prompt)[:600]\n") == [])
    ck("through-a-name", lines("def f(p):\n    t = p[:600]\n    return redact(t)\n") == [3])
    ck("through-a-helper",
       lines("def a(ti):\n    return json.dumps(ti)[:600]\n\ndef h(ti):\n    text = a(ti)\n"
             "    return {'A': redact(text)[:600]}\n") == [6])
    ck("helper-without-slice-is-fine",
       lines("def a(ti):\n    return json.dumps(ti)\n\ndef h(ti):\n    text = a(ti)\n    return redact(text)[:600]\n")
       == [])
    ck("attribute-call", lines("y = sensitive_lib.redact(s[-400:])[0]\n") == [1])
    ck("unrelated-slice-is-fine", lines("a = b[:3]\nc = redact(d)\n") == [])
    ck("fragment-does-not-parse", check_source("    return redact(x[:5]\n") == [])
    ck("tuple-unpacking", lines("def t():\n    return 'r', 'e'[:9]\n\ndef d():\n    a, b = t()\n"
                                "    return redact(b)\n") == [6])
    ck("cut-after-redact-then-redacted-again-is-fine",
       lines("def f(xs):\n    out = []\n    for x in xs:\n        out.append(redact(x)[:300])\n"
             "    return redact(' '.join(out))\n") == [])
    ck("other-masker-names", lines("x = mask(s[:9])\ny = scrub(t[-5:])\nz = sanitize(u)\n") == [1, 2])
    ck("inside-a-lambda", lines("f = lambda s: redact(s[:5])\n") == [1])
    ck("annotated-assign", lines("def f(p):\n    t: str = p[:600]\n    return redact(t)\n") == [3])
    ck("other-functions-names-do-not-leak",
       lines("def a(p):\n    t = p[:600]\n    return t\n\ndef b(t):\n    return redact(t)\n") == [])
    ck("through-a-list-and-two-helpers",
       lines("def part(x):\n    out = []\n    out.append(json.dumps(x)[:300])\n    return ''.join(out)\n\n"
             "def whole(rows):\n    parts = [part(r) for r in rows]\n    return parts\n\n"
             "def h(rows):\n    return redact(whole(rows))\n") == [11])
    print(f"\n  RESULT: {'FAIL' if fails else 'PASS'} -- {len(ran) - len(fails)}/{len(ran)} cases")
    return 1 if fails else 0


def main(argv):
    if argv[1:2] == ["--selftest"]:
        return selftest()
    if argv[1:2] == ["check"] and len(argv) > 2:
        bad = 0
        for path in argv[2:]:
            with open(path) as fh:
                for name, line, msg in check_source(fh.read()):
                    print(f"{path}:{line}: [{name}] {msg}")
                    bad += 1
        return 1 if bad else 0
    print(__doc__)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv))
