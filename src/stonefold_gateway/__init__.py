# SPDX-License-Identifier: Apache-2.0
"""stonefold_gateway — the application layer: transports (SIF-native tool + MCP proxy),
the kill control plane, the thin admin console, and the dispatch worker (M4–M6).

The ``Gateway`` chokepoint and the transport/coverage helpers depend only on
``stonefold_core`` and are re-exported here. The FastAPI app factory lives in
``stonefold_gateway.main`` (``create_app``) and the REST routers in ``kill_api`` /
``admin_api`` — imported directly so ``import stonefold_gateway`` never requires FastAPI.
"""

from __future__ import annotations

from stonefold_gateway.identity import (
    Identity,
    IdentityProvider,
    IdentityRejected,
    SessionIdentityProvider,
    TransportCredential,
)
from stonefold_gateway.kill_service import KillService
from stonefold_gateway.transport import (
    CoverageError,
    Gateway,
    MCPProxy,
    SifNativeTransport,
    ToolMapping,
    interception_coverage_check,
    submit_intent_schema,
)

__all__ = [
    "Gateway",
    "SifNativeTransport",
    "submit_intent_schema",
    "MCPProxy",
    "ToolMapping",
    "interception_coverage_check",
    "CoverageError",
    "KillService",
    # identity seam (CS-021)
    "IdentityProvider",
    "SessionIdentityProvider",
    "TransportCredential",
    "Identity",
    "IdentityRejected",
]
