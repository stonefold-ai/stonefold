# SPDX-License-Identifier: Apache-2.0
"""OpenAPI-spec and MCP-tool-list → draft registry actions.

Both importers share the same shape: derive a verb + entity from what the
source names things, guess the kind from the verb table (``kinds.py``), map
JSON-Schema parameter types, and mark everything for review. ``observe``-kind
sources produce only the entity — reads are implicit per entity (docs/06 §4),
so drafting a named ``read`` action would just be noise.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from stonefold_registry_gen.kinds import (
    entity_from_words,
    guess_kind,
    pascal,
    singular,
    split_words,
    suggest_reversibility,
)
from stonefold_registry_gen.model import DraftAction, DraftProperty, DraftRegistry


def _map_json_type(schema: Mapping[str, Any]) -> str:
    t = schema.get("type")
    if t == "integer":
        return "int"
    if t == "number":
        return "decimal"
    if t == "boolean":
        return "boolean"
    if t == "string" and schema.get("format") in ("date-time", "date", "time"):
        return "dateTime"
    return "string"


def _data_from_json_schema(schema: Mapping[str, Any]) -> list[DraftProperty]:
    required = set(schema.get("required") or [])
    props: Mapping[str, Any] = schema.get("properties") or {}
    out: list[DraftProperty] = []
    for name, prop_schema in props.items():
        if not isinstance(prop_schema, Mapping):
            prop_schema = {}
        out.append(
            DraftProperty(
                name=str(name),
                type=_map_json_type(prop_schema),
                required=str(name) in required,
            )
        )
    return out


def _add_action(
    draft: DraftRegistry,
    *,
    entity: str,
    action_name: str,
    verb: str,
    data: list[DraftProperty],
    kind_override: str | None = None,
) -> None:
    if kind_override is not None:
        kind, certain = kind_override, True
    else:
        kind, certain = guess_kind(verb)
    ent = draft.entity(entity)
    if kind == "observe":
        # reads are implicit per entity — the entity's existence is the grant
        ent.hint = ent.hint or f"observed via {action_name!r} (implicit read; no declared action needed)"
        return
    ent.actions.append(
        DraftAction(
            name=action_name,
            kind=kind,
            certain=certain,
            verb=verb,
            data=data,
            suggested_reversibility=suggest_reversibility(verb) if kind == "effect" else None,
        )
    )


# --------------------------------------------------------------------------
# MCP tool list
# --------------------------------------------------------------------------
def draft_from_mcp_tools(
    tools: Sequence[Mapping[str, Any]] | Mapping[str, Any], *, domain: str
) -> DraftRegistry:
    """Draft a registry from an MCP tool list.

    Accepts either the bare list of tool objects or the ``{"tools": [...]}``
    wrapper a ``tools/list`` response uses. Each tool name is split into
    verb + entity (``send_email`` → effect on ``Email``).
    """
    if isinstance(tools, Mapping):
        tool_list = list(tools.get("tools") or [])
    else:
        tool_list = list(tools)
    draft = DraftRegistry(domain=domain, source="mcp")
    for tool in tool_list:
        name = str(tool.get("name") or "")
        if not name:
            continue
        words = split_words(name)
        verb = words[0].lower() if words else name.lower()
        entity = entity_from_words(words[1:], fallback=pascal([singular(w) for w in words]) or "Misc")
        schema = tool.get("inputSchema")
        data = _data_from_json_schema(schema) if isinstance(schema, Mapping) else []
        _add_action(draft, entity=entity, action_name=name, verb=verb, data=data)
    return draft


# --------------------------------------------------------------------------
# OpenAPI
# --------------------------------------------------------------------------
_METHOD_KINDS = {"get": "observe", "put": "record", "patch": "record", "delete": "record"}


def _entity_from_path(path: str) -> str:
    segments = [s for s in path.split("/") if s and not s.startswith("{")]
    if not segments:
        return "Misc"
    words = split_words(segments[-1])
    return entity_from_words(words, fallback="Misc") if words else "Misc"


def _request_body_schema(op: Mapping[str, Any]) -> Mapping[str, Any]:
    body = op.get("requestBody")
    if not isinstance(body, Mapping):
        return {}
    content = body.get("content")
    if not isinstance(content, Mapping):
        return {}
    for mime, media in content.items():
        if isinstance(media, Mapping) and "json" in str(mime):
            schema = media.get("schema")
            if isinstance(schema, Mapping):
                return schema
    return {}


def draft_from_openapi(spec: Mapping[str, Any], *, domain: str) -> DraftRegistry:
    """Draft a registry from a parsed OpenAPI document.

    GET → the entity only (implicit read). PUT/PATCH/DELETE → ``record``
    actions. POST → kind guessed from the operationId's verb (``payInvoice`` →
    effect), defaulting to an uncertain ``effect``.
    """
    draft = DraftRegistry(domain=domain, source="openapi")
    paths = spec.get("paths")
    if not isinstance(paths, Mapping):
        return draft
    for path, ops in paths.items():
        if not isinstance(ops, Mapping):
            continue
        entity = _entity_from_path(str(path))
        for method, op in ops.items():
            m = str(method).lower()
            if m not in ("get", "post", "put", "patch", "delete") or not isinstance(op, Mapping):
                continue
            op_id = str(op.get("operationId") or f"{m}{entity}")
            words = split_words(op_id)
            verb = words[0].lower() if words else m
            data = _data_from_json_schema(_request_body_schema(op))
            _add_action(
                draft,
                entity=entity,
                action_name=op_id,
                verb=verb,
                data=data,
                kind_override=_METHOD_KINDS.get(m),
            )
    return draft
