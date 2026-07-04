"""Connector digest pinning (CS-020, RFC change set v0.4→v0.5; docs/06 §5).

A connector declaration MAY pin the artifact that implements it by content digest
(``digest: "sha256:<64 hex>"`` in the registry). When a digest is declared the
gateway MUST verify the loaded connector against it **at policy load and at
dispatch**; a mismatch is a *dependency failure* under the policy's ``failureMode``
(RFC §10) — fail closed by default, with an audit record. The registry already
declares *what* a connector does; the digest declares *which code* is trusted to do
it, so silently swapping a connector's implementation stops being invisible.

The hashable artifact (deliberately implementation-defined by the RFC)
-----------------------------------------------------------------------
The reference implementation pins **the source bytes of the Python module that
implements the connector** — ``sha256`` over the file returned by
``inspect.getsourcefile(type(connector))``, formatted ``sha256:<hex>``. Rationale:

- it is the "which code runs" question at the granularity a reviewer reads and a
  registry change gates (a connector lives in one module in this codebase);
- it needs no build step, signing infrastructure, or packaging assumptions, so the
  concept deliverable can demonstrate the load/dispatch check end to end;
- a production deployment would instead pin the *built/deployed* artifact (a wheel,
  an image layer, a signed bundle) — the digest declaration is identical, only this
  ``artifact_digest`` function changes. The RFC leaves that choice to the
  implementation; docs/06 §5 says as much.

A digest that cannot be computed (a built-in, a connector with no on-disk source)
is treated as a **mismatch**, never a pass: a control you cannot evaluate must not
be assumed satisfied (the same principle as the ``failureMode`` floor).

This module is pure (no framework, no network) — part of the trust kernel.
"""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from stonefold_core.audit import AuditSink, build_record
from stonefold_core.connector import ConnectorRegistry
from stonefold_core.enums import Decision
from stonefold_core.failure import should_fail_closed
from stonefold_core.models import (
    Actor,
    AuditRecord,
    EvalResult,
    RawCall,
    ResolvedAction,
    Session,
)
from stonefold_core.policy import FailureMode

# The rule/settle-reason recorded on a digest-mismatch refusal (audit + outbox).
DIGEST_MISMATCH = "connector-digest-mismatch"


@dataclass(frozen=True)
class DigestMismatch:
    """One connector whose loaded artifact does not match its pinned digest.

    ``actual`` is ``None`` when the artifact digest could not be computed at all
    (still a mismatch — see the module docstring)."""

    connector: str
    expected: str
    actual: str | None


class DigestMismatchError(Exception):
    """Raised at policy load when a pinned connector fails verification and the
    active ``failureMode`` is closed — the gateway MUST NOT come up trusting an
    unverified connector (RFC §10)."""

    def __init__(self, mismatches: list[DigestMismatch]) -> None:
        self.mismatches = mismatches
        detail = ", ".join(
            f"{m.connector} (pinned {m.expected}, loaded {m.actual})" for m in mismatches
        )
        super().__init__(f"connector digest mismatch at load: {detail}")


class _DigestRegistry(Protocol):
    """The slice of the registry this module reads (structural, no import cost)."""

    @property
    def connector_digests(self) -> Mapping[str, str]: ...


def artifact_digest(connector: object) -> str:
    """The ``sha256:<hex>`` digest of the connector's implementing module source.

    Raises ``OSError`` / ``TypeError`` when the source cannot be located (a
    built-in, a dynamically built class) — callers that must not fail treat that
    as a mismatch via :func:`digest_matches`.
    """
    source_file = inspect.getsourcefile(type(connector))
    if source_file is None:  # no associated source (e.g. a C extension type)
        raise OSError(f"no source file for {type(connector)!r}")
    data = Path(source_file).read_bytes()
    return "sha256:" + hashlib.sha256(data).hexdigest()


def digest_matches(connector: object, expected: str) -> bool:
    """Whether the connector's artifact matches ``expected`` (hex case-insensitive).

    Returns ``False`` — a mismatch — when the artifact digest cannot be computed;
    an unverifiable connector is never treated as verified.
    """
    try:
        actual = artifact_digest(connector)
    except Exception:
        return False
    return actual.lower() == expected.strip().lower()


def pinned_connector_mismatch(
    connectors: ConnectorRegistry, resolved: ResolvedAction
) -> bool:
    """True iff ``resolved`` pins a connector digest AND the wired connector exists
    but does not match it.

    ``False`` when nothing is pinned (the common case — zero overhead) or when the
    connector is absent: an unknown connector is already handled as a fail-closed
    dependency failure by the normal execute/dispatch path, so this check does not
    duplicate it.
    """
    expected = resolved.connector_digest
    if expected is None:
        return False
    try:
        connector = connectors.get(resolved.connector)
    except Exception:
        return False
    return not digest_matches(connector, expected)


def verify_connector_digests(
    registry: _DigestRegistry, connectors: ConnectorRegistry
) -> list[DigestMismatch]:
    """Verify every pinned connector against its wired implementation (load-time).

    A connector declared with a digest but not wired into ``connectors`` is skipped
    (it cannot be checked here; the unknown-connector path fails it closed at
    dispatch). Returns the mismatches; the empty list means every pin verified.
    """
    mismatches: list[DigestMismatch] = []
    for name, expected in registry.connector_digests.items():
        try:
            connector = connectors.get(name)
        except Exception:
            continue  # not wired here — deferred to the dispatch-time check
        if not digest_matches(connector, expected):
            actual: str | None
            try:
                actual = artifact_digest(connector)
            except Exception:
                actual = None
            mismatches.append(DigestMismatch(connector=name, expected=expected, actual=actual))
    return mismatches


def assert_connector_digests(
    registry: _DigestRegistry,
    connectors: ConnectorRegistry,
    *,
    failure_mode: FailureMode,
    audit: AuditSink | None = None,
    agent: str = "gateway",
) -> list[DigestMismatch]:
    """Load-time gate: verify pinned connectors and enforce ``failureMode``.

    Under a closed failure mode (the default) any mismatch is fatal: an audit
    record is written per mismatch and :class:`DigestMismatchError` is raised so the
    gateway refuses to serve with an unverified connector. Under an open failure
    mode the mismatches are returned (non-fatal, low-stakes) for the caller to log.
    The ``resolved is None`` load context maps onto :func:`should_fail_closed`
    exactly as an outage before any specific action does.
    """
    mismatches = verify_connector_digests(registry, connectors)
    if not mismatches:
        return mismatches
    if should_fail_closed(None, failure_mode):
        if audit is not None:
            for mismatch in mismatches:
                audit.write(_load_mismatch_record(mismatch, agent))
        raise DigestMismatchError(mismatches)
    return mismatches


def _load_mismatch_record(mismatch: DigestMismatch, agent: str) -> AuditRecord:
    """A DENY audit record for a load-time digest refusal (RFC §11)."""
    result = EvalResult(decision=Decision.DENY, rule=DIGEST_MISMATCH)
    return build_record(
        agent=agent,
        actor=Actor(id=agent),
        session=Session(id="policy-load"),
        call=RawCall(resource=mismatch.connector),
        resolved=None,
        result=result,
    )
