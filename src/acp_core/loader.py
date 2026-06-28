"""Policy loading: raw mapping → validated, compiled, ready-to-run policy.

Pipeline (RFC §13, design §4): merge ``extends`` fragments → JSON-Schema
structural validation → pydantic parse → semantic linter → compile. Any
ERROR-severity lint finding raises ``PolicyError`` so the gateway refuses to
start (never falls back to a permissive default).

Kept I/O-free (the trust kernel stays pure): callers read the YAML/JSON files
and pass already-parsed mappings, plus the JSON Schema mapping.
"""

from __future__ import annotations

from typing import Any

import jsonschema

from acp_core.compiler import CompiledPolicy
from acp_core.linter import LintReport, PolicyError, lint
from acp_core.policy import Policy
from acp_core.registry import InMemoryRegistry


class SchemaError(Exception):
    """Raised when a policy fails JSON-Schema structural validation."""


def _merge_chain(docs: list[dict[str, Any]]) -> dict[str, Any]:
    """Union allow/deny/gates/scope/standing across a fragment chain
    (RFC §3.2). Later docs (the governed document) are applied last; deny is
    unioned so a fragment's deny can never be widened (deny wins at authorize
    time). ACP-AMBIGUITY: RFC §3.2 says "more-restrictive gate wins"; for this
    PoC a later doc's gate config overrides an earlier one on the same gate."""
    out: dict[str, Any] = {}
    allow: list[Any] = []
    deny: list[Any] = []
    standing: list[Any] = []
    for d in docs:
        for key, value in d.items():
            if key == "allow":
                allow.extend(value)
            elif key == "deny":
                deny.extend(value or [])
            elif key == "standing":
                standing.extend(value or [])
            elif key == "scope":
                out.setdefault("scope", {}).update(value)
            elif key == "gates":
                gates = out.setdefault("gates", {})
                for gkey, gset in value.items():
                    gates.setdefault(gkey, {}).update(gset)
            elif key == "extends":
                continue  # consumed by merge; not part of the flattened policy
            else:
                out[key] = value
    out["allow"] = allow
    if deny:
        out["deny"] = deny
    if standing:
        out["standing"] = standing
    return out


def merge_extends(
    data: dict[str, Any], fragments: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Flatten ``extends`` fragments (one level) then this document last."""
    chain: list[dict[str, Any]] = []
    for name in data.get("extends", ()):  # fragments applied in declared order
        if name not in fragments:
            raise SchemaError(f"extends references unknown fragment {name!r}")
        chain.append(fragments[name])
    chain.append(data)
    return _merge_chain(chain)


def load_policy(
    data: dict[str, Any],
    registry: InMemoryRegistry,
    *,
    schema: dict[str, Any] | None = None,
    fragments: dict[str, dict[str, Any]] | None = None,
) -> CompiledPolicy:
    """Validate, lint, and compile one policy. Raises ``SchemaError`` /
    ``PolicyError`` on failure. Lint *warnings* are attached to the returned
    ``CompiledPolicy`` (``.lint_report``)."""
    merged = merge_extends(data, fragments or {})

    if schema is not None:
        try:
            jsonschema.validate(merged, schema)
        except jsonschema.ValidationError as exc:
            raise SchemaError(str(exc.message)) from exc

    policy = Policy.model_validate(merged)
    report = lint(policy, registry)
    if report.has_errors:
        raise PolicyError(report)

    compiled = CompiledPolicy(policy)
    compiled.lint_report = report
    return compiled


def validate_only(
    data: dict[str, Any],
    registry: InMemoryRegistry,
    *,
    schema: dict[str, Any] | None = None,
    fragments: dict[str, dict[str, Any]] | None = None,
) -> LintReport:
    """Run schema + lint without compiling; returns the full report (errors and
    warnings). Used by tooling and the A4/A5 acceptance tests."""
    merged = merge_extends(data, fragments or {})
    if schema is not None:
        try:
            jsonschema.validate(merged, schema)
        except jsonschema.ValidationError as exc:
            raise SchemaError(str(exc.message)) from exc
    policy = Policy.model_validate(merged)
    return lint(policy, registry)
