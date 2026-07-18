# SPDX-License-Identifier: Apache-2.0
"""Accounts-Payable demo for the Stonefold Gateway (docs/05-demo-spec.md, acceptance §G).

A **real-LLM** AI accounts-payable assistant that reads invoices and pays vendors,
sitting behind the *unmodified* ``examples/payments-ops.stele.yaml`` policy. The bank
and ledger are faked (no real money, all data fictional); the agent and the gateway
enforcement are real. The package is import-clean (no FastAPI/psycopg/LLM SDK at
module import) so the scenario logic can be unit-tested without Docker or a key —
the heavy dependencies are imported lazily inside the functions that need them.

Layout:
* ``principals``  — server-side identity directory (id → tenant/roles, invariant 3)
* ``ledger``      — the fake ledger: backend protocol, in-memory + Postgres backends,
                    the ``LedgerConnector`` (scope-injecting reads, money-moving
                    dispatch) and the ``payeeCoolingOffElapsed`` precondition.
* ``seed``        — fictional vendors/accounts/payees/invoices + the malicious .eml
* ``trace``       — in-process trace bus (intent → decision → effect) for the UI
* ``gateway``     — assemble the full enforcement stack over payments-ops
* ``llm``         — provider abstraction (Anthropic default, OpenAI, fake-LLM)
* ``agent``       — the real-LLM tool-use loop + the two tool backends
* ``scenarios``   — G1–G7 orchestration, shared by the tests and ``make demo``
"""

from __future__ import annotations

DEMO_AGENT = "payments-ops-agent"
DEMO_TENANT = "acme-treasury"
