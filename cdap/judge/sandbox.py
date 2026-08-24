"""The AST static guard — a cheap first filter, deliberately **not** a security boundary.

Read this before you decide the guard looks weak. Its weakness is the point.

What it does
------------
Before any submitted source is executed, ``check_source`` parses it to an AST and walks
the tree, rejecting a short list of obviously-dangerous constructs: imports of anything
outside a small whitelist, calls to ``eval`` / ``exec`` / ``compile`` / ``open`` /
``__import__``, and access to dunder attributes like ``__globals__`` or ``__subclasses__``
that are the first step of every sandbox-escape recipe. A violation is raised as
``SandboxViolation`` and the judge turns it into ``609 SANDBOX_VIOLATION`` — before the
code has run, which is the one thing a static check can offer that a runtime one cannot.

What it is not
--------------
It is **not** a security boundary, and the project depends on it not pretending to be.
AST guarding is bypassable in principle — getattr chains, string tricks, and constructs
this walker does not model can all reach past it — and CLAUDE.md forbids "fixing" it to
claim otherwise. The real boundary is the container: ``--network none`` is a kernel-level
block, ``--memory`` a real cgroup cap, ``--pids-limit`` an actual fork-bomb defence. The
guard is what stops the *accidental* and the *lazy*; the container is what stops the
*determined*.

This is not a hedge — it is a claim the report proves. ``experiments/backend_overhead.py``
runs the ``samples/evil_*.py`` programs with the guard **disabled** (``--no-ast-guard``)
under both backends. Under ``subprocess`` the escapes succeed: ``evil_socket.py`` opens a
connection, ``evil_fork.py`` spawns processes. Under ``docker`` the identical code fails at
the kernel. That side-by-side is a far stronger statement than any assertion in prose that
"the guard is defence-in-depth", so the guard is kept honestly weak on purpose and the
``--no-ast-guard`` switch is a supported, documented flag rather than a debugging leftover.

Why a whitelist for imports
---------------------------
A blacklist of dangerous modules is a losing game — you forget ``ctypes``, or ``os`` is
reachable as ``posix``, or a new module ships. The whitelist is the set a competitive
algorithm problem legitimately needs (``math``, ``collections``, ``heapq``, ``bisect``,
``itertools``, ``functools``, ``operator``, ``string``, ``re``, ``array``, ``decimal``,
``fractions``, ``typing``), and everything else is refused. A false positive here is
cheap — a player sees ``609`` and adjusts — while a false negative is exactly what the
container is there to catch anyway.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import FrozenSet, List, Tuple

# --------------------------------------------------------------------------
# Policy — the whole policy, in three sets, so it can be read aloud in one breath
# --------------------------------------------------------------------------

#: Modules a solution may import. Chosen for algorithm problems, not for generality.
ALLOWED_IMPORTS: FrozenSet[str] = frozenset({
    "math",
    "collections",
    "heapq",
    "bisect",
    "itertools",
    "functools",
    "operator",
    "string",
    "re",
    "array",
    "decimal",
    "fractions",
    "typing",
})

#: Builtins that turn data into code or reach the filesystem/OS. The reason each is here:
#: the first four execute strings, the next three touch the outside world, and the last
#: three (``globals``/``locals``/``vars``) hand back a namespace dict from which the rest
#: of the interpreter is reachable.
FORBIDDEN_CALLS: FrozenSet[str] = frozenset({
    "eval",
    "exec",
    "compile",
    "__import__",
    "open",
    "input",
    "globals",
    "locals",
    "vars",
    "getattr",   # the usual first hop of a getattr-chain escape; solutions never need it
    "setattr",
    "delattr",
})

#: Attribute names that begin an introspection escape. ``__class__.__bases__`` and
#: ``().__class__.__subclasses__()`` are the canonical routes from an ordinary object to
#: arbitrary types, so dunder attribute access is refused wholesale. A solution to a
#: numeric or list problem has no honest reason to read one.
FORBIDDEN_ATTR_PREFIX = "__"


class SandboxViolation(Exception):
    """Raised when the static guard refuses a source.

    Carries a human-readable reason *and* the line number, because the reason is shown to
    the player in the ``609`` verdict's detail and "line 12: import of 'os' is not allowed"
    is a far better experience than a bare rejection.
    """

    def __init__(self, reason: str, lineno: int = 0):
        self.reason = reason
        self.lineno = lineno
        super().__init__(f"line {lineno}: {reason}" if lineno else reason)


@dataclass
class GuardReport:
    """The result of a scan. ``ok`` is the headline; ``violations`` explains a refusal."""

    ok: bool
    violations: Tuple[str, ...] = ()

    def first_reason(self) -> str:
        return self.violations[0] if self.violations else ""


class _Walker(ast.NodeVisitor):
    """Collects every violation in one pass instead of stopping at the first.

    Reporting all of them is a small kindness with a real payoff on camera: a demo
    solution that trips two rules shows both at once, rather than turning into a
    fix-one-rerun-find-the-next loop while the clock runs.
    """

    def __init__(self):
        self.violations: List[Tuple[str, int]] = []

    # -- imports -----------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            top = alias.name.split(".")[0]
            if top not in ALLOWED_IMPORTS:
                self._reject(f"import of {alias.name!r} is not allowed", node)
        # No generic_visit: nothing reachable under an Import node needs checking.

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # node.module is None for a bare "from . import x"; a relative import has no place
        # in a single submitted file, so refuse it outright.
        module = node.module or ""
        top = module.split(".")[0]
        if node.level != 0:
            self._reject("relative imports are not allowed", node)
        elif top not in ALLOWED_IMPORTS:
            self._reject(f"import from {module!r} is not allowed", node)

    # -- calls -------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        # Only a bare-name call like eval(...) is matched here; an attribute call like
        # obj.eval(...) is caught (or not) by the attribute rule, on purpose — we are
        # blocking the *builtins*, not every method that happens to share a name.
        func = node.func
        if isinstance(func, ast.Name) and func.id in FORBIDDEN_CALLS:
            self._reject(f"call to {func.id!r} is not allowed", node)
        self.generic_visit(node)

    # -- attribute access --------------------------------------------------

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith(FORBIDDEN_ATTR_PREFIX):
            self._reject(f"access to dunder attribute {node.attr!r} is not allowed", node)
        self.generic_visit(node)

    # -- names -------------------------------------------------------------

    def visit_Name(self, node: ast.Name) -> None:
        # Catches the reference form `f = eval` that visit_Call would miss, since here the
        # dangerous builtin is captured without being called on the spot.
        if node.id in FORBIDDEN_CALLS:
            self._reject(f"reference to {node.id!r} is not allowed", node)
        self.generic_visit(node)

    def _reject(self, reason: str, node: ast.AST) -> None:
        self.violations.append((reason, getattr(node, "lineno", 0)))


def scan(source: str) -> GuardReport:
    """Walk ``source`` and return every policy violation, without raising.

    A syntax error is reported as a violation rather than allowed to escape, so a caller
    that only wants the guard's opinion — the profiler CLI, for one — gets a clean boolean
    instead of a traceback. The judge proper distinguishes the two: unparseable source is
    ``604 COMPILE_ERROR``, a policy breach is ``609 SANDBOX_VIOLATION``.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return GuardReport(ok=False, violations=(f"syntax error: {exc.msg}",))

    walker = _Walker()
    walker.visit(tree)
    if not walker.violations:
        return GuardReport(ok=True)

    # Order by line so the log reads top-to-bottom like the source does.
    walker.violations.sort(key=lambda item: item[1])
    reasons = tuple(
        f"line {lineno}: {reason}" if lineno else reason
        for reason, lineno in walker.violations
    )
    return GuardReport(ok=False, violations=reasons)


