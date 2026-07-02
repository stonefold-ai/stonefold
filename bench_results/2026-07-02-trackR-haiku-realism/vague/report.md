# Track R — reliability vs. tool count

> Pilot output. Publish only with the harness, task set, and raw logs (docs/15 §5-6); report the honest picture (§6).

Models: small - reps/cell: 2 - probes: 10

### Correct capability selection (higher is better)
| N | mcp | sif |
|---|---|---|
| 10 |    5% |   25% |
| 50 |    0% |   15% |

### Wrong-tool selection
| N | mcp | sif |
|---|---|---|
| 10 |    0% |    0% |
| 50 |   15% |    5% |

### Hallucinated names
| N | mcp | sif |
|---|---|---|
| 10 |    0% |    0% |
| 50 |    0% |    0% |

### Wrong arguments (right capability, gold value missing)
| N | mcp | sif |
|---|---|---|
| 10 |   30% |   10% |
| 50 |   15% |   10% |

### No tool call (froze)
| N | mcp | sif |
|---|---|---|
| 10 |   10% |   20% |
| 50 |   10% |    5% |

### Asked a clarifying question instead
| N | mcp | sif |
|---|---|---|
| 10 |   55% |   45% |
| 50 |   60% |   65% |

### Mean tokens / call
| N | mcp | sif |
|---|---|---|
| 10 | 447 | 447 |
| 50 | 1634 | 1130 |
