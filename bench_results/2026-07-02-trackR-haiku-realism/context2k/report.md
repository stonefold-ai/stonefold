# Track R — reliability vs. tool count

> Pilot output. Publish only with the harness, task set, and raw logs (docs/15 §5-6); report the honest picture (§6).

Models: small - reps/cell: 2 - probes: 10

### Correct capability selection (higher is better)
| N | mcp | sif |
|---|---|---|
| 10 |   75% |   95% |
| 50 |   60% |   90% |

### Wrong-tool selection
| N | mcp | sif |
|---|---|---|
| 10 |    0% |    0% |
| 50 |   25% |   10% |

### Hallucinated names
| N | mcp | sif |
|---|---|---|
| 10 |    0% |    0% |
| 50 |    0% |    0% |

### Wrong arguments (right capability, gold value missing)
| N | mcp | sif |
|---|---|---|
| 10 |   20% |    0% |
| 50 |    5% |    0% |

### No tool call (froze)
| N | mcp | sif |
|---|---|---|
| 10 |    5% |    5% |
| 50 |   10% |    0% |

### Mean tokens / call
| N | mcp | sif |
|---|---|---|
| 10 | 2815 | 2822 |
| 50 | 4000 | 3486 |
