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


def _is_cut(sl):
    """PURE: does this slice cut text to a length -- an end bound (x[:N], x[a:b]) or a negative start (x[-N:])? A
    positive start with no end (argv[1:]) only drops a prefix and is not a cut."""
    if sl.upper is not None:
        return True
    low = sl.lower
    if low is None:
        return False
    if isinstance(low, ast.UnaryOp) and isinstance(low.op, ast.USub):
        return True
    return isinstance(low, ast.Constant) and isinstance(low.value, int) and low.value < 0


def _is_slice(node):
    return isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice) and _is_cut(node.slice)


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
        if (isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Slice) and _is_cut(n.slice)
                and not (isinstance(n.value, ast.Call) and _call_name(n.value) in REDACTORS)):
            return True
        if isinstance(n, ast.Call) and _call_name(n) in slicers:
            return True
        if isinstance(n, ast.Name) and n.id in tainted_names:
            return True
    return False


def _bound_names(target):
    """PURE: the plain names an assignment target binds (recursing into tuple/list/starred unpacking only)."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [x for e in target.elts for x in _bound_names(e)]
    if isinstance(target, ast.Starred):
        return _bound_names(target.value)
    return []


COMPS = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)


def _pos(n):
    return (getattr(n, "lineno", 0), getattr(n, "col_offset", 0))


def _scope_taint(scope: ast.AST, slicers: set[str], upto=None, initial=frozenset()) -> set[str]:
    """The names in SCOPE that hold cut text at position UPTO (the whole scope when None), in source order: plain,
    annotated (`t: str = p[:600]`) and augmented assignment, loop and comprehension variables (bound from what they
    iterate over), and a list a cut value is appended or extended into. Clean text bound to a name clears it, and a
    use only sees bindings that come before it."""
    tainted: set[str] = set(initial)
    nodes = sorted((n for n in _own_nodes(scope) if hasattr(n, "lineno")), key=_pos)
    for n in nodes:
        if upto is not None and _pos(n) >= upto:
            break
        if isinstance(n, (ast.For, ast.AsyncFor, *COMPS)):
            gens = [n] if isinstance(n, (ast.For, ast.AsyncFor)) else n.generators
            for g in gens:
                names = set(_bound_names(g.target))
                if _tainted(g.iter, slicers, tainted):
                    tainted |= names
                else:
                    tainted -= names
        elif isinstance(n, ast.Assign):
            # names a value lands in: plain names and `a, b = f()` unpacking -- not self.x or d[k], which would
            # taint the whole object. Clean text assigned to a name clears its taint.
            names = {x for t in n.targets for x in _bound_names(t)}
            if _tainted(n.value, slicers, tainted):
                tainted |= names
            else:
                tainted -= names
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
    parent = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}

    def enclosing(node):
        """The nearest scope that contains NODE (a function, lambda or the module)."""
        up = parent.get(node)
        while up is not None and not isinstance(up, FUNCS):
            up = parent.get(up)
        return up if up is not None else tree

    def inherited(scope):
        """What a nested function or lambda sees from the scope that encloses it, as of where it is defined
        (QA: `t = p[:600]; g = lambda: redact(t)` was missed once lambdas became their own scopes)."""
        if scope is tree:
            return frozenset()
        outer = enclosing(scope)
        return frozenset(_scope_taint(outer, slicers, upto=_pos(scope), initial=inherited(outer)))

    for scope in scopes:
        start = inherited(scope)
        for n in _own_nodes(scope):
            if not (isinstance(n, ast.Call) and _call_name(n) in REDACTORS):
                continue
            tainted = _scope_taint(scope, slicers, upto=_pos(n), initial=start)  # only bindings BEFORE the call
            if any(_tainted(a, slicers, tainted) for a in [*n.args, *(k.value for k in n.keywords)]):
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
    ck("keyword-argument", lines("x = redact(text=s[:9])\n") == [1])
    ck("prefix-drop-is-not-a-cut", lines("x = sanitize(' '.join(argv[1:]))\n") == [])
    ck("negative-start-is-a-cut", lines("x = redact(s[-40:])\n") == [1])
    ck("attribute-target-does-not-taint-object",
       lines("def f(self, t):\n    self.x = t[:10]\n    return redact(self.y)\n") == [])
    ck("reassignment-clears", lines("def f(s):\n    t = s[:9]\n    t = s\n    return redact(t)\n") == [])
    ck("later-binding-does-not-taint-earlier-use",
       lines("def f(fields, s):\n    out = {k: scrub(v) for k, v in fields.items()}\n    v = s[:9]\n"
             "    return out, v\n") == [])
    ck("chunks-loop", lines("def f(s):\n    for c in [s[i:i + 9] for i in range(0, len(s), 9)]:\n"
                            "        redact(c)\n") == [3])
    ck("chunks-comprehension", lines("def f(chunks):\n    cut = [x[:9] for x in chunks]\n"
                                     "    return [redact(c) for c in cut]\n") == [3])
    ck("lambda-sees-enclosing-taint", lines("def f(p):\n    t = p[:600]\n    g = lambda: redact(t)\n"
                                            "    return g\n") == [3])
    ck("nested-def-sees-enclosing-taint", lines("def f(p):\n    t = p[:9]\n    def g():\n"
                                                "        return redact(t)\n    return g\n") == [4])
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
