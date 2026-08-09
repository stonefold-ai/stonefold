# SPDX-License-Identifier: Apache-2.0
"""stonefold_gateway — the application layer: the transports (the intent endpoint,
and the MCP surface of typed tools, retrieval and the interception proxy), the kill
control plane, the thin admin console, and the dispatch worker (M4–M6).

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
from stonefold_gateway.mcp_search import score, search, search_response
from stonefold_gateway.transport import (
    CoverageError,
    Gateway,
    InvalidIntentError,
    MCPProxy,
    SifNativeTransport,
    ToolMapping,
    interception_coverage_check,
    mcp_tool_schemas,
    validate_intent_data,
)

__all__ = [
    "Gateway",
    "SifNativeTransport",
    "MCPProxy",
    "ToolMapping",
    "interception_coverage_check",
    "CoverageError",
    "KillService",
    # the MCP surface (design §1.2): the typed tools, and retrieval over them
    "mcp_tool_schemas",
    "score",
    "search",
    "search_response",
    # declared shape, shared by every transport
    "InvalidIntentError",
    "validate_intent_data",
    # identity seam (CS-021)
    "IdentityProvider",
    "SessionIdentityProvider",
    "TransportCredential",
    "Identity",
    "IdentityRejected",
]
