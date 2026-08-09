# SPDX-License-Identifier: Apache-2.0
"""Server-side tool retrieval for the MCP surface (design §1.2).

**Why a gateway needs this.** A registry that covers a real estate declares
hundreds of actions, and an agent asked to choose among hundreds of tools
chooses worse — not because the list is long, but because a long list is likely
to contain two names that plausibly fit the same sentence. Given
``Disbursement.suspend`` and ``Invoicing.hold`` in the same context, a small
model asked to stop a payment reaches for either.

Some runtimes solve this at the model provider, which retrieves a short
candidate list before the model sees anything. Runtimes without that facility
have only what the gateway offers, and a gateway whose only answer is *here are
all five hundred schemas* has handed them the harder problem. So retrieval
belongs here too, as an endpoint any client can use.

**Why it is lexical and not learned.** No model, no embeddings, no index:
scoring is a match over the action's own name, label and declared field names,
which is all the registry has. A reviewer can read *why* a tool was returned,
which a vector index does not give and which matters here, because the thing
being retrieved decides what an agent is about to attempt.

**What it does not do.** Retrieval is not enforcement and gives no guarantee.
It changes how many schemas a model chooses between; the gate decides whether
the chosen action is allowed. Two limits are worth knowing before relying on it:

* On genuinely confusable names the winning margin can be narrow — a few
  hundredths, at several hundred tools — so a top hit is a short list of one,
  not a resolved ambiguity.
* Matching is lexical, so a request that *paraphrases* rather than shares
  vocabulary can rank a look-alike first: "stop a payment going out" hits an
  action described as "the outbound bank payment" before one described as
  "suspend a pending payment". The right action is still in the list, which is
  why this returns candidates and not an answer.

Neither is a reason to filter the catalogue by policy instead: that would move
an enforcement decision into a ranking function. Nothing here can be relied on
to keep an agent away from an action — that is what policy is for.
"""

from __future__ import annotations

import re
from typing import Any


def _terms(text: str) -> list[str]:
    """Split on non-letters and on camelCase, lowercased.

    ``updateBankAccount`` becomes {update, bank, account}, so "change supplier
    bank details" reaches it without anyone maintaining a synonym list.
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text or "")
    return [term for term in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if term]


def _tool_terms(tool: dict[str, Any]) -> set[str]:
    """Every term a tool can be matched on: its name, description, and the
    names and descriptions of its declared fields."""
    parts = [str(tool.get("name", "")), str(tool.get("description", ""))]
    schema = tool.get("input_schema") or {}
    for field, spec in (schema.get("properties") or {}).items():
        parts.append(str(field))
        if isinstance(spec, dict) and spec.get("description"):
            parts.append(str(spec["description"]))
    return set(_terms(" ".join(parts)))


def score(tool: dict[str, Any], query: str) -> float:
    """How well one tool answers one query.

    A term matching the action's own *name* counts for more than one matching
    its prose, because confusable actions are confusable by name — two
    plausible names, not two plausible sentences.
    """
    wanted = _terms(query)
    if not wanted:
        return 0.0
    have = _tool_terms(tool)
    name_terms = set(_terms(str(tool.get("name", ""))))

    hits = sum(1 for term in wanted if term in have)
    if not hits:
        return 0.0
    name_hits = sum(1 for term in wanted if term in name_terms)
    return (hits / len(wanted)) + 0.5 * (name_hits / len(wanted))


def search(
    tools: list[dict[str, Any]], query: str, limit: int = 5
) -> list[dict[str, Any]]:
    """The short candidate list a model should see instead of the whole surface.

    Each row carries the score and the terms that matched, so a retrieval can be
    explained after the fact. An empty result is returned as an empty result:
    falling back to the whole registry would restore the surface this exists to
    avoid, at the moment the agent is least sure what it wants.
    """
    ranked: list[dict[str, Any]] = []
    for tool in tools:
        value = score(tool, query)
        if value <= 0:
            continue
        matched = sorted(set(_terms(query)) & _tool_terms(tool))
        ranked.append({"tool": tool, "score": round(value, 4), "matched": matched})
    ranked.sort(key=lambda row: (-float(row["score"]), str(row["tool"].get("name", ""))))
    return ranked[:limit]


def search_response(
    tools: list[dict[str, Any]], query: str, limit: int = 5
) -> dict[str, Any]:
    """The ``GET /mcp/search`` body: the candidates, and how many there were.

    ``of`` is the size of the full surface, so a client can see what it was
    spared and a reviewer can see that retrieval ran against everything.
    """
    return {
        "query": query,
        "of": len(tools),
        "results": [
            {
                "name": row["tool"].get("name"),
                "description": row["tool"].get("description"),
                "input_schema": row["tool"].get("input_schema"),
                "score": row["score"],
                "matched": row["matched"],
            }
            for row in search(tools, query, limit=limit)
        ],
    }
