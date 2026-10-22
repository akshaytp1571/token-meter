# Contributing

Most useful thing you can do is tell me which agent logs fail to parse. The
adapters are the fragile part and I only have three agents on one Windows
machine to test against.

## Adding an agent

Each agent is a `read_*()` function in `agent_adapters.py` returning a
`SourceReport`. The contract:

```python
SourceReport(
    source="my-agent",           # short tag, shows up in the UI
    status="ok",                 # ok | partial | unavailable | absent
    detail="3 files, 308 records",
    samples=[Usage(...), ...],
)
```

Rules that aren't negotiable:

1. **`unavailable` is not `0`.** If you can't read it, say so. A zero claims
   the agent spent nothing, which is a different and false statement.
2. **`partial` when you read some of it.** If half the records parse, report
   `partial` and put the failure count in `detail`.
3. **Don't guess at shapes.** Check for the ones you know. Skip what doesn't
   match rather than inferring a field.
4. **Filter zero-token records.** Streaming placeholders have a `usage` block
   with every field zeroed. Counting them inflates your record count and, on
   one of my transcripts, meant parsing 77.8 MB of nothing.

Then add it to `collect_all()` and write a test in `test_adapters()` that
checks it contributes nothing when its status is `unavailable`.

## Before opening a PR

```bash
python test_token_meter.py
```

All tests pass or it's not ready. If you're changing anything about pricing,
add a test — a silent regression there means showing a wrong dollar amount,
which is the one thing this tool exists to avoid.

## Reporting a parse failure

Open an issue with:

- the agent and its version
- the `detail` string from the `all agents` panel
- **one** redacted sample line from the log

Please strip tokens, paths, and anything from your actual conversations. The
`usage` object on its own is usually enough:

```json
{"usage": {"input_tokens": 1234, "output_tokens": 567, ...}}
```

## Style

Plain Python, stdlib only, no build step. Keep it that way — the whole point
is that someone can read one file and understand what it does.

Comments should say *why*, not *what*. If a line needs a comment to explain
what it does, rewrite the line.
