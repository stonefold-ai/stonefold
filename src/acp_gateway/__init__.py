"""acp_gateway — the application layer: transports (SIF-native tool + MCP proxy),
the kill control plane, the thin admin console, and the dispatch worker (M4–M6).

The ``Gateway`` chokepoint and the transport/coverage helpers depend only on
``acp_core`` and are re-exported here. The FastAPI app factory lives in
``acp_gateway.main`` (``create_app``) and the REST routers in ``kill_api`` /
``admin_api`` — imported directly so ``import acp_gateway`` never requires FastAPI.
"""

from __future__ import annotations

from acp_gateway.identity import (
    Identity,
    IdentityProvider,
    IdentityRejected,
    SessionIdentityProvider,
    TransportCredential,
)
from acp_gateway.kill_service import KillService
from acp_gateway.transport import (
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
