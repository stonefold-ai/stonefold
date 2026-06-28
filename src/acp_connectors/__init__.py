"""acp_connectors — the concrete connector adapters (M3, design §5).

Connectors only **execute** an action and apply the **injected scope filter**;
they hold no policy logic (CLAUDE.md). Each satisfies ``acp_core.connector.Connector``
and is injected into ``enforce`` via ``acp_core.Connectors`` — the kernel never
imports this package. The SQL connector imports ``psycopg`` lazily, so importing
this package does not require it.
"""

from __future__ import annotations

from acp_connectors.email import EmailConnector
from acp_connectors.http import HttpConnector
from acp_connectors.memory import InMemoryConnector
from acp_connectors.sql import SqlConnector

__all__ = ["InMemoryConnector", "SqlConnector", "HttpConnector", "EmailConnector"]