def check_source(source: str) -> None:
    """Raise ``SandboxViolation`` on the first policy breach; return ``None`` if clean.

    This is the form the runner uses: it wants to *stop*, with a reason and a line, the
    moment the source is unacceptable. ``scan`` is the form for callers that want the
    whole list. A ``SyntaxError`` is deliberately *not* caught here — the runner needs to
    tell ``604`` (won't parse) apart from ``609`` (parses, but hostile), and the only way
    to know the source won't parse is to let ``ast.parse`` say so.
    """
    tree = ast.parse(source)   # SyntaxError propagates -> caller maps it to 604
    walker = _Walker()
    walker.visit(tree)
    if walker.violations:
        walker.violations.sort(key=lambda item: item[1])
        reason, lineno = walker.violations[0]
        raise SandboxViolation(reason, lineno)


def main() -> int:
    """``python -m cdap.judge.sandbox <file>`` — print the guard's verdict on one file.

    A tiny convenience for authoring samples: it answers "would this trip the guard?"
    without spinning up the whole judge.
    """
    import sys

    from .. import capabilities

    capabilities.enable_utf8_output()
    if len(sys.argv) != 2:
        print("usage: python -m cdap.judge.sandbox <file.py>")
        return 2

    path = sys.argv[1]
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read()

    report = scan(source)
    if report.ok:
        print(f"[guard] {path}: clean (passes the static guard)")
        print("        note: passing the guard is NOT a safety guarantee — the container")
        print("        is the boundary. See docs/threat-model.md.")
        return 0

    print(f"[guard] {path}: REFUSED")
    for reason in report.violations:
        print(f"        - {reason}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
