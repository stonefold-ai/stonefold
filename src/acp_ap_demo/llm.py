"""Provider-neutral LLM tool-use seam (Anthropic default, OpenAI, fake-LLM).

The agent loop in ``agent`` is written once against this seam; each provider
translates the neutral messages/tools into its own SDK shape. The real SDKs are
imported **lazily** so the package (and the fast fake-LLM CI path) never needs
them installed. Model defaults are cheap — Claude Haiku / GPT-4o-mini — because
the demo only needs basic tool-calling, not reasoning.

Neutral message shapes (a plain list of dicts):
* ``{"role": "user", "content": str}``
* ``{"role": "assistant", "text": str, "tool_calls": list[ToolCall]}``
* ``{"role": "tool", "tool_call_id": str, "name": str, "content": str}``
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

# Cheap, tool-capable defaults (overridable via env).
ANTHROPIC_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class AssistantTurn:
    """One model turn: free text plus any tool calls it wants run."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMProvider(Protocol):
    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[ToolDef]
    ) -> AssistantTurn: ...

    @property
    def label(self) -> str: ...


# --------------------------------------------------------------------------- #
# Anthropic                                                                     #
# --------------------------------------------------------------------------- #
class AnthropicProvider:
    """Claude tool-use. The spec's default provider."""

    def __init__(self, *, model: str | None = None, api_key: str | None = None,
                 max_tokens: int = 1024) -> None:
        import anthropic  # lazy: only needed for the real path

        self._model = model or os.environ.get("ANTHROPIC_MODEL", ANTHROPIC_DEFAULT_MODEL)
        self._max_tokens = max_tokens
        self._client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))

    @property
    def label(self) -> str:
        return f"anthropic:{self._model}"

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[ToolDef]
    ) -> AssistantTurn:
        api_tools = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ]
        resp = self._client.messages.create(
            model=self._model, max_tokens=self._max_tokens, system=system,
            tools=api_tools, messages=_to_anthropic(messages),
        )
        turn = AssistantTurn()
        for block in resp.content:
            if block.type == "text":
                turn.text += block.text
            elif block.type == "tool_use":
                turn.tool_calls.append(
                    ToolCall(id=block.id, name=block.name, args=dict(block.input))
                )
        return turn


def _to_anthropic(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            content: list[dict[str, Any]] = []
            if m.get("text"):
                content.append({"type": "text", "text": m["text"]})
            for tc in m.get("tool_calls", []):
                content.append({"type": "tool_use", "id": tc.id, "name": tc.name,
                                "input": tc.args})
            out.append({"role": "assistant", "content": content})
        elif role == "tool":
            out.append({"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": m["tool_call_id"],
                "content": m["content"],
            }]})
    return out


# --------------------------------------------------------------------------- #
# OpenAI                                                                        #
# --------------------------------------------------------------------------- #
class OpenAIProvider:
    """OpenAI chat tool-calling. Supported alternative to Anthropic."""

    def __init__(self, *, model: str | None = None, api_key: str | None = None) -> None:
        import openai  # lazy

        self._model = model or os.environ.get("OPENAI_MODEL", OPENAI_DEFAULT_MODEL)
        self._client = openai.OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))

    @property
    def label(self) -> str:
        return f"openai:{self._model}"

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[ToolDef]
    ) -> AssistantTurn:
        import json

        api_tools = [{
            "type": "function",
            "function": {"name": t.name, "description": t.description,
                         "parameters": t.input_schema},
        } for t in tools]
        resp = self._client.chat.completions.create(
            model=self._model, tools=api_tools,
            messages=_to_openai(system, messages),
        )
        choice = resp.choices[0].message
        turn = AssistantTurn(text=choice.content or "")
        for tc in choice.tool_calls or []:
            turn.tool_calls.append(ToolCall(
                id=tc.id, name=tc.function.name,
                args=json.loads(tc.function.arguments or "{}"),
            ))
        return turn


def _to_openai(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    import json

    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": m.get("text") or None}
            tcs = m.get("tool_calls", [])
            if tcs:
                msg["tool_calls"] = [{
                    "id": tc.id, "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.args)},
                } for tc in tcs]
            out.append(msg)
        elif role == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"],
                        "content": m["content"]})
    return out


# --------------------------------------------------------------------------- #
# Provider selection                                                            #
# --------------------------------------------------------------------------- #
def select_provider(name: str = "auto") -> LLMProvider:
    """Pick a provider.

    ``auto`` (default): Anthropic if its key + SDK are present, else OpenAI if
    its key + SDK are present, else the scripted fake — so a keyless or SDK-less
    environment degrades cleanly instead of crashing. ``anthropic``/``openai``
    are explicit and *do* raise if unavailable (you asked for that provider).
    ``fake`` forces the no-key scripted brain.
    """
    from acp_ap_demo.fake_llm import FakeProvider

    name = (name or "auto").lower()
    if name == "fake":
        return FakeProvider()
    if name == "anthropic":
        return AnthropicProvider()
    if name == "openai":
        return OpenAIProvider()
    # auto: try the real providers, fall back to fake on any missing key/SDK.
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return AnthropicProvider()
        except ImportError:
            pass
    if os.environ.get("OPENAI_API_KEY"):
        try:
            return OpenAIProvider()
        except ImportError:
            pass
    return FakeProvider()
