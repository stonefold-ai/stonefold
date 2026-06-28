"""The condition expression language (RFC §8, design §10).

A *small, frozen* boolean language used by gate ``when:`` clauses. This module
provides the tokenizer, a recursive-descent parser producing an immutable AST,
and a static validator for RFC §13.9 (paths reference only known namespaces;
functions are from the fixed set). The tree-walk **evaluator** is added in M2;
keeping parse/validate here lets the M1 linter check every condition at load.

There is **no ``eval``/``exec``/``compile``** of policy expressions anywhere —
ever (CLAUDE.md). The grammar (RFC §8 EBNF)::

    condition  := orExpr
    orExpr     := andExpr ("or" andExpr)*
    andExpr    := unary ("and" unary)*
    unary      := "not" unary | comparison | "(" condition ")"
    comparison := operand op operand
                | operand ("in" | "not in") list
                | "exists" path
    op         := "==" | "!=" | "<" | "<=" | ">" | ">=" | "matches"
    operand    := path | literal | function
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, Union

# --- namespaces & functions frozen by RFC §8 ---
NAMESPACES = frozenset({"action", "data", "resource", "actor", "context"})
FUNCTIONS = frozenset({"count", "now", "window", "spend"})

_KEYWORDS = frozenset(
    {"and", "or", "not", "in", "exists", "matches", "true", "false"}
)
_COMPARE_OPS = frozenset({"==", "!=", "<", "<=", ">", ">=", "matches"})


class ConditionError(ValueError):
    """A condition that fails to tokenize, parse, or validate (RFC §13.9)."""


# --------------------------------------------------------------------------
# AST nodes (immutable)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Path:
    """A dotted reference, e.g. ``action.reversibility``. A single segment whose
    root is not a known namespace is an *implicit string literal* (RFC examples
    write ``== restricted`` / ``== irreversible`` unquoted)."""

    parts: tuple[str, ...]


@dataclass(frozen=True)
class Literal:
    """A string, number, boolean, or list literal."""

    value: Union[str, float, int, bool, tuple["Literal", ...]]


@dataclass(frozen=True)
class Func:
    name: str
    args: tuple["Operand", ...]


Operand = Union[Path, Literal, Func]


@dataclass(frozen=True)
class Compare:
    left: "Operand"
    op: str
    right: "Operand"


@dataclass(frozen=True)
class InExpr:
    left: "Operand"
    right: Union[Literal, "Operand"]
    negated: bool


@dataclass(frozen=True)
class Exists:
    path: Path


@dataclass(frozen=True)
class And:
    left: "Expr"
    right: "Expr"


@dataclass(frozen=True)
class Or:
    left: "Expr"
    right: "Expr"


@dataclass(frozen=True)
class Not:
    expr: "Expr"


Expr = Union[And, Or, Not, Compare, InExpr, Exists]


# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Token:
    type: str  # op|lparen|rparen|lbrack|rbrack|comma|string|number|duration|bool|ident|kw|eof
    value: str


_SINGLE = {
    "(": "lparen",
    ")": "rparen",
    "[": "lbrack",
    "]": "rbrack",
    ",": "comma",
    ".": "dot",
}


def tokenize(src: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        if c.isspace():
            i += 1
            continue
        if c in _SINGLE:
            tokens.append(Token(_SINGLE[c], c))
            i += 1
            continue
        if c in "=!<>":
            # two-char ops first
            two = src[i : i + 2]
            if two in {"==", "!=", "<=", ">="}:
                tokens.append(Token("op", two))
                i += 2
                continue
            if c in "<>":
                tokens.append(Token("op", c))
                i += 1
                continue
            raise ConditionError(f"unexpected character {c!r} at {i}")
        if c in "'\"":
            j = i + 1
            buf: list[str] = []
            while j < n and src[j] != c:
                buf.append(src[j])
                j += 1
            if j >= n:
                raise ConditionError("unterminated string literal")
            tokens.append(Token("string", "".join(buf)))
            i = j + 1
            continue
        if c.isdigit() or (c == "-" and i + 1 < n and src[i + 1].isdigit()):
            j = i + 1 if c == "-" else i
            while j < n and (src[j].isdigit() or src[j] == "."):
                j += 1
            num = src[i:j]
            # duration suffix: a single s/m/h/d not followed by another ident char
            if j < n and src[j] in "smhd" and not (
                j + 1 < n and (src[j + 1].isalnum() or src[j + 1] == "_")
            ):
                tokens.append(Token("duration", num + src[j]))
                i = j + 1
            else:
                tokens.append(Token("number", num))
                i = j
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            word = src[i:j]
            if word in ("true", "false"):
                tokens.append(Token("bool", word))
            elif word in _KEYWORDS:
                tokens.append(Token("kw", word))
            else:
                tokens.append(Token("ident", word))
            i = j
            continue
        raise ConditionError(f"unexpected character {c!r} at {i}")
    tokens.append(Token("eof", ""))
    return tokens


# --------------------------------------------------------------------------
# Parser (recursive descent)
# --------------------------------------------------------------------------
class _Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self._toks = tokens
        self._pos = 0

    def _peek(self) -> Token:
        return self._toks[self._pos]

    def _advance(self) -> Token:
        tok = self._toks[self._pos]
        self._pos += 1
        return tok

    def _expect(self, type_: str, value: str | None = None) -> Token:
        tok = self._peek()
        if tok.type != type_ or (value is not None and tok.value != value):
            raise ConditionError(
                f"expected {value or type_!r}, got {tok.value or tok.type!r}"
            )
        return self._advance()

    def _is_kw(self, value: str) -> bool:
        tok = self._peek()
        return tok.type == "kw" and tok.value == value

    def parse(self) -> Expr:
        expr = self._or()
        if self._peek().type != "eof":
            raise ConditionError(f"trailing tokens at {self._peek().value!r}")
        return expr

    def _or(self) -> Expr:
        left = self._and()
        while self._is_kw("or"):
            self._advance()
            left = Or(left, self._and())
        return left

    def _and(self) -> Expr:
        left = self._unary()
        while self._is_kw("and"):
            self._advance()
            left = And(left, self._unary())
        return left

    def _unary(self) -> Expr:
        if self._is_kw("not"):
            self._advance()
            return Not(self._unary())
        if self._peek().type == "lparen":
            self._advance()
            inner = self._or()
            self._expect("rparen")
            return inner
        return self._comparison()

    def _comparison(self) -> Expr:
        if self._is_kw("exists"):
            self._advance()
            return Exists(self._path())
        left = self._operand()
        if self._is_kw("in"):
            self._advance()
            return InExpr(left, self._rhs_collection(), negated=False)
        if self._is_kw("not"):
            self._advance()
            self._expect("kw", "in")
            return InExpr(left, self._rhs_collection(), negated=True)
        tok = self._peek()
        if tok.type == "op" or (tok.type == "kw" and tok.value == "matches"):
            self._advance()
            right = self._operand()
            return Compare(left, tok.value, right)
        raise ConditionError(
            f"expected comparison operator after operand, got {tok.value or tok.type!r}"
        )

    def _rhs_collection(self) -> Union[Literal, Operand]:
        # RFC grammar says `in` takes a list; examples also use `in window(...)`.
        # ACP-AMBIGUITY (RFC §8 vs §14.3): we accept a list literal OR an
        # operand (e.g. a function) on the right of `in`.
        if self._peek().type == "lbrack":
            return self._list()
        return self._operand()

    def _operand(self) -> Operand:
        tok = self._peek()
        if tok.type == "string":
            self._advance()
            return Literal(tok.value)
        if tok.type == "number":
            self._advance()
            return Literal(float(tok.value) if "." in tok.value else int(tok.value))
        if tok.type == "duration":
            self._advance()
            return Literal(tok.value)
        if tok.type == "bool":
            self._advance()
            return Literal(tok.value == "true")
        if tok.type == "lbrack":
            return self._list()
        if tok.type == "ident":
            name = self._advance().value
            if self._peek().type == "lparen":
                return self._func(name)
            return self._path(first=name)
        raise ConditionError(f"unexpected token {tok.value or tok.type!r}")

    def _func(self, name: str) -> Func:
        self._expect("lparen")
        args: list[Operand] = []
        if self._peek().type != "rparen":
            args.append(self._operand())
            while self._peek().type == "comma":
                self._advance()
                args.append(self._operand())
        self._expect("rparen")
        return Func(name, tuple(args))

    def _path(self, first: str | None = None) -> Path:
        parts: list[str] = []
        parts.append(first if first is not None else self._expect("ident").value)
        # consume ('.' ident)*
        while self._peek().type == "dot":
            self._advance()
            parts.append(self._expect("ident").value)
        return Path(tuple(parts))

    def _list(self) -> Literal:
        self._expect("lbrack")
        items: list[Literal] = []
        if self._peek().type != "rbrack":
            items.append(self._literal_item())
            while self._peek().type == "comma":
                self._advance()
                items.append(self._literal_item())
        self._expect("rbrack")
        return Literal(tuple(items))

    def _literal_item(self) -> Literal:
        operand = self._operand()
        if not isinstance(operand, Literal):
            # A bare ident inside a list (e.g. [careTeam]) is an implicit string.
            if isinstance(operand, Path) and len(operand.parts) == 1:
                return Literal(operand.parts[0])
            raise ConditionError("list items must be literals")
        return operand


def parse(src: str) -> Expr:
    """Parse a condition string into an AST, or raise ``ConditionError``."""
    return _Parser(tokenize(src)).parse()


# --------------------------------------------------------------------------
# Static validation (RFC §13.9)
# --------------------------------------------------------------------------
def _check_operand(op: "Operand", problems: list[str]) -> None:
    if isinstance(op, Path):
        # A single-segment non-namespace ident is an implicit string literal
        # (RFC examples: `== restricted`). Multi-segment paths MUST root in a
        # known namespace.
        if len(op.parts) > 1 and op.parts[0] not in NAMESPACES:
            problems.append(
                f"path {'.'.join(op.parts)!r} does not start with a known "
                f"namespace {sorted(NAMESPACES)}"
            )
    elif isinstance(op, Func):
        if op.name not in FUNCTIONS:
            problems.append(
                f"unknown function {op.name!r}; allowed: {sorted(FUNCTIONS)}"
            )
        for arg in op.args:
            _check_operand(arg, problems)
    # Literal: nothing to check (lists hold literals only by construction)


def _walk(node: object, problems: list[str]) -> None:
    if isinstance(node, (And, Or)):
        _walk(node.left, problems)
        _walk(node.right, problems)
    elif isinstance(node, Not):
        _walk(node.expr, problems)
    elif isinstance(node, Compare):
        _check_operand(node.left, problems)
        _check_operand(node.right, problems)
    elif isinstance(node, InExpr):
        _check_operand(node.left, problems)
        if not isinstance(node.right, Literal):
            _check_operand(node.right, problems)
    elif isinstance(node, Exists):
        _check_operand(node.path, problems)


def validate(expr: Expr) -> list[str]:
    """Return a list of RFC §13.9 problems (unknown namespace roots, unknown
    functions). Empty list ⇒ the condition is structurally valid."""
    problems: list[str] = []
    _walk(expr, problems)
    return problems


def parse_and_validate(src: str) -> list[str]:
    """Parse ``src`` and return §13.9 problems. A parse failure is itself
    returned as a single problem (so the linter can report it rather than
    crash)."""
    try:
        expr = parse(src)
    except ConditionError as exc:
        return [f"cannot parse condition {src!r}: {exc}"]
    return validate(expr)


# --------------------------------------------------------------------------
# Runtime evaluation (M2, design §10) — a tree-walk interpreter over the AST.
#
# There is still **no ``eval``/``exec``** — this walks the immutable AST and
# resolves paths against a context the pipeline built. Per design §10, a
# *runtime resolution error* — a missing path, an uncomparable
# value — is **fail-closed for the gate**, never silently "false": it raises a
# ``ConditionRuntimeError`` that the gate engine turns into FAIL/DENY. ``exists``
# is the one construct that may probe a missing path without raising.
# --------------------------------------------------------------------------
class ConditionRuntimeError(ConditionError):
    """A condition that parses but cannot be *evaluated* against the runtime
    context. Gates treat this as fail-closed (design §10), distinct from the
    condition evaluating to ``False``."""


class MissingValueError(ConditionRuntimeError):
    """A referenced path is absent at runtime (≠ "evaluated false")."""


@dataclass(frozen=True)
class WindowRange:
    """The value of ``window("HH:MM-HH:MM")`` — a daily time interval (minutes
    since midnight). Supports midnight wrap (start > end)."""

    start: int
    end: int

    def __contains__(self, value: object) -> bool:
        minutes = _to_minutes(value)
        if minutes is None:
            return False
        if self.start <= self.end:
            return self.start <= minutes <= self.end
        return minutes >= self.start or minutes <= self.end


def _to_minutes(value: object) -> int | None:
    if isinstance(value, datetime):
        return value.hour * 60 + value.minute
    if isinstance(value, time):
        return value.hour * 60 + value.minute
    if isinstance(value, str) and ":" in value:
        hh, _, mm = value.partition(":")
        try:
            return int(hh) * 60 + int(mm)
        except ValueError:
            return None
    return None


def make_window(spec: object) -> WindowRange:
    """Host implementation of the ``window()`` function (RFC §8)."""
    if not isinstance(spec, str) or "-" not in spec:
        raise ConditionRuntimeError(f"window() expects 'HH:MM-HH:MM', got {spec!r}")
    lo, _, hi = spec.partition("-")
    s = _to_minutes(lo.strip())
    e = _to_minutes(hi.strip())
    if s is None or e is None:
        raise ConditionRuntimeError(f"window() bad spec {spec!r}")
    return WindowRange(s, e)


@dataclass(frozen=True)
class EvalContext:
    """The runtime context a condition resolves against.

    ``namespaces`` maps each frozen namespace (``action``/``data``/``resource``/
    ``actor``/``context``) to a mapping of its fields. ``functions`` supplies the
    four host functions (``now``/``window``/``count``/``spend``); absent ones
    resolve fail-closed.
    """

    namespaces: Mapping[str, Mapping[str, Any]]
    functions: Mapping[str, Callable[..., Any]] = field(default_factory=dict)

    def lookup(self, parts: tuple[str, ...]) -> Any:
        ns = parts[0]
        if ns not in self.namespaces:
            raise MissingValueError(f"unknown namespace {ns!r}")
        value: Any = self.namespaces[ns]
        for p in parts[1:]:
            if isinstance(value, Mapping) and p in value:
                value = value[p]
            else:
                raise MissingValueError(f"missing path {'.'.join(parts)!r}")
        return value

    def has(self, parts: tuple[str, ...]) -> bool:
        try:
            self.lookup(parts)
            return True
        except MissingValueError:
            return False

    def call(self, name: str, args: list[Any]) -> Any:
        fn = self.functions.get(name)
        if fn is None:
            raise MissingValueError(f"function {name!r} unavailable")
        return fn(*args)


def evaluate(expr: Expr, ctx: EvalContext) -> bool:
    """Evaluate a parsed condition to a boolean, or raise
    ``ConditionRuntimeError`` (fail-closed) if a value can't be resolved."""
    if isinstance(expr, And):
        return evaluate(expr.left, ctx) and evaluate(expr.right, ctx)
    if isinstance(expr, Or):
        return evaluate(expr.left, ctx) or evaluate(expr.right, ctx)
    if isinstance(expr, Not):
        return not evaluate(expr.expr, ctx)
    if isinstance(expr, Exists):
        return ctx.has(expr.path.parts)
    if isinstance(expr, InExpr):
        left = _resolve(expr.left, ctx)
        right = _resolve(expr.right, ctx)
        present = _contains(right, left)
        return (not present) if expr.negated else present
    if isinstance(expr, Compare):
        return _compare(_resolve(expr.left, ctx), expr.op, _resolve(expr.right, ctx))
    raise ConditionRuntimeError(f"cannot evaluate node {type(expr).__name__}")


