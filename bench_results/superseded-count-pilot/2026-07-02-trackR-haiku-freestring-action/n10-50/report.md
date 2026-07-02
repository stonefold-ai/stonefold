# Track R — reliability vs. tool count

> Pilot output. Publish only with the harness, task set, and raw logs (docs/15 §5-6); report the honest picture (§6).

Models: small - reps/cell: 2 - probes: 10

### Correct capability selection (higher is better)
| N | mcp | sif |
|---|---|---|
| 10 |   90% |   80% |
| 50 |   90% |  100% |

### Wrong-tool selection
| N | mcp | sif |
|---|---|---|
| 10 |    0% |   20% |
| 50 |    0% |    0% |

### Hallucinated names
| N | mcp | sif |
|---|---|---|
| 10 |    0% |    0% |
| 50 |    0% |    0% |

### Mean tokens / call
| N | mcp | sif |
|---|---|---|
| 10 | 299 | 239 |
| 50 | 1175 | 438 |
