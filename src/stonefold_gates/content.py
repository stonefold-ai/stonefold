# SPDX-License-Identifier: Apache-2.0
"""The ``contentCheck`` hook SPI and a sample ``dlp.basic`` hook (design §6).

``contentCheck`` is the only gate that calls out synchronously, so it is the
latency/availability risk. Hooks run under a **bounded timeout**; on timeout or
error the gate applies ``failureMode`` (closed ⇒ block — see ``gates.content_check``).
A hook returns ``True`` for *clean / pass* and ``False`` for *block*; it may raise
``HookError`` (e.g. ``HookTimeout``) to signal a dependency failure.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Any

# True ⇒ content is clean (pass); False ⇒ block.
ContentHook = Callable[[Mapping[str, Any]], bool]


class HookError(Exception):
    """A content hook failed to produce a verdict (dependency failure)."""


class HookTimeout(HookError):
    """A content hook exceeded its bounded timeout."""


class ContentHookRegistry:
    """Named, registered content hooks with a shared bounded timeout."""

    def __init__(
        self, hooks: Mapping[str, ContentHook] | None = None, *, timeout_s: float = 2.0
    ) -> None:
        self._hooks: dict[str, ContentHook] = dict(hooks or {})
        self._timeout_s = timeout_s

    def register(self, name: str, hook: ContentHook) -> None:
        self._hooks[name] = hook

    def has(self, name: str) -> bool:
        return name in self._hooks

    def run(self, name: str, content: Mapping[str, Any]) -> bool:
        """Run hook ``name`` against ``content`` under the bounded timeout.

        Re-raises ``HookError`` (including ``HookTimeout``) on failure so the
        gate can apply ``failureMode``. Never returns on timeout — it raises.
        """
        hook = self._hooks.get(name)
        if hook is None:
            raise HookError(f"unknown content hook {name!r}")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(hook, content)
            try:
                return bool(future.result(timeout=self._timeout_s))
            except FuturesTimeout as exc:
                raise HookTimeout(f"content hook {name!r} timed out") from exc
            except HookError:
                raise
            except Exception as exc:  # any hook crash is a dependency failure
                raise HookError(f"content hook {name!r} failed: {exc}") from exc


# --- the sample dlp.basic hook -------------------------------------------
_DLP_PATTERNS = (
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),          # US SSN-shaped
    re.compile(r"\b(?:\d[ -]?){13,16}\b"),          # card-number-shaped
    re.compile(r"(?i)\b(classified|top[\s-]?secret)\b"),
)


def dlp_basic(content: Mapping[str, Any]) -> bool:
    """A deterministic stand-in DLP scan: blocks if the parameters look like they
    carry an SSN, a card number, or a classification marking."""
    blob = " ".join(str(v) for v in content.values())
    return not any(p.search(blob) for p in _DLP_PATTERNS)


def default_hooks(*, timeout_s: float = 2.0) -> ContentHookRegistry:
    """The default registry: the one sample hook the registry YAML declares."""
    return ContentHookRegistry({"dlp.basic": dlp_basic}, timeout_s=timeout_s)
