# Track R — reliability vs. tool count

> Pilot output. Publish only with the harness, task set, and raw logs (docs/15 §5-6); report the honest picture (§6).

Models: small - reps/cell: 2 - probes: 10

### Correct capability selection (higher is better)
| N | mcp | sif |
|---|---|---|
| 10 |   90% |   80% |
| 50 |   90% |   70% |

### Wrong-tool selection
| N | mcp | sif |
|---|---|---|
| 10 |    0% |    0% |
| 50 |    0% |   10% |

### Hallucinated names
| N | mcp | sif |
|---|---|---|
| 10 |    0% |    0% |
| 50 |    0% |    0% |

### No tool call (froze)
| N | mcp | sif |
|---|---|---|
| 10 |   10% |    5% |
| 50 |   10% |    5% |

### Asked a clarifying question instead
| N | mcp | sif |
|---|---|---|
| 10 |    0% |   15% |
| 50 |    0% |   15% |

### Mean tokens / call
| N | mcp | sif |
|---|---|---|
| 10 | 1713 | 500 |
| 50 | 8251 | 1519 |
