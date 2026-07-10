"""THE OBLIGATION-REGISTRY ADAPTER — owned by the function developer.

This is the code between the gateway and YOUR system of record (an ERP, an
EMR, a case system). The contract is four operations, all idempotent per
(obligation ref, intent id):

    query(selector)          -> the typed records matching the gateway's selector
    reserve(ref, intent_id)  -> claim a record for one staged action
    consume(ref, intent_id)  -> mark it spent, with a receipt
    release(ref, intent_id)  -> return it (cancellation, expiry)

In production you implement these against the real ERP (reservations carry a
TTL on the ERP's own clock, so a crashed gateway can never lock a line
forever). For the guide, the reference in-memory implementation stands in;
``state_path`` makes reservations visible to the match itself, so a spoken-
for line stops matching at DECISION time.
"""

from __future__ import annotations

from stonefold_store import InMemoryObligationRegistry


def build_adapter() -> InMemoryObligationRegistry:
    """The 'ERP' with one open purchase order: ACME, one $800 line."""
    return InMemoryObligationRegistry(
        {
            "PO-7001": {
                "vendorId": "ACME",
                "state": "open",
                "line": {"amount": 800.0, "state": "unconsumed"},
            },
        },
        state_path="line.state",
    )
