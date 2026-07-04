"""stonefold_connectors — the concrete connector adapters (M3, design §5).

Connectors only **execute** an action and apply the **injected scope filter**;
they hold no policy logic (CLAUDE.md). Each satisfies ``stonefold_core.connector.Connector``
and is injected into ``enforce`` via ``stonefold_core.Connectors`` — the kernel never
imports this package. The SQL connector imports ``psycopg`` lazily, so importing
this package does not require it.
"""

from __future__ import annotations

from stonefold_connectors.email import EmailConnector
from stonefold_connectors.http import HttpConnector
from stonefold_connectors.memory import InMemoryConnector
from stonefold_connectors.sql import SqlConnector

__all__ = ["InMemoryConnector", "SqlConnector", "HttpConnector", "EmailConnector"]
