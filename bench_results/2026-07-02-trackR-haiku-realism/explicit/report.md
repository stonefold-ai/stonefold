# Track R — reliability vs. tool count

> Pilot output. Publish only with the harness, task set, and raw logs (docs/15 §5-6); report the honest picture (§6).

Models: small - reps/cell: 2 - probes: 10

### Correct capability selection (higher is better)
| N | mcp | sif |
|---|---|---|
| 10 |   85% |   90% |
| 50 |   40% |   60% |

### Wrong-tool selection
| N | mcp | sif |
|---|---|---|
| 10 |    0% |    5% |
| 50 |   55% |   40% |

### Hallucinated names
| N | mcp | sif |
|---|---|---|
| 10 |    0% |    0% |
| 50 |    0% |    0% |

### Wrong arguments (right capability, gold value missing)
| N | mcp | sif |
|---|---|---|
| 10 |   10% |    0% |
| 50 |    5% |    0% |

### No tool call (froze)
| N | mcp | sif |
|---|---|---|
| 10 |    5% |    0% |
| 50 |    0% |    0% |

### Asked a clarifying question instead
| N | mcp | sif |
|---|---|---|
| 10 |    0% |    5% |
| 50 |    0% |    0% |

### Mean tokens / call
| N | mcp | sif |
|---|---|---|
| 10 | 410 | 417 |
| 50 | 1589 | 1079 |
