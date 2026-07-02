# Track R — reliability vs. tool count

> Pilot output. Publish only with the harness, task set, and raw logs (docs/15 §5-6); report the honest picture (§6).

Models: mid - reps/cell: 2 - probes: 10

### Correct capability selection (higher is better)
| N | mcp | sif |
|---|---|---|
| 10 |  100% |  100% |
| 50 |  100% |  100% |
| 100 |  100% |  100% |

### Wrong-tool selection
| N | mcp | sif |
|---|---|---|
| 10 |    0% |    0% |
| 50 |    0% |    0% |
| 100 |    0% |    0% |

### Hallucinated names
| N | mcp | sif |
|---|---|---|
| 10 |    0% |    0% |
| 50 |    0% |    0% |
| 100 |    0% |    0% |

### Mean tokens / call
| N | mcp | sif |
|---|---|---|
| 10 | 289 | 257 |
| 50 | 1162 | 459 |
| 100 | 2263 | 718 |