def evaluate_str(src: str, ctx: EvalContext) -> bool:
    """Parse and evaluate ``src`` in one step (used by gates' ``when:``)."""
    return evaluate(parse(src), ctx)


def _resolve(op: Operand, ctx: EvalContext) -> Any:
    if isinstance(op, Literal):
        return _literal_value(op)
    if isinstance(op, Path):
        # A single-segment non-namespace ident is an implicit string literal
        # (RFC examples: ``== restricted``). Anything else is a real lookup.
        if len(op.parts) == 1 and op.parts[0] not in NAMESPACES:
            return op.parts[0]
        return ctx.lookup(op.parts)
    if isinstance(op, Func):
        return ctx.call(op.name, [_resolve(a, ctx) for a in op.args])
    raise ConditionRuntimeError(f"cannot resolve operand {op!r}")


def _literal_value(lit: Literal) -> Any:
    if isinstance(lit.value, tuple):
        return [_literal_value(x) for x in lit.value]
    return lit.value


def _as_number(v: Any) -> float | None:
    if isinstance(v, bool):  # bool is an int subclass — exclude it from numerics
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _eq(a: Any, b: Any) -> bool:
    na, nb = _as_number(a), _as_number(b)
    if na is not None and nb is not None:
        return na == nb
    return bool(a == b)


def _compare(left: Any, op: str, right: Any) -> bool:
    if op == "==":
        return _eq(left, right)
    if op == "!=":
        return not _eq(left, right)
    if op == "matches":
        return re.search(str(right), str(left)) is not None
    lo, ro = _as_number(left), _as_number(right)
    if lo is None or ro is None:
        raise ConditionRuntimeError(f"uncomparable: {left!r} {op} {right!r}")
    if op == "<":
        return lo < ro
    if op == "<=":
        return lo <= ro
    if op == ">":
        return lo > ro
    if op == ">=":
        return lo >= ro
    raise ConditionRuntimeError(f"unknown operator {op!r}")


def _contains(collection: Any, item: Any) -> bool:
    if isinstance(collection, WindowRange):
        return item in collection
    if isinstance(collection, (list, tuple, set, frozenset)):
        return any(_eq(item, c) for c in collection)
    if isinstance(collection, str):
        return str(item) in collection
    raise ConditionRuntimeError(f"{collection!r} is not a collection")
