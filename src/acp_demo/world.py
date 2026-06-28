"""The simulated systems the demo agent acts on (M-DEMO).

A tiny, in-memory "company": a customer table (the scope column is ``owner_id``),
an outbound mailbox (where an email *leaves* the building), and a bulk exporter
(the exfiltration channel). The connectors are the real ``InMemoryConnector`` —
they apply the injected scope on reads and capture effects — so the demo exercises
the genuine enforcement path, not a mock. ``alice`` owns 3 of the 10 customers;
the rest belong to other reps and must never be visible to her.
"""

from __future__ import annotations

from dataclasses import dataclass

from acp_core import Connectors
from acp_connectors import InMemoryConnector

ALICE = "alice"


def _seed_customers() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for i in range(1, 4):  # alice's own customers
        rows.append({"id": f"C{i}", "owner_id": ALICE, "name": f"Acme Buyer {i}",
                     "ssn": f"000-00-000{i}", "email": f"buyer{i}@acme.example"})
    for i in range(4, 11):  # other reps' customers — out of alice's scope
        rows.append({"id": f"C{i}", "owner_id": "rep-bob", "name": f"Other Buyer {i}",
                     "ssn": f"111-11-11{i:02d}", "email": f"other{i}@acme.example"})
    return rows


@dataclass
class World:
    """Holds the connectors and lets the narration/tests inspect what happened."""

    customer_db: InMemoryConnector
    mailbox: InMemoryConnector
    exporter: InMemoryConnector

    def connectors(self) -> Connectors:
        # connector NAMES are pinned by the registry (Customer→sql, Email→email,
        # Export→in_memory); map each to the matching world surface.
        return Connectors(
            {"sql": self.customer_db, "email": self.mailbox, "in_memory": self.exporter}
        )

    # --- inspection helpers --------------------------------------------------
    def all_customers(self) -> list[dict[str, object]]:
        return self.customer_db.tables.get("Customer", [])

    def emails_sent_to(self) -> list[str]:
        return [str(e["data"].get("to", "")) for e in self.mailbox.effects]

    def exported_payloads(self) -> list[dict[str, object]]:
        return [e["data"] for e in self.exporter.effects]

    def external_emails(self, corporate_domain: str = "acme.example") -> list[str]:
        return [to for to in self.emails_sent_to() if not to.endswith("@" + corporate_domain)]


def build_world() -> World:
    return World(
        customer_db=InMemoryConnector({"Customer": _seed_customers()}),
        mailbox=InMemoryConnector(),
        exporter=InMemoryConnector(),
    )
