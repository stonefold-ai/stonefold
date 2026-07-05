# Security policy

Stonefold is an enforcement layer. A flaw that lets an action bypass, weaken, or race the
gateway's stated guarantees is a security issue, not an ordinary bug — please report it
privately.

## How to report

**Do not open a public issue or PR.** Use GitHub's private vulnerability reporting on
this repository (Security tab → "Report a vulnerability"). If that is unavailable to you,
email **gallas.robert@gmail.com** with the subject line `STONEFOLD SECURITY`.

A useful report includes:

- The affected component (module in `src/`, or the RFC section if the flaw is in the spec
  itself — spec-level flaws are also welcome here).
- A reproduction. The ideal shape is an intent + policy + registry that demonstrates the
  bypass — the same shape as the scenarios in `tests/acceptance-scenarios.md`.
- The guarantee you believe is broken (default deny, scope injection below the model,
  staged effects, the kill no-race transaction, transactional audit, fail closed — see
  `CLAUDE.md` for the full list).

Reports must be verified by a human before sending. Unverified AI-generated reports waste
the capacity this policy exists to protect and will get the sender banned.

## What to expect

This is a solo-maintained project, so the promises are modest but honest:

- Acknowledgment within a few days.
- An honest assessment of whether the report is valid, and why.
- Coordinated disclosure: a fix (or a documented limitation) before the report is made
  public, on a timeline agreed with you.
- Credit in the changelog and advisory, if you want it.
- There is no bounty program.

## Scope

**In scope:** anything that breaks a guarantee stated in the RFCs or `CLAUDE.md` —
enforcement bypasses, scope or identity injection, kill-switch races, audit omissions,
outbox bypasses, fail-open behavior not opted into, and spec wording that would make a
conformant implementation exploitable.

**Out of scope:** hardening of the demo application beyond its stated purpose, and
vulnerabilities in third-party dependencies with no Stonefold-specific attack path
(report those upstream).
