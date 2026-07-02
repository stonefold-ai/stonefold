"""acp_bench — the benchmark harness for docs/15 (SIF surface vs. tool surface).

**Build only.** This package is the *machinery* that turns the project's central
claim into a measurement (docs/15). It does not contain, and MUST NOT be committed
with, any result: execution, baseline-fairness sign-off, and publication are the
author's, personally and non-delegably (docs/15 §7). Nothing in the repo may quote a
number until the harness, gateway configs, and raw logs are public (docs/15 §6).

What it builds, reusing the demo agent, the ``make demo`` battery, and the AP demo
(docs/05) as the domain substrate:

* Track S — the defense ablation S0→S3 (``conditions``) over one payments domain, so
  only enforcement strength varies (fairness, §4.4).
* Track R — the tool-count reliability sweep (``tracks``): N MCP tools vs. one SIF
  ``submit_intent`` declaring the same N capabilities.
* The A1–A7 attack slots (``attacks``): success = an unauthorized effect *executed*,
  not merely attempted (§3).
* The matrix reporter (``matrix``/``report``): ASR-executed vs. ASR-attempted, BTS,
  token counts, with variance over ≥5 repetitions (``runner``, §5).
* Raw-log output (``raw_log``) and a one-command runner (``__main__``).

The smoke path (``python -m acp_bench --smoke``) drives the deterministic fake LLM to
prove the machinery runs end to end; its output is labelled SMOKE and is not a result.
"""

from __future__ import annotations

__all__ = ["__doc__"]
