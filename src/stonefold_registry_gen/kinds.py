"""Verb → kind heuristics for the registry generator.

These are DRAFTING heuristics, not policy: every guess is surfaced to the
reviewer as a ``TODO(review)`` marker in the emitted YAML. An unknown verb
drafts as ``effect`` — the most-gated kind — so an unrecognised capability is
over-governed until a human classifies it, never under-governed.
"""

from __future__ import annotations

import re

OBSERVE_VERBS = frozenset(
    {"get", "list", "read", "search", "find", "fetch", "query", "view", "show", "describe"}
)
ASSESS_VERBS = frozenset(
    {"assess", "classify", "score", "evaluate", "estimate", "triage", "rank", "grade"}
)
RECORD_VERBS = frozenset(
    {
        "create", "add", "update", "edit", "set", "delete", "remove", "insert",
        "write", "log", "record", "link", "unlink", "upsert", "patch", "tag",
    }
)
TRANSITION_VERBS = frozenset(
    {
        "approve", "reject", "sign", "submit", "confirm", "cancel", "close",
        "reopen", "activate", "deactivate", "archive", "complete", "finalize",
        "discharge", "promote", "engage",
    }
)
EFFECT_VERBS = frozenset(
    {
        "send", "pay", "email", "notify", "publish", "post", "dispatch",
        "transfer", "charge", "refund", "deploy", "restart", "execute", "run",
        "trigger", "actuate", "start", "stop", "print", "export", "sync",
        "push", "wipe", "purge", "provision", "scale", "invoke", "call",
    }
)

# reversibility suggestions for effect-ish verbs — the reviewer confirms
_IRREVERSIBLE_VERBS = frozenset(
    {
        "send", "email", "pay", "notify", "publish", "post", "dispatch",
        "transfer", "charge", "print", "export", "wipe", "purge", "execute",
        "trigger", "delete",
    }
)
_COMPENSABLE_VERBS = frozenset({"refund"})

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SEPARATORS = re.compile(r"[_\-\s./]+")


def split_words(name: str) -> list[str]:
    """Split a snake/kebab/camel-case identifier into its words."""
    words: list[str] = []
    for part in _SEPARATORS.split(name):
        if part:
            words.extend(w for w in _CAMEL_BOUNDARY.split(part) if w)
    return words


def singular(word: str) -> str:
    """Naive English singularisation — good enough for a reviewed draft."""
    lower = word.lower()
    if lower.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    if lower.endswith(("ses", "xes", "zes", "ches", "shes")):
        return word[:-2]
    if lower.endswith("s") and not lower.endswith("ss"):
        return word[:-1]
    return word


def pascal(words: list[str]) -> str:
    """PascalCase an entity name from words, preserving inner capitals."""
    return "".join(w[:1].upper() + w[1:] for w in words if w)


def entity_from_words(words: list[str], fallback: str = "Misc") -> str:
    """Entity name from the non-verb words of a tool/action name."""
    if not words:
        return fallback
    parts = list(words[:-1]) + [singular(words[-1])]
    return pascal(parts)


def guess_kind(verb: str) -> tuple[str, bool]:
    """Return ``(kind, certain)`` for a verb. Unknown ⇒ ``("effect", False)``."""
    v = verb.lower()
    if v in OBSERVE_VERBS:
        return ("observe", True)
    if v in ASSESS_VERBS:
        return ("assess", True)
    if v in RECORD_VERBS:
        return ("record", True)
    if v in TRANSITION_VERBS:
        return ("transition", True)
    if v in EFFECT_VERBS:
        return ("effect", True)
    return ("effect", False)


def suggest_reversibility(verb: str) -> str | None:
    """Suggested ``reversibility`` for the reviewer, or ``None`` (leave default)."""
    v = verb.lower()
    if v in _IRREVERSIBLE_VERBS:
        return "irreversible"
    if v in _COMPENSABLE_VERBS:
        return "compensable"
    return None
