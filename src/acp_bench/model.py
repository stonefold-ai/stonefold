"""Pinned model configuration + token metering (docs/15 §4.5, §5).

Reuses the demo's provider-neutral LLM seam (``acp_ap_demo.llm``): the *same* agent
loop runs against a pinned real model or the deterministic fake, so only the model
varies (fairness §4.3). ``MeteredProvider`` records per-condition token counts —
design §4.2 mandates logging them next to every result.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from acp_ap_demo.llm import (
    AnthropicProvider,
    AssistantTurn,
    LLMProvider,
    OpenAIProvider,
    ToolDef,
    select_provider,
)


@dataclass(frozen=True)
class ModelSpec:
    """One pinned model in the sweep. ``model=None`` uses the provider default."""

    key: str
    provider: str  # "fake" | "anthropic" | "openai"
    model: str | None = None

    @property
    def label(self) -> str:
        return self.key


# design §4.5: at least three pinned tiers spanning capability, including a small one
# (the headline test is whether SIF's advantage grows as the model shrinks). The real
# ids are the author's to finalize before execution; ``fake`` is the build/smoke model.
PINNED_MODELS: tuple[ModelSpec, ...] = (
    ModelSpec(key="fake", provider="fake"),
    ModelSpec(key="small", provider="anthropic", model="claude-haiku-4-5-20251001"),
    ModelSpec(key="mid", provider="anthropic", model="claude-sonnet-5"),
    ModelSpec(key="large", provider="anthropic", model="claude-opus-4-8"),
    ModelSpec(key="oss-small", provider="openai", model="gpt-4o-mini"),
)


def model_by_key(key: str) -> ModelSpec:
    for spec in PINNED_MODELS:
        if spec.key == key:
            return spec
    raise KeyError(f"unknown model key {key!r}; known: {[m.key for m in PINNED_MODELS]}")


def build_provider(spec: ModelSpec) -> LLMProvider:
    """Instantiate the provider for a spec. Real providers import their SDK and need
    an API key — supplied by the author at execution time; the fake needs neither."""
    if spec.provider == "fake":
        return select_provider("fake")
    if spec.provider == "anthropic":
        return AnthropicProvider(model=spec.model)
    if spec.provider == "openai":
        return OpenAIProvider(model=spec.model)
    raise ValueError(f"unknown provider {spec.provider!r}")


@dataclass
class TokenMeter:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


def _est_tokens(text: str) -> int:
    """A deterministic ~4-chars/token estimate (build/smoke path only)."""
    return math.ceil(len(text) / 4)


class MeteredProvider:
    """Wraps a provider, accumulating a token count per ``complete`` call.

    On the build/smoke path this is a deterministic *estimate* (~4 chars/token over
    the serialized prompt + response) so the matrix has a token column. A real run
    MUST replace it with the SDK's reported usage (Anthropic/OpenAI ``response.usage``)
    before any figure is published — the estimate is never a result.
    """

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner
        self.meter = TokenMeter()

    @property
    def label(self) -> str:
        return self._inner.label

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[ToolDef]
    ) -> AssistantTurn:
        self.meter.input_tokens += (
            _est_tokens(system)
            + sum(_est_tokens(json.dumps(m, default=str)) for m in messages)
            + sum(_est_tokens(t.name + t.description + json.dumps(t.input_schema)) for t in tools)
        )
        turn = self._inner.complete(system, messages, tools)
        self.meter.output_tokens += _est_tokens(turn.text) + sum(
            _est_tokens(json.dumps(tc.args, default=str)) for tc in turn.tool_calls
        )
        self.meter.calls += 1
        return turn
