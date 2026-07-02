"""Handler-stub generation (registry_gen extension, plan G1).

The generator drafts connector / scope-predicate / precondition-check / content-hook
*code* from the same inputs it drafts a registry from. Contract under test:

1. a SQL/OpenAPI draft yields a CRUD/HTTP connector stub + a scope-predicate stub per
   scope-key column;
2. an authoring registry yields a stub per *declared* name (docs/06 §5-6);
3. emitted stubs are valid, importable Python (`validate_stub_code`; we also exec it);
4. every stub is over-governed — it raises NotImplementedError until implemented, so it
   can never be silently trusted (the same discipline as the registry drafts).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from acp_registry_gen import (
    draft_from_sql,
    emit_stubs,
    plan_from_draft,
    plan_from_registry,
    validate_stub_code,
)
from acp_registry_gen.__main__ import main
from acp_registry_gen.stubs import _ident

_ROOT = Path(__file__).resolve().parents[1]
PAYMENTS_REGISTRY = _ROOT / "examples" / "payments.registry.yaml"

_DDL = """
CREATE TABLE accounts (
    id text PRIMARY KEY, tenant_id text NOT NULL, balance numeric NOT NULL
);
CREATE TABLE tickets (id text PRIMARY KEY, owner_id text, subject text);
"""


def _exec_module(code: str) -> dict[str, Any]:
    ns: dict[str, Any] = {}
    exec(compile(code, "<generated-stubs>", "exec"), ns)  # noqa: S102 - our own emitter
    return ns


# --- from a SQL draft (connector + scope predicates) ----------------------
def test_sql_draft_plans_connector_and_scope_predicates() -> None:
    plan = plan_from_draft(draft_from_sql(_DDL, domain="payments"))
    assert len(plan.connectors) == 1
    conn = plan.connectors[0]
    assert conn.type == "sql" and set(conn.entities) == {"Account", "Ticket"}
    names = {n for n, _ in plan.scope_predicates}
    assert names == {"tenantOf", "assignedToCurrentUser"}


def test_sql_stubs_are_valid_and_fail_closed() -> None:
    code = emit_stubs(plan_from_draft(draft_from_sql(_DDL, domain="payments")))
    assert validate_stub_code(code) == []
    assert "TODO(review)" in code and "raise NotImplementedError" in code
    assert code.isascii()  # generated text is pure ASCII (cp1252 consoles)

    ns = _exec_module(code)
    connector = ns["PaymentsSqlConnector"]()
    # over-governed: calling an un-implemented handler raises (⇒ fail closed).
    with pytest.raises(NotImplementedError):
        connector.execute(None, None, None)
    with pytest.raises(NotImplementedError):
        connector.dispatch(None, None, "key")
    scope = ns["TenantOfScope"]()
    assert scope.name == "tenantOf"
    with pytest.raises(NotImplementedError):
        scope.matches({}, None)


def test_generated_connector_matches_the_protocol_shape() -> None:
    code = emit_stubs(plan_from_draft(draft_from_sql(_DDL, domain="payments")))
    connector = _exec_module(code)["PaymentsSqlConnector"]()
    for method in ("execute", "dispatch", "fetch_target", "cancel"):
        assert callable(getattr(connector, method))


# --- from an existing authoring registry (declared names) -----------------
def test_registry_plan_covers_every_declared_name() -> None:
    doc = yaml.safe_load(PAYMENTS_REGISTRY.read_text(encoding="utf-8"))
    plan = plan_from_registry(doc)
    connector_names = {c.name for c in plan.connectors}
    assert connector_names == set(doc["connectors"])
    assert set(n for n, _ in plan.scope_predicates) == set(doc.get("scopePredicates") or [])
    assert set(plan.precondition_checks) == set(doc.get("preconditionChecks") or [])


def test_registry_stubs_emit_precondition_and_hook_functions() -> None:
    doc = yaml.safe_load(PAYMENTS_REGISTRY.read_text(encoding="utf-8"))
    code = emit_stubs(plan_from_registry(doc))
    assert validate_stub_code(code) == []
    ns = _exec_module(code)
    # every declared precondition check became a fail-closed function
    for name in doc.get("preconditionChecks") or []:
        fn = ns[_ident(name)]
        with pytest.raises(NotImplementedError):
            fn(None)


def test_dotted_names_become_valid_identifiers() -> None:
    assert _ident("dlp.basic") == "dlp_basic"
    assert _ident("9lives") == "_9lives"
    # a hook named with a dot still produces a callable stub
    doc = {"domain": "d", "entities": {}, "hooks": ["dlp.basic"]}
    ns = _exec_module(emit_stubs(plan_from_registry(doc)))
    assert callable(ns["dlp_basic"])


# --- the CLI wires both paths ---------------------------------------------
def test_cli_sql_with_stubs_writes_both(tmp_path: Path) -> None:
    ddl = tmp_path / "schema.sql"
    ddl.write_text(_DDL, encoding="utf-8")
    registry_out = tmp_path / "draft.registry.yaml"
    stubs_out = tmp_path / "handlers.py"
    rc = main(["sql", str(ddl), "--domain", "pay", "-o", str(registry_out), "--stubs", str(stubs_out)])
    assert rc == 0
    assert registry_out.exists() and stubs_out.exists()
    assert validate_stub_code(stubs_out.read_text(encoding="utf-8")) == []


def test_cli_stubs_from_registry(tmp_path: Path) -> None:
    out = tmp_path / "handlers.py"
    rc = main(["stubs", str(PAYMENTS_REGISTRY), "-o", str(out)])
    assert rc == 0 and out.exists()
    assert "raise NotImplementedError" in out.read_text(encoding="utf-8")
