"""SQL DDL → draft registry entities.

Parses ``CREATE TABLE`` statements from a DDL text (a small, paren-balanced
scanner — not a full SQL parser; it targets the common Postgres-ish dialect of
schema dumps). Tables become entities with typed properties; ``*_id`` columns
get reference/scope-key hints for the reviewer.
"""

from __future__ import annotations

import re

from acp_registry_gen.kinds import pascal, singular, split_words
from acp_registry_gen.model import DraftEntity, DraftProperty, DraftRegistry

_CREATE_TABLE = re.compile(
    r"create\s+table\s+(?:if\s+not\s+exists\s+)?(?P<name>\"?[\w.]+\"?)\s*\(",
    re.IGNORECASE,
)
_CONSTRAINT_STARTS = (
    "primary", "foreign", "constraint", "unique", "check", "key", "index", "exclude",
)
_SCOPE_KEY_COLUMNS = frozenset({"tenant_id", "owner_id", "user_id", "org_id", "client_id"})


def _map_sql_type(sql_type: str) -> str:
    t = sql_type.lower()
    if t.startswith(("bool",)):
        return "boolean"
    if t.startswith(("int", "bigint", "smallint", "serial", "bigserial", "smallserial")):
        return "int"
    if t.startswith(("numeric", "decimal", "money", "real", "double", "float")):
        return "decimal"
    if t.startswith(("timestamp", "date", "time")):
        return "dateTime"
    return "string"


def _table_block(ddl: str, open_paren: int) -> str:
    """Return the balanced-paren column block starting at ``open_paren``."""
    depth = 0
    for i in range(open_paren, len(ddl)):
        if ddl[i] == "(":
            depth += 1
        elif ddl[i] == ")":
            depth -= 1
            if depth == 0:
                return ddl[open_paren + 1 : i]
    return ddl[open_paren + 1 :]


def _split_columns(block: str) -> list[str]:
    """Split the column block on top-level commas."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in block:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _entity_name(raw: str) -> str:
    name = raw.strip('"').rsplit(".", 1)[-1]
    words = split_words(name)
    if not words:
        return "Misc"
    return pascal(list(words[:-1]) + [singular(words[-1])])


def _column_property(line: str) -> DraftProperty | None:
    tokens = line.split()
    if not tokens or tokens[0].lower().startswith(_CONSTRAINT_STARTS):
        return None
    name = tokens[0].strip('"')
    sql_type = tokens[1].split("(", 1)[0] if len(tokens) > 1 else "text"
    rest = " ".join(tokens[2:]).lower()
    required = "not null" in rest or "primary key" in rest
    hint: str | None = None
    scope_key = name.lower() in _SCOPE_KEY_COLUMNS
    if scope_key:
        hint = "possible scope key -- consider a scope predicate over this column (docs/06 sec. 5)"
    elif name.lower().endswith("_id") and name.lower() != "id":
        referenced = pascal([singular(w) for w in split_words(name[:-3])])
        hint = f"reference -- consider `type: {referenced}` (entity reference)"
    return DraftProperty(
        name=name, type=_map_sql_type(sql_type), required=required, hint=hint, scope_key=scope_key
    )


def draft_from_sql(ddl: str, *, domain: str) -> DraftRegistry:
    """Draft a registry from ``CREATE TABLE`` DDL text."""
    draft = DraftRegistry(domain=domain, source="sql")
    for match in _CREATE_TABLE.finditer(ddl):
        block = _table_block(ddl, match.end() - 1)
        entity = DraftEntity(name=_entity_name(match.group("name")))
        for line in _split_columns(block):
            prop = _column_property(line)
            if prop is not None:
                entity.properties.append(prop)
        entity.hint = "observe/record are implicit per entity (docs/06 sec. 4); declare effects/transitions by hand"
        draft.entities.append(entity)
    return draft
