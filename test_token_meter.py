#!/usr/bin/env python3
"""Invariants for Token Meter. Run: python test_token_meter.py

These test the claims the widget makes about money and coverage. A silent
regression here means showing a wrong dollar figure, so each test pins a
property rather than an exact number (the store keeps growing).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod          # dataclasses need the module registered
    spec.loader.exec_module(mod)
    return mod


tm = _load("token_meter", "token_meter.py")
ad = _load("agent_adapters", "agent_adapters.py")

PASS = FAIL = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  {detail}")


# ── Pricing: the money invariant ─────────────────────────────────────────────

def test_pricing():
    print("\npricing")
    p = tm.price_for("claude-opus-5.5")
    check("known model is priced", p is not None and p["input"] > 0, str(p))

    usd, ok = tm.cost_of("claude-opus-5.5", 1_000_000, 0, 0, 0)
    check("1M input bills the input rate", ok and abs(usd - p["input"]) < 1e-9,
          f"{usd} vs {p['input']}")

    usd, ok = tm.cost_of("claude-opus-5.5", 0, 1_000_000, 0, 0)
    check("1M output bills the output rate", ok and abs(usd - p["output"]) < 1e-9,
          f"{usd} vs {p['output']}")

    # The central honesty rule: unknown is None, never 0.
    usd, ok = tm.cost_of("a-model-that-does-not-exist-xyz", 1_000_000, 0, 0, 0)
    check("unknown model -> (None, False), never 0", usd is None and ok is False,
          f"got {usd!r}, {ok!r}")

    free = tm.price_for("minimax/minimax-m3.1-flash:free")
    check(":free model prices at zero, not unknown",
          free is not None and free["input"] == 0 and free["free"] is True, str(free))


# ── Formatting ───────────────────────────────────────────────────────────────

def test_formatting():
    print("\nformatting")
    check("compact never zeroes a small positive", tm.compact(0.3) != "0", tm.compact(0.3))
    check("compact keeps sub-cent visible", tm.compact(0.04) == "0.04", tm.compact(0.04))
    check("compact scales to K", tm.compact(1500) == "1.5K", tm.compact(1500))
    check("compact scales to M", tm.compact(3_300_000) == "3.3M", tm.compact(3_300_000))
    check("compact of 0 is 0", tm.compact(0) == "0", tm.compact(0))

    check("money of None is a dash", tm.money(None) == "—", tm.money(None))
    check("money of 0 is $0", tm.money(0) == "$0", tm.money(0))
    check("sub-cent money is escaped", "&lt;" in tm.money(0.001), tm.money(0.001))
    check("money uses 2dp under $10", tm.money(0.15) == "$0.15", tm.money(0.15))

    check("plural of 1 is singular", tm.plural(1, "session") == "1 session",
          tm.plural(1, "session"))
    check("plural of 0 is plural", tm.plural(0, "call") == "0 calls",
          tm.plural(0, "call"))
    check("plural of 3 is plural", tm.plural(3, "call") == "3 calls",
          tm.plural(3, "call"))


# ── Payload + render ─────────────────────────────────────────────────────────

def test_payload():
    print("\npayload")
    p = tm.collect(30)
    t = p["totals"]

    check("total = prompt + output",
          t["total"] == t["prompt"] + t["output"], f"{t['total']}")
    check("prompt = input + cache_read + cache_write",
          t["prompt"] == t["input"] + t["cache_read"] + t["cache_write"], "")
    check("coverage is a percentage", 0 <= t["priced_pct"] <= 100, f"{t['priced_pct']}")
    check("cost is non-negative", t["cost_usd"] >= 0, f"{t['cost_usd']}")
    check("cache_hit within 0..100",
          t["cache_hit"] is None or 0 <= t["cache_hit"] <= 100, f"{t['cache_hit']}")

    # A cost must never appear for a model that has no price.
    for m in p["by_model"]:
        if not m["priced"]:
            check(f"unpriced model {m['model']!r} has no cost",
                  m["cost_usd"] is None, f"got {m['cost_usd']}")
            break

    check("burn rate is None or a number, never fabricated",
          p["burn"]["tok_per_min"] is None or p["burn"]["tok_per_min"] >= 0,
          f"{p['burn']['tok_per_min']}")

    # Bars must be monotonic in the value they draw.
    vals = [(m["input"] or 0) + (m["output"] or 0) for m in p["by_model"]]
    check("by_model sorted by drawn value",
          all(vals[i] >= vals[i + 1] for i in range(len(vals) - 1)), str(vals[:5]))


def test_render():
    print("\nrender")
    p = tm.collect(30)
    h = tm.render(p)

    import re
    left = sorted(set(re.findall(r"__[A-Z_]+__", h)))
    check("no unfilled placeholders", not left, str(left))
    check("no literal None in output", ">None<" not in h, "")
    check("no unescaped '<$' in text", not re.search(r">[^<]*<\$", h), "")
    check("bad pluralisation absent",
          not re.search(r"\b1 (sessions|calls)\b", h), "")
    check("html is non-trivial", len(h) > 5000, str(len(h)))

    # No catalog must never render as "$0" — that reads as "these tokens were
    # free", which is a claim the meter cannot support.
    real_cache, real_index = tm.pricing_cache, tm._PRICE_INDEX
    try:
        tm.pricing_cache = lambda: Path("C:/nonexistent/models_dev_cache.json")
        tm._PRICE_INDEX = None
        h2 = tm.render(tm.collect(30))
        check("missing catalog shows n/a, not $0",
              ">$0<" not in h2 and "n/a" in h2, "rendered a cost with no catalog")
        check("missing catalog is disclosed",
              "no pricing catalog" in h2, "")
    finally:
        tm.pricing_cache, tm._PRICE_INDEX = real_cache, real_index


# ── Adapters ─────────────────────────────────────────────────────────────────

def test_adapters():
    print("\nadapters")
    reports = ad.collect_all()
    check("every agent is reported", len(reports) >= 3, str(len(reports)))

    for r in reports:
        check(f"{r.source} has a valid status",
              r.status in ("ok", "partial", "unavailable", "absent"), r.status)
        # An unreadable source must not contribute tokens.
        if r.status in ("unavailable", "absent"):
            check(f"{r.source} contributes 0 when unreadable",
                  r.totals["total"] == 0, str(r.totals["total"]))

    agg = ad.aggregate(reports)
    g = agg["grand_total"]
    check("grand total = sum of source totals",
          g["total"] == sum(v.get("total", 0) for v in agg["sources"].values()),
          f"{g['total']}")
    check("grand total is positive on this machine", g["total"] > 0, str(g["total"]))

    # Claude Code, when present, must be internally consistent.
    cc = next((r for r in reports if r.source == "claude-code"), None)
    if cc and cc.status == "ok":
        t = cc.totals
        check("claude-code total = sum of parts",
              t["total"] == t["input"] + t["output"] + t["cache_read"] + t["cache_write"], "")
        check("claude-code records are positive", t["records"] > 0, str(t["records"]))


# ── Ledger ───────────────────────────────────────────────────────────────────

def test_ledger():
    print("\nledger")
    p = tm.collect(30)
    tm.rollup(p)
    tm.record_sample(p)

    import sqlite3
    con = sqlite3.connect(f"file:{tm.ledger_path()}?mode=ro", uri=True)
    try:
        days = con.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
        samples = con.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    finally:
        con.close()
    check("ledger has day rows", days > 0, str(days))
    check("ledger has samples", samples > 0, str(samples))

    # Idempotent: re-running must not duplicate a day.
    tm.rollup(p)
    con = sqlite3.connect(f"file:{tm.ledger_path()}?mode=ro", uri=True)
    try:
        days2 = con.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
    finally:
        con.close()
    check("rollup is idempotent", days2 == days, f"{days} -> {days2}")


# ── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_pricing()
    test_formatting()
    test_payload()
    test_render()
    test_adapters()
    test_ledger()

    print(f"\n{'=' * 46}")
    print(f"  {PASS} passed, {FAIL} failed")
    if FAILURES:
        print("\n  failures:")
        for f in FAILURES:
            print(f"    - {f}")
    raise SystemExit(1 if FAIL else 0)
