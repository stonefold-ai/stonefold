"""stonefold_bench — the benchmark harness for docs/15. Two experiments, each runnable in
isolation from the console (``python -m stonefold_bench --track r|s ...``):

**Track R — tool-selection effectiveness (the comparison this harness was asked for).**
How effectively does a model *select the right capability* when the same N capabilities
are offered three ways: as N separate MCP tools, as a retrieval-filtered top-k of those
tools, and as one SIF ``submit_intent`` whose registry declares them all? Measured per
condition × N: correct / wrong-tool / hallucinated / malformed / no-call rates and
token cost (``reliability``, ``tracks``).

**Track S — security ablation (the docs/15 headline claim).** Does the SIF+Stele surface
stop injection/hallucination from becoming *executed* unauthorized effects where
commodity gateway defenses (S0 naked → S1 allowlist → S2 parameter policy → S3 full)
do not? Measured as ASR-executed / ASR-attempted per attack class × rung, with benign
task success (BTS) alongside (``conditions``, ``attacks``, ``runner``, ``matrix``).

**Output is streamed, structured, and graph-ready** (``raw_log``): every finished trial
is appended + flushed to ``trials.jsonl`` immediately; the aggregated cells are
rewritten as ``cells.json``/``cells.csv`` after every completed round (repetition); a
Markdown report and ``meta.json`` close the run. See ``README.md`` for the field
schemas.

**Build only.** This package is machinery. It does not contain, and MUST NOT be
committed with, any result: execution, baseline-fairness sign-off, and publication are
the author's, personally and non-delegably (docs/15 §7). Nothing in the repo may quote
a number until the harness, gateway configs, and raw logs are public (docs/15 §6). The
smoke path (``--smoke``) drives the deterministic fake LLM to prove the machinery runs
end to end; its output is labelled SMOKE and is not a result.
"""

from __future__ import annotations

__all__ = ["__doc__"]
