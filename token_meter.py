#!/usr/bin/env python3
"""Token Meter v2 — token *and cost* monitor for Hermes.

Why v2 exists
-------------
v1 was a dashboard: it showed tokens, cache ratio, per-model split. Correct,
but it answered "what happened". A monitoring product has to answer three more
questions:

  1. **What did it cost?**  v1 showed $0 because it trusted the store's own
     cost columns, which are `unknown` for custom providers. But 99.4% of this
     user's tokens ARE priceable from the local models.dev catalog. So v2
     prices every model itself and labels the number an estimate at list
     price, with the coverage % shown — a partial number can never
     masquerade as a total.
  2. **Am I about to overspend?**  Budgets (daily/monthly), a gauge, and one
     alert per period when a cap is crossed.
  3. **What am I burning right now?**  A live-session card with tok/min.

Plus the things a monitor needs to be trusted over months: a **durable
ledger** (our own sqlite — Hermes prunes sessions, the ledger must not),
**export** (CSV/JSON for expense reports), and **forecast** (current pace
projected to a day).

Read-only against state.db. Writes only its own config, ledger, and alert file.

Usage
-----
    python token_meter_v2.py                       # serve + open popup
    python token_meter_v2.py --no-open             # serve only
    python token_meter_v2.py --days 7 --port 8765
    python token_meter_v2.py --set-budget daily=5 monthly=50 alert=80
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP = "token-meter"


# ── Paths ────────────────────────────────────────────────────────────────────

def hermes_home() -> Path:
    if env := os.environ.get("HERMES_HOME"):
        return Path(env)
    if local := os.environ.get("LOCALAPPDATA"):
        return Path(local) / "hermes"
    return Path.home() / ".hermes"


def state_db() -> Path:
    return hermes_home() / "state.db"


def pricing_cache() -> Path:
    return hermes_home() / "models_dev_cache.json"


def config_path() -> Path:
    return hermes_home() / "token_meter.json"


def ledger_path() -> Path:
    return hermes_home() / "token_meter_ledger.db"


# ── Config ───────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "daily_usd": None,
    "monthly_usd": None,
    "alert_at_pct": 80,
    "currency": "$",
}


def load_config() -> dict:
    try:
        raw = json.loads(config_path().read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {**DEFAULT_CONFIG, **raw}
    except (OSError, ValueError):
        pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    config_path().write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


# ── Pricing (models.dev catalog, USD per MILLION tokens) ─────────────────────

_PRICE_INDEX: dict[str, dict] | None = None


def _normalize(model: str) -> str:
    m = (model or "").strip().lower()
    for tag in (":free", ":nitro", ":beta"):
        if tag in m:
            m = m.split(tag)[0]
    return m.split("/")[-1] if "/" in m else m


def load_pricing() -> dict[str, dict]:
    """model-id -> {input, output, cache_read, cache_write, context, free}.

    Cached per process. A missing or corrupt catalog yields {} — callers must
    treat absent pricing as UNKNOWN, never as free.
    """
    global _PRICE_INDEX
    if _PRICE_INDEX is not None:
        return _PRICE_INDEX

    index: dict[str, dict] = {}
    try:
        catalog = json.loads(pricing_cache().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _PRICE_INDEX = {}
        return _PRICE_INDEX

    for _pid, prov in (catalog or {}).items():
        if not isinstance(prov, dict):
            continue
        for mid, mv in (prov.get("models") or {}).items():
            if not isinstance(mv, dict):
                continue
            cost = mv.get("cost")
            if not isinstance(cost, dict):
                continue
            if not (cost.get("input") or cost.get("output")):
                continue
            entry = {
                "input": float(cost.get("input") or 0),
                "output": float(cost.get("output") or 0),
                "cache_read": float(cost.get("cache_read") or 0),
                "cache_write": float(cost.get("cache_write") or 0),
                "context": (mv.get("limit") or {}).get("context"),
                "free": False,
            }
            index.setdefault(_normalize(mid), entry)
            index.setdefault(mid.lower(), entry)

    _PRICE_INDEX = index
    return index


def price_for(model: str) -> dict | None:
    if ":free" in (model or "").lower():
        return {"input": 0.0, "output": 0.0, "cache_read": 0.0,
                "cache_write": 0.0, "context": None, "free": True}
    index = load_pricing()
    key = _normalize(model)
    hit = index.get(key) or index.get((model or "").lower())
    if hit:
        return hit
    for k, v in index.items():  # dated snapshot of a known family
        if len(k) > 6 and key.startswith(k):
            return v
    return None


def cost_of(model: str, inp: int, out: int, cr: int, cw: int) -> tuple[float | None, bool]:
    """(usd, priced?). None when the catalog has no rate — never a silent 0."""
    p = price_for(model)
    if p is None:
        return None, False
    usd = ((inp or 0) * p["input"] + (out or 0) * p["output"]
           + (cr or 0) * p["cache_read"] + (cw or 0) * p["cache_write"]) / 1_000_000
    return usd, True


# ── Store access ─────────────────────────────────────────────────────────────

_STATE_CON: sqlite3.Connection | None = None


def _conn() -> sqlite3.Connection:
    """One reused read-only connection per process.

    Opening a SQLite connection on Windows costs ~5 ms while the queries
    themselves run in ~1 ms, so a per-query connect() made a 40 ms payload take
    236 ms. The connection is read-only and long-lived; a closed handle (after
    a fork or an interrupted read) is detected and reopened.
    """
    global _STATE_CON
    if _STATE_CON is None:
        con = sqlite3.connect(f"file:{state_db()}?mode=ro", uri=True,
                              timeout=5, check_same_thread=False)
        con.row_factory = sqlite3.Row
        _STATE_CON = con
    return _STATE_CON


def query(sql: str, params: tuple = ()) -> list[dict]:
    try:
        return [dict(r) for r in _conn().execute(sql, params).fetchall()]
    except sqlite3.ProgrammingError:
        # Handle closed underneath us — reopen once, then retry.
        global _STATE_CON
        _STATE_CON = None
        return [dict(r) for r in _conn().execute(sql, params).fetchall()]


def _attach_cost(rows: list[dict], model_key: str = "model") -> tuple[list[dict], int, int]:
    priced = total = 0
    for r in rows:
        tok = ((r.get("input") or 0) + (r.get("output") or 0)
               + (r.get("cache_read") or 0) + (r.get("cache_write") or 0))
        usd, ok = cost_of(r.get(model_key) or "", r.get("input") or 0, r.get("output") or 0,
                          r.get("cache_read") or 0, r.get("cache_write") or 0)
        r["cost_usd"] = usd
        r["priced"] = ok
        total += tok
        if ok:
            priced += tok
    return rows, priced, total


# ── Collectors ───────────────────────────────────────────────────────────────

def agent_sources() -> dict:
    """Cross-agent totals via agent_adapters, failing soft.

    The meter's own source is Hermes' state.db; other agents write their own
    logs. If the adapter module is missing or a log is unreadable, the widget
    still renders Hermes data and simply omits the cross-agent panel — it never
    invents a number for an agent it could not read.
    """
    try:
        import agent_adapters  # same directory
    except ImportError:
        here = Path(__file__).resolve().parent
        if str(here) not in sys.path:
            sys.path.insert(0, str(here))
        try:
            import agent_adapters
        except ImportError:
            return {}
    try:
        reports = agent_adapters.collect_all()
        return agent_adapters.aggregate(reports)
    except Exception:  # noqa: BLE001 — a broken log must not break the widget
        return {}


def collect(days: int = 30) -> dict:
    cutoff = time.time() - days * 86400

    daily = query("""
        SELECT date(started_at,'unixepoch','localtime') AS day,
               SUM(COALESCE(input_tokens,0))        AS input,
               SUM(COALESCE(output_tokens,0))       AS output,
               SUM(COALESCE(cache_read_tokens,0))   AS cache_read,
               SUM(COALESCE(cache_write_tokens,0))  AS cache_write,
               SUM(COALESCE(reasoning_tokens,0))    AS reasoning,
               COUNT(*)                             AS sessions,
               SUM(COALESCE(api_call_count,0))      AS calls
          FROM sessions WHERE started_at > ?
         GROUP BY day ORDER BY day
    """, (cutoff,))

    # Cost needs a per-(day, model) split, then folds into the day rows.
    day_model = query("""
        SELECT date(started_at,'unixepoch','localtime') AS day, model,
               SUM(COALESCE(input_tokens,0))       AS input,
               SUM(COALESCE(output_tokens,0))      AS output,
               SUM(COALESCE(cache_read_tokens,0))  AS cache_read,
               SUM(COALESCE(cache_write_tokens,0)) AS cache_write
          FROM sessions WHERE started_at > ? AND model IS NOT NULL
         GROUP BY day, model
    """, (cutoff,))
    day_model, _, _ = _attach_cost(day_model)
    by_day_cost: dict[str, float] = {}
    for r in day_model:
        if r["cost_usd"] is not None:
            by_day_cost[r["day"]] = by_day_cost.get(r["day"], 0.0) + r["cost_usd"]
    for d in daily:
        d["cost_usd"] = by_day_cost.get(d["day"])

    by_model = query("""
        SELECT model,
               SUM(COALESCE(input_tokens,0))       AS input,
               SUM(COALESCE(output_tokens,0))      AS output,
               SUM(COALESCE(cache_read_tokens,0))  AS cache_read,
               SUM(COALESCE(cache_write_tokens,0)) AS cache_write,
               COUNT(*)                            AS sessions,
               SUM(COALESCE(api_call_count,0))     AS calls
          FROM sessions
         WHERE started_at > ? AND model IS NOT NULL
         GROUP BY model
    """, (cutoff,))
    by_model, priced_tokens, total_tokens = _attach_cost(by_model)
    # Rank by the token volume the bars draw, so the data layer and the UI agree
    # and cost ordering (which can differ, e.g. a cheap high-volume model) does
    # not put a 3.3K row above a 358K one.
    by_model.sort(key=lambda r: ((r["input"] or 0) + (r["output"] or 0)), reverse=True)

    by_source = query("""
        SELECT COALESCE(source,'unknown') AS source,
               SUM(COALESCE(input_tokens,0))      AS input,
               SUM(COALESCE(output_tokens,0))     AS output,
               SUM(COALESCE(cache_read_tokens,0)) AS cache_read,
               COUNT(*)                           AS sessions
          FROM sessions WHERE started_at > ?
         GROUP BY source ORDER BY SUM(COALESCE(input_tokens,0)) DESC
    """, (cutoff,))

    top_sessions = query("""
        SELECT id, COALESCE(title,'') AS title, COALESCE(source,'') AS source,
               COALESCE(model,'') AS model, started_at, ended_at,
               COALESCE(input_tokens,0)       AS input,
               COALESCE(output_tokens,0)      AS output,
               COALESCE(cache_read_tokens,0)  AS cache_read,
               COALESCE(cache_write_tokens,0) AS cache_write,
               COALESCE(message_count,0)      AS messages
          FROM sessions WHERE started_at > ?
         ORDER BY COALESCE(input_tokens,0) + COALESCE(output_tokens,0) DESC
         LIMIT 10
    """, (cutoff,))
    top_sessions, _, _ = _attach_cost(top_sessions)

    task_rows = query("""
        SELECT u.task, u.model,
               SUM(u.input_tokens)  AS input,
               SUM(u.output_tokens) AS output,
               SUM(COALESCE(u.cache_read_tokens,0))  AS cache_read,
               SUM(COALESCE(u.cache_write_tokens,0)) AS cache_write,
               SUM(COALESCE(u.api_call_count,0))     AS calls
          FROM session_model_usage u
          JOIN sessions s ON s.id = u.session_id
         WHERE s.started_at > ? AND u.task != ''
         GROUP BY u.task, u.model
    """, (cutoff,))
    task_rows, _, _ = _attach_cost(task_rows)
    tasks: dict[str, dict] = {}
    for r in task_rows:
        t = tasks.setdefault(r["task"], {"task": r["task"], "input": 0, "output": 0,
                                         "cache_read": 0, "cache_write": 0,
                                         "calls": 0, "cost_usd": 0.0,
                                         "priced_tok": 0, "total_tok": 0})
        for k in ("input", "output", "cache_read", "cache_write", "calls"):
            t[k] += r.get(k) or 0
        tok = ((r.get("input") or 0) + (r.get("output") or 0)
               + (r.get("cache_read") or 0) + (r.get("cache_write") or 0))
        t["total_tok"] += tok
        if r["cost_usd"] is not None:
            t["cost_usd"] += r["cost_usd"]
            t["priced_tok"] += tok
    for t in tasks.values():
        # Three honest states, not two: fully priced, partially priced (show the
        # real number and flag it), or nothing priceable (show 'unpriced').
        if t["total_tok"] and t["priced_tok"] == t["total_tok"]:
            t["cost_state"] = "full"
        elif t["priced_tok"]:
            t["cost_state"] = "partial"
        else:
            t["cost_state"] = "none"
    by_task = sorted(tasks.values(), key=lambda r: r["total_tok"], reverse=True)

    tools = query("""
        SELECT tool_name AS name, COUNT(*) AS count
          FROM messages m JOIN sessions s ON s.id = m.session_id
         WHERE s.started_at > ? AND m.tool_name IS NOT NULL AND m.tool_name != ''
         GROUP BY tool_name ORDER BY count DESC LIMIT 8
    """, (cutoff,))

    totals_row = query("""
        SELECT COALESCE(SUM(COALESCE(input_tokens,0)),0)       AS input,
               COALESCE(SUM(COALESCE(output_tokens,0)),0)      AS output,
               COALESCE(SUM(COALESCE(cache_read_tokens,0)),0)  AS cache_read,
               COALESCE(SUM(COALESCE(cache_write_tokens,0)),0) AS cache_write,
               COALESCE(SUM(COALESCE(reasoning_tokens,0)),0)   AS reasoning,
               COUNT(*)                                        AS sessions,
               COALESCE(SUM(api_call_count),0)                 AS calls,
               COALESCE(SUM(tool_call_count),0)                AS tool_calls
          FROM sessions WHERE started_at > ?
    """, (cutoff,))[0]

    prompt = totals_row["input"] + totals_row["cache_read"] + totals_row["cache_write"]
    totals = {
        **totals_row,
        "prompt": prompt,
        "total": prompt + totals_row["output"],
        "cache_hit": round(100.0 * totals_row["cache_read"] / prompt, 1) if prompt else None,
        "cost_usd": sum((m["cost_usd"] or 0) for m in by_model),
        "priced_pct": round(100.0 * priced_tokens / total_tokens, 1) if total_tokens else 100.0,
    }

    return {
        "generated_at": time.time(),
        "days": days,
        "totals": totals,
        "daily": daily,
        "by_model": by_model,
        "by_source": by_source,
        "by_task": by_task,
        "top_sessions": top_sessions,
        "tools": tools,
        "live": live_session(),
        "budget": budget_state(),
        "burn": burn_rate(),
        "agents": agent_sources(),
    }


def live_session() -> dict | None:
    """The most recently active session that has not ended.

    The rate shown is this session's AVERAGE (tokens / elapsed), labelled as
    such — a single cumulative counter cannot yield an instantaneous rate.
    """
    rows = query("""
        SELECT id, COALESCE(title,'') AS title, COALESCE(model,'') AS model,
               started_at, last_activity_at,
               COALESCE(input_tokens,0)       AS input,
               COALESCE(output_tokens,0)      AS output,
               COALESCE(cache_read_tokens,0)  AS cache_read,
               COALESCE(cache_write_tokens,0) AS cache_write,
               COALESCE(api_call_count,0)     AS calls
          FROM sessions
         WHERE ended_at IS NULL AND COALESCE(last_activity_at, started_at) > ?
         ORDER BY COALESCE(last_activity_at, started_at) DESC LIMIT 1
    """, (time.time() - 7200,))
    if not rows:
        return None
    r = rows[0]
    usd, ok = cost_of(r["model"], r["input"], r["output"], r["cache_read"], r["cache_write"])
    last = r["last_activity_at"] or r["started_at"]
    elapsed = max(1.0, last - r["started_at"])
    toks = r["input"] + r["output"] + r["cache_read"] + r["cache_write"]
    return {**r, "cost_usd": usd, "priced": ok, "tokens": toks,
            "elapsed_min": elapsed / 60, "avg_tok_per_min": toks / (elapsed / 60),
            "idle_s": time.time() - last}


def burn_rate() -> dict:
    """Tokens/min measured from OUR ledger's day deltas, not session totals.

    A session's token counters are cumulative for its whole life — reading them
    as "last hour" activity projects absurd numbers (a long session touched once
    reports its entire history as fresh burn). Instead we take the delta between
    the last two samples of today's running total, which is the only honest
    instantaneous rate available without per-call instrumentation.
    """
    now = time.time()
    today = datetime.now().strftime("%Y-%m-%d")

    # Today's running total from the store (cheap, one scan).
    row = query("""
        SELECT COALESCE(SUM(COALESCE(input_tokens,0) + COALESCE(output_tokens,0)
                            + COALESCE(cache_read_tokens,0)),0) AS tok,
               COALESCE(SUM(COALESCE(api_call_count,0)),0)     AS calls
          FROM sessions WHERE date(started_at,'unixepoch','localtime') = ?
    """, (today,))
    cur_tok = (row[0]["tok"] or 0) if row else 0
    cur_calls = (row[0]["calls"] or 0) if row else 0

    # Previous sample, from our own ledger (survives restarts, cheap). Skip
    # samples that are too recent: a refresh only seconds after the last one
    # yields a window too short to measure, which would make the label flip
    # between "idle" and "measuring" on every reload.
    prev = None
    try:
        con = sqlite3.connect(f"file:{ledger_path()}?mode=ro", uri=True, timeout=3)
        con.row_factory = sqlite3.Row
        try:
            prev = con.execute(
                "SELECT sampled_at, tokens, calls FROM samples "
                "WHERE sampled_at <= ? ORDER BY sampled_at DESC LIMIT 1",
                (now - 5,),
            ).fetchone()
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        prev = None

    rate = None
    if prev:
        dt = now - float(prev["sampled_at"])
        if 5 <= dt <= 900:  # a usable window, not a stale sample
            d_tok = max(0, cur_tok - int(prev["tokens"] or 0))
            rate = d_tok / (dt / 60.0)

    return {
        "tok_today": cur_tok,
        "calls_today": cur_calls,
        "tok_per_min": rate,
        "projected_day": (rate * 60 * 24) if rate is not None else None,
        "window_s": (now - float(prev["sampled_at"])) if prev else None,
    }


def record_sample(payload: dict) -> None:
    """Append a (sampled_at, tokens, calls) row to the ledger's sample table.

    Bounded to the last 500 rows so the file cannot grow without limit. This is
    what makes burn_rate() honest across restarts.
    """
    try:
        con = sqlite3.connect(ledger_path(), timeout=5)
        try:
            con.execute("""
                CREATE TABLE IF NOT EXISTS samples (
                    sampled_at REAL PRIMARY KEY,
                    tokens INTEGER NOT NULL DEFAULT 0,
                    calls INTEGER NOT NULL DEFAULT 0,
                    cost_usd REAL
                )""")
            today = datetime.now().strftime("%Y-%m-%d")
            d = next((x for x in (payload.get("daily") or []) if x["day"] == today), None)
            tok = (d["input"] + d["output"] + d["cache_read"]) if d else 0
            con.execute(
                "INSERT OR REPLACE INTO samples (sampled_at, tokens, calls, cost_usd) VALUES (?,?,?,?)",
                (time.time(), tok, (d["calls"] if d else 0), (d["cost_usd"] if d else None)),
            )
            con.execute(
                "DELETE FROM samples WHERE sampled_at < "
                "(SELECT sampled_at FROM samples ORDER BY sampled_at DESC LIMIT 1 OFFSET 500)"
            )
            con.commit()
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        pass


def budget_state() -> dict:
    cfg = load_config()
    today = datetime.now().strftime("%Y-%m-%d")
    month = datetime.now().strftime("%Y-%m")

    # Per-(day, model) split is needed to price a day correctly, because each
    # model carries its own rates. It is the widest query in the payload, so it
    # is deliberately limited to the budget window (32d) rather than --days.
    rows = query("""
        SELECT date(started_at,'unixepoch','localtime') AS day,
               COALESCE(model,'') AS model,
               SUM(COALESCE(input_tokens,0))       AS input,
               SUM(COALESCE(output_tokens,0))      AS output,
               SUM(COALESCE(cache_read_tokens,0))  AS cache_read,
               SUM(COALESCE(cache_write_tokens,0)) AS cache_write
          FROM sessions WHERE started_at > ?
         GROUP BY day, model
    """, (time.time() - 32 * 86400,))
    rows, _, _ = _attach_cost(rows)

    spent_today = sum(r["cost_usd"] or 0 for r in rows if r["day"] == today)
    spent_month = sum(r["cost_usd"] or 0 for r in rows if (r["day"] or "").startswith(month))

    def pct(spent, cap):
        return round(100.0 * spent / cap, 1) if cap else None

    d_cap, m_cap = cfg.get("daily_usd"), cfg.get("monthly_usd")
    state = {
        "spent_today": spent_today, "spent_month": spent_month,
        "daily_usd": d_cap, "monthly_usd": m_cap,
        "daily_pct": pct(spent_today, d_cap), "monthly_pct": pct(spent_month, m_cap),
        "alert_at_pct": cfg.get("alert_at_pct", 80),
        "currency": cfg.get("currency", "$"),
        "alerts": [],
    }

    # One alert per period: tracked in a small state file so a refresh loop
    # cannot spam. Keyed by date so it re-arms tomorrow.
    alert_file = hermes_home() / f".{APP}-alert.json"
    try:
        fired = json.loads(alert_file.read_text(encoding="utf-8"))
        if not isinstance(fired, dict):
            fired = {}
    except (OSError, ValueError):
        fired = {}

    msgs = []
    for key, spent, cap, label in (("daily", spent_today, d_cap, "Today"),
                                    ("monthly", spent_month, m_cap, "This month")):
        if not cap:
            continue
        p = 100.0 * spent / cap
        stamp = today if key == "daily" else month
        if p >= 100 and fired.get(key + "_over") != stamp:
            msgs.append(f"{label} spend is over budget — {spent:.2f} of {cap:.2f}")
            fired[key + "_over"] = stamp
        elif p >= state["alert_at_pct"] and fired.get(key + "_warn") != stamp:
            msgs.append(f"{label} spend is at {p:.0f}% of budget — {spent:.2f} of {cap:.2f}")
            fired[key + "_warn"] = stamp

    if msgs:
        try:
            alert_file.write_text(json.dumps(fired), encoding="utf-8")
        except OSError:
            pass
    state["alerts"] = msgs
    return state


# ── Durable ledger ───────────────────────────────────────────────────────────

def rollup(payload: dict) -> None:
    """Upsert the day series into our own db.

    Hermes prunes sessions; this ledger is the durable record of what was spent
    on which day. Idempotent — re-running replaces the day's row.
    """
    try:
        con = sqlite3.connect(ledger_path(), timeout=5)
        try:
            con.execute("""
                CREATE TABLE IF NOT EXISTS daily (
                    day TEXT PRIMARY KEY,
                    input INTEGER NOT NULL DEFAULT 0,
                    output INTEGER NOT NULL DEFAULT 0,
                    cache_read INTEGER NOT NULL DEFAULT 0,
                    cache_write INTEGER NOT NULL DEFAULT 0,
                    sessions INTEGER NOT NULL DEFAULT 0,
                    api_calls INTEGER NOT NULL DEFAULT 0,
                    cost_usd REAL,
                    updated_at REAL NOT NULL
                )""")
            now = time.time()
            for d in payload.get("daily") or []:
                con.execute("""
                    INSERT INTO daily (day, input, output, cache_read, cache_write,
                                       sessions, api_calls, cost_usd, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(day) DO UPDATE SET
                      input=excluded.input, output=excluded.output,
                      cache_read=excluded.cache_read, cache_write=excluded.cache_write,
                      sessions=excluded.sessions, api_calls=excluded.api_calls,
                      cost_usd=excluded.cost_usd, updated_at=excluded.updated_at
                """, (d["day"], d["input"] or 0, d["output"] or 0, d["cache_read"] or 0,
                      d["cache_write"] or 0, d["sessions"] or 0, d["calls"] or 0,
                      d["cost_usd"], now))
            con.commit()
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        pass  # convenience only; never break the dashboard for the ledger


# ── Formatting ───────────────────────────────────────────────────────────────

def plural(n, word: str) -> str:
    """'1 session' / '3 sessions' — never '1 sessions'."""
    n = int(n or 0)
    return f"{n} {word}" + ("" if n == 1 else "s")


def compact(n) -> str:
    """Compact magnitude. Never rounds a positive value down to '0'."""
    v = float(n or 0)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"{v / div:.1f}".rstrip("0").rstrip(".") + unit
    if v == int(v):
        return str(int(v))
    if abs(v) >= 1:
        return f"{v:.1f}".rstrip("0").rstrip(".")
    # Sub-1 values (rates, tiny counts) keep a decimal so they don't read as 0.
    return f"{v:.2f}".rstrip("0").rstrip(".")


def money(v, cur: str = "$") -> str:
    """One system: 2 decimals from 1c up, '<1c' below, no 4-decimal soup."""
    if v is None:
        return "—"
    if v == 0:
        return f"{cur}0"
    if v < 0.01:
        # Escaped: a bare '<' inside HTML text is parsed as a tag start.
        return f"&lt;{cur}0.01"
    if v < 10:
        return f"{cur}{v:.2f}"
    return f"{cur}{v:,.0f}"


def ago(ts: float | None) -> str:
    if not ts:
        return ""
    s = time.time() - ts
    if s < 60:
        return f"{int(s)}s ago"
    if s < 3600:
        return f"{int(s / 60)}m ago"
    if s < 86400:
        return f"{int(s / 3600)}h ago"
    return f"{int(s / 86400)}d ago"


# ── Page ─────────────────────────────────────────────────────────────────────

PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Token Meter</title>
<style>
  :root{--bg:#0b0d10;--card:#14181d;--line:#232a33;--fg:#e6edf3;--dim:#9aa4ae;
        --faint:#78828d;--accent:#4a84fe;--ok:#3fb950;--warn:#d29922;--bad:#f85149}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:13px/1.45 -apple-system,"Segoe UI",system-ui,sans-serif;
       -webkit-font-smoothing:antialiased}
  .wrap{padding:15px 17px 20px}
  header{display:flex;align-items:baseline;gap:9px;margin-bottom:12px}
  h1{font-size:15px;margin:0;font-weight:650;letter-spacing:-.01em}
  .sub{color:var(--faint);font-size:10.5px;margin-left:auto}
  .alert{background:rgba(248,81,73,.1);border:1px solid rgba(248,81,73,.4);
         color:#ffb3ae;border-radius:7px;padding:8px 10px;font-size:11.5px;
         margin-bottom:11px;line-height:1.55}
  .alert b{color:#ffd7d4;display:block;margin-bottom:2px}
  .gauge{background:var(--card);border:1px solid var(--line);border-radius:8px;
         padding:11px 12px;margin-bottom:11px}
  .gauge .top{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:5px}
  .gauge .name{color:var(--faint);font-size:9.5px;letter-spacing:.07em;text-transform:uppercase}
  .gauge .amt{font:600 12.5px ui-monospace,Consolas,monospace}
  .gauge .amt small{color:var(--faint);font-weight:400;font-size:10.5px}
  .bar{height:5px;background:#1d232b;border-radius:3px;overflow:hidden;margin-bottom:9px}
  .bar i{display:block;height:100%;border-radius:3px;transition:width .5s ease}
  .live{background:linear-gradient(180deg,rgba(74,132,254,.09),transparent);
        border:1px solid rgba(74,132,254,.3);border-radius:8px;
        padding:10px 12px;margin-bottom:11px}
  .live .hdr{display:flex;align-items:center;gap:6px;margin-bottom:7px}
  .dot{width:6px;height:6px;border-radius:50%;background:var(--ok);
       box-shadow:0 0 0 3px rgba(63,185,80,.16);flex:none}
  .live .ti{font-size:11.5px;font-weight:600;overflow:hidden;
            text-overflow:ellipsis;white-space:nowrap}
  .live .meta{color:var(--faint);font-size:9.5px;margin-left:auto;white-space:nowrap}
  .nums{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}
  .nums .v{font:600 12.5px ui-monospace,Consolas,monospace}
  .nums .k{color:var(--faint);font-size:9px;text-transform:lowercase}
  .grid{display:grid;grid-template-columns:repeat(4,1fr);gap:7px;margin-bottom:12px}
  .stat{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:9px 10px}
  .stat .v{font:600 18px/1.15 ui-monospace,Consolas,monospace;letter-spacing:-.02em}
  .stat .k{color:var(--dim);font-size:10px;margin-top:2px;text-transform:lowercase}
  .stat .s{color:var(--faint);font-size:9px;margin-top:1px;white-space:nowrap;
           overflow:hidden;text-overflow:ellipsis}
  .stat.hi .v{color:var(--accent)}
  .stat.money .v{color:var(--ok)}
  section{background:var(--card);border:1px solid var(--line);border-radius:8px;
          padding:11px 12px;margin-bottom:9px}
  .lbl{color:var(--faint);font-size:9.5px;letter-spacing:.07em;
       text-transform:uppercase;margin-bottom:8px;display:flex;justify-content:space-between}
  .lbl span{letter-spacing:0;text-transform:none;color:var(--faint)}
  .unpriced{font-style:normal;color:var(--warn);background:rgba(210,153,34,.13);
            border:1px solid rgba(210,153,34,.35);border-radius:3px;
            padding:0 4px;font-size:9px;letter-spacing:.02em;margin-left:3px}
  .bars{display:flex;align-items:flex-end;gap:2px;height:42px;margin-bottom:5px}
  .bars i{flex:1;background:var(--accent);border-radius:2px 2px 0 0;
          min-height:2px;transition:height .4s ease}
  .bars i:hover{background:#6b9bff}
  .axis{display:flex;justify-content:space-between;color:var(--faint);
        font-size:8.5px;margin:-2px 0 6px;letter-spacing:.02em}
  .row{display:grid;grid-template-columns:1fr auto;gap:7px;align-items:baseline;
       margin-bottom:2px;font-size:11.5px}
  .row .n{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .row .t{font:500 11px ui-monospace,Consolas,monospace;color:var(--dim);white-space:nowrap}
  .row .t b{color:var(--fg);font-weight:600}
  .row .t i{color:#39424d;font-style:normal}
  .track{grid-column:1/-1;height:3px;background:#1d232b;border-radius:2px;
         overflow:hidden;margin-bottom:7px}
  .track i{display:block;height:100%;border-radius:2px;transition:width .4s ease}
  .meta{color:var(--faint);font-size:9px;margin:-5px 0 8px}
  .sess{display:grid;grid-template-columns:1fr auto;gap:6px;font-size:11px;
        padding:4px 0;border-top:1px solid #1a2028;align-items:baseline}
  .sess:first-of-type{border-top:0}
  .sess .ti{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--dim)}
  .sess .t{font:500 10.5px ui-monospace,Consolas,monospace;color:var(--faint);
           white-space:nowrap;text-align:right}
  footer{color:var(--faint);font-size:9px;margin-top:11px;
         display:flex;justify-content:space-between;align-items:center;gap:8px}
  footer .note{color:#4a535d}
  footer .acts{display:flex;gap:6px}
  footer a{color:var(--dim);text-decoration:none;border:1px solid var(--line);
           border-radius:4px;padding:2px 7px;font-size:9px;transition:all .15s}
  footer a:hover{color:var(--fg);border-color:#3a4552;background:#1a2028}
</style></head>
<body><div class="wrap">

  <header><h1>Token Meter</h1><span class="sub">last __DAYS__d · __UPDATED__</span></header>

  __ALERTS__
  __BUDGET__
  __LIVE__

  <div class="grid">
    <div class="stat"><div class="v">__COST__</div><div class="k">est. spend</div>
      <div class="s">__COVERAGE__</div></div>
    <div class="stat hi"><div class="v">__TOTAL__</div><div class="k">total tokens</div>
      <div class="s">__INPUT__ in · __OUTPUT__ out</div></div>
    <div class="stat hi"><div class="v">__CACHE_HIT__</div><div class="k">cache hit</div>
      <div class="s">__CACHE_READ__ cached</div></div>
    <div class="stat"><div class="v">__SESSIONS__</div><div class="k">sessions</div>
      <div class="s">__CALLS__ api calls</div></div>
  </div>

  <section>
    <div class="lbl">__CHART_TITLE__ <span>__BURN__</span></div>
    <div class="bars">__BARS__</div>
    <div class="axis"><span>__FIRST_DAY__</span><span>__LAST_DAY__</span></div>
    <div class="meta">__TODAY__</div>
    <div class="meta" style="margin-top:2px">__PEAK__</div>
  </section>

  __AGENTS__

  <section><div class="lbl">cost by model <span>list price</span></div>__MODELS__</section>
  <section><div class="lbl">by surface</div>__SOURCES__</section>
  <section><div class="lbl">background work</div>__TASKS__</section>
  <section><div class="lbl">heaviest sessions</div>__SESSIONS_LIST__</section>
  <section><div class="lbl">most-used tools</div>__TOOLS__</section>

  <footer>
    <span class="note">read-only · __DBNAME__</span>
    <span class="acts"><a href="/export.csv">CSV</a><a href="/export.json">JSON</a></span>
  </footer>
</div>
<script>setTimeout(() => location.reload(), 10000)</script>
</body></html>
"""


def _bar_rows(rows, name_key, val_fn, cost_fn=None, meta_fn=None, cur="$",
              cost_state_fn=None):
    """Ranked rows with a value bar.

    Sorts by the metric actually drawn (val_fn) so the bars are monotonic —
    a list ordered by cost but barred by tokens reads as broken. Zero-value
    rows are dropped; non-zero values get a floor width so a small number
    never looks like zero.

    cost_state_fn returns 'full' | 'partial' | 'none' so a partially-priced row
    shows its real cost with a marker instead of being hidden or overclaimed.
    """
    rows = [r for r in rows if (val_fn(r) or 0) > 0]
    rows.sort(key=val_fn, reverse=True)
    top = max((val_fn(r) for r in rows), default=1) or 1
    out = []
    for r in rows[:6]:
        v = val_fn(r)
        pct = max(4, round(100 * v / top))
        right = f"<b>{compact(v)}</b>"
        if cost_fn is not None:
            c = cost_fn(r)
            state = cost_state_fn(r) if cost_state_fn else ("full" if c is not None else "none")
            if state == "none":
                right += ' <em class="unpriced">unpriced</em>'
            elif state == "partial":
                # A zero here means "priced at $0" (a free model), which is a
                # real answer — say 'free' rather than printing nothing.
                right += (f' <i>·</i> {money(c, cur)}<em class="unpriced">part</em>'
                          if c else ' <i>·</i> free<em class="unpriced">part</em>')
            elif c and c > 0:
                right += f' <i>·</i> {money(c, cur)}'
            elif c == 0:
                right += ' <i>·</i> free'
        out.append(
            f'<div class="row"><span class="n">{html.escape(str(r.get(name_key) or "—"))}</span>'
            f'<span class="t">{right}</span>'
            f'<span class="track"><i style="width:{pct}%;background:var(--accent)"></i></span></div>'
        )
        if meta_fn:
            out.append(f'<div class="meta">{meta_fn(r)}</div>')
    return "".join(out) or '<div class="meta">no data</div>'


def render(payload: dict) -> str:
    t = payload["totals"]
    daily = payload["daily"]
    cur = payload["budget"].get("currency", "$")

    alerts = payload["budget"].get("alerts") or []
    alerts_html = (
        '<div class="alert"><b>Budget alert</b>'
        + "<br>".join(html.escape(a) for a in alerts) + "</div>"
        if alerts else ""
    )

    b = payload["budget"]
    gauge = []
    for label, spent, cap, pct in (
        ("today", b["spent_today"], b["daily_usd"], b["daily_pct"]),
        ("this month", b["spent_month"], b["monthly_usd"], b["monthly_pct"]),
    ):
        if not cap:
            gauge.append(
                f'<div class="top"><span class="name">{label}</span>'
                f'<span class="amt">{money(spent, cur)} '
                f'<small>· no cap</small></span></div>'
            )
            continue
        # Colour is a status, not decoration: blue while comfortable, amber on
        # approach, red only once the cap is actually breached.
        if pct >= 100:
            color = "var(--bad)"
        elif pct >= b["alert_at_pct"]:
            color = "var(--warn)"
        else:
            color = "var(--accent)"
        gauge.append(
            f'<div class="top"><span class="name">{label}</span>'
            f'<span class="amt">{money(spent, cur)} '
            f'<small>of {money(cap, cur)} · {pct:.0f}%</small></span></div>'
            f'<div class="bar"><i style="width:{min(100, pct or 0)}%;background:{color}"></i></div>'
        )
    budget_html = f'<div class="gauge">{"".join(gauge)}</div>'

    live = payload.get("live")
    if live:
        usd = money(live["cost_usd"], cur) if live["priced"] else "—"
        live_html = (
            '<div class="live"><div class="hdr"><span class="dot"></span>'
            f'<span class="ti">{html.escape((live["title"] or live["id"])[:50])}</span>'
            f'<span class="meta">{html.escape(live["model"][:22])} · {ago(live["last_activity_at"])}</span>'
            '</div><div class="nums">'
            f'<div><div class="v">{compact(live["tokens"])}</div><div class="k">tokens</div></div>'
            f'<div><div class="v">{usd}</div><div class="k">est cost</div></div>'
            f'<div><div class="v">{compact(live["avg_tok_per_min"])}</div>'
            '<div class="k">avg tok/min</div></div>'
            f'<div><div class="v">{live["calls"]}</div><div class="k">api calls</div></div>'
            "</div></div>"
        )
    else:
        live_html = ""

    peak = max(((d["input"] or 0) + (d["output"] or 0)) for d in daily) if daily else 1
    bars = "".join(
        f'<i style="height:{max(3, round(100 * (((d["input"] or 0) + (d["output"] or 0)) / peak)))}%"'
        f' title="{d["day"]}: {compact((d["input"] or 0) + (d["output"] or 0))} tokens'
        f'{" · " + money(d["cost_usd"], cur) if d.get("cost_usd") is not None else ""}"></i>'
        for d in daily[-30:]
    ) or "<i></i>"

    today = daily[-1] if daily else None
    today_txt = (
        f'today · {compact((today["input"] or 0) + (today["output"] or 0))} tokens · '
        f'{money(today["cost_usd"], cur)} · {today["sessions"]} sessions'
        if today else "no activity yet"
    )

    # Burn needs two samples to exist. Until then say so — never print 0.
    burn = payload["burn"]
    rate = burn.get("tok_per_min")

    # The bars are per-day totals over the whole window, so the label says
    # that; the rate beside it is a separate, clearly-scoped measurement.
    chart_title = f"daily tokens · {plural(len(daily), 'day')} of history"
    if rate is None:
        burn_txt = "measuring…" if burn.get("tok_today") else "no activity today"
        burn_sub = (f'{compact(burn.get("tok_today") or 0)} tokens today · '
                    "rate appears after two refreshes")
    elif rate <= 0:
        # A flat window is idle, not "0/min projected to 0/day" — that reads
        # like a broken widget.
        burn_txt = "idle"
        burn_sub = (f'{compact(burn.get("tok_today") or 0)} tokens today · '
                    f'{plural(burn.get("calls_today"), "call")} · no new tokens in '
                    f'{int(burn.get("window_s") or 0)}s')
    else:
        burn_txt = f'{compact(rate)}/min · proj {compact(burn["projected_day"])}/day'
        burn_sub = (f'{compact(burn.get("tok_today") or 0)} tokens today · '
                    f'{plural(burn.get("calls_today"), "call")} · window {int(burn.get("window_s") or 0)}s')

    models = _bar_rows(
        payload["by_model"], "model",
        lambda r: (r["input"] or 0) + (r["output"] or 0),
        cost_fn=lambda r: r["cost_usd"] if r["priced"] else None,
        meta_fn=lambda r: (plural(r["sessions"], "session") + " · "
                           + plural(r["calls"], "call")
                           + ("" if r["priced"] else " · no list price")),
        cur=cur,
    )
    sources = _bar_rows(payload["by_source"], "source",
                        lambda r: (r["input"] or 0) + (r["output"] or 0),
                        meta_fn=lambda r: plural(r["sessions"], "session"))
    tasks = _bar_rows(payload["by_task"], "task",
                      lambda r: r["total_tok"],
                      cost_fn=lambda r: (r["cost_usd"] if r.get("cost_state") == "full"
                                         else None if r.get("cost_state") == "none"
                                         else r["cost_usd"]),
                      cost_state_fn=lambda r: r.get("cost_state", "full"),
                      meta_fn=lambda r: plural(r["calls"], "call"),
                      cur=cur)
    tools = _bar_rows(payload["tools"], "name", lambda r: r["count"] or 0,
                      meta_fn=lambda r: plural(r["count"], "call"))

    # Sessions with no tokens are noise in a "heaviest" ranking.
    ranked = [s for s in payload["top_sessions"]
              if ((s["input"] or 0) + (s["output"] or 0)) > 0]
    sessions_list = "".join(
        f'<div class="sess"><span class="ti">{html.escape((s["title"] or s["id"])[:58])}</span>'
        f'<span class="t">{compact((s["input"] or 0) + (s["output"] or 0))}'
        f'{" · " + money(s["cost_usd"], cur) if s["priced"] and s["cost_usd"] else ""}</span></div>'
        for s in ranked[:8]
    ) or '<div class="meta">no sessions</div>'

    # ── cross-agent panel ──
    # Only shown when at least one other agent yielded data. Sources that
    # exist but expose no counters are listed as unavailable, never as zero.
    agents = payload.get("agents") or {}
    srcs = agents.get("sources") or {}
    agents_html = ""
    if srcs:
        rows = []
        order = ["hermes", "claude-code", "codex", "vscode", "cursor"]
        names = sorted(srcs, key=lambda s: (order.index(s) if s in order else 99, s))
        readable = {s: v for s, v in srcs.items() if v.get("status") in ("ok", "partial") and v.get("total")}
        top = max((v["total"] for v in readable.values()), default=1) or 1
        for s in names:
            v = srcs[s]
            label = {"hermes": "Hermes", "claude-code": "Claude Code",
                     "codex": "Codex", "vscode": "VS Code", "cursor": "Cursor"}.get(s, s)
            if s in readable:
                pct = max(4, round(100 * v["total"] / top))
                right = f'<b>{compact(v["total"])}</b>'
                rows.append(
                    f'<div class="row"><span class="n">{html.escape(label)}</span>'
                    f'<span class="t">{right}</span>'
                    f'<span class="track"><i style="width:{pct}%;background:var(--accent)"></i></span></div>'
                    f'<div class="meta">{plural(v.get("records"), "record")} · {html.escape(v.get("detail", ""))}</div>'
                )
            else:
                rows.append(
                    f'<div class="row"><span class="n" style="color:var(--faint)">'
                    f'{html.escape(label)}</span>'
                    f'<span class="t"><em class="unpriced">'
                    f'{"not found" if v.get("status") == "absent" else "no token log"}'
                    f'</em></span></div>'
                    f'<div class="meta">{html.escape(v.get("detail", ""))}</div>'
                )
        grand = agents.get("grand_total") or {}
        agents_html = (
            f'<section><div class="lbl">all agents '
            f'<span>total {compact(grand.get("total"))}</span></div>'
            + "".join(rows) + "</section>"
        )

    subs = {
        "__DAYS__": str(payload["days"]),
        "__UPDATED__": time.strftime("%H:%M:%S", time.localtime(payload["generated_at"])),
        "__ALERTS__": alerts_html,
        "__BUDGET__": budget_html,
        "__LIVE__": live_html,
        # With no pricing catalog every model is unpriced, and printing "$0"
        # would claim these tokens were free. Say so instead.
        "__COST__": (money(t["cost_usd"], cur) if t["priced_pct"] > 0 else "n/a"),
        "__COVERAGE__": (f'{t["priced_pct"]:.0f}% priced' if t["priced_pct"] > 0
                         else "no pricing catalog"),
        "__TOTAL__": compact(t["total"]),
        "__INPUT__": compact(t["input"]),
        "__OUTPUT__": compact(t["output"]),
        "__CACHE_HIT__": f'{t["cache_hit"]}%' if t["cache_hit"] is not None else "—",
        "__CACHE_READ__": compact(t["cache_read"]),
        "__SESSIONS__": str(t["sessions"]),
        "__CALLS__": str(t["calls"]),
        "__BURN__": burn_txt,
        "__CHART_TITLE__": chart_title,
        "__BARS__": bars,
        "__FIRST_DAY__": (daily[-30:][0]["day"][5:] if daily else ""),
        "__LAST_DAY__": (daily[-1]["day"][5:] if daily else ""),
        "__TODAY__": burn_sub,
        "__PEAK__": (f'peak {compact(peak)} · bars are per-day totals, hover for exact' if daily else ""),
        "__AGENTS__": agents_html,
        "__MODELS__": models,
        "__SOURCES__": sources,
        "__TASKS__": tasks,
        "__SESSIONS_LIST__": sessions_list,
        "__TOOLS__": tools,
        "__DBNAME__": state_db().name,
    }
    out = PAGE
    for k, v in subs.items():
        out = out.replace(k, v)
    return out


def export_rows(payload: dict) -> list[dict]:
    rows = []
    for d in payload["daily"]:
        rows.append({
            "scope": "day", "key": d["day"],
            "input_tokens": d["input"], "output_tokens": d["output"],
            "cache_read_tokens": d["cache_read"], "cache_write_tokens": d["cache_write"],
            "sessions": d["sessions"], "api_calls": d["calls"],
            "cost_usd": round(d["cost_usd"], 6) if d["cost_usd"] is not None else "",
        })
    for m in payload["by_model"]:
        rows.append({
            "scope": "model", "key": m["model"],
            "input_tokens": m["input"], "output_tokens": m["output"],
            "cache_read_tokens": m["cache_read"], "cache_write_tokens": m["cache_write"],
            "sessions": m["sessions"], "api_calls": m["calls"],
            "cost_usd": round(m["cost_usd"], 6) if m["priced"] else "",
        })
    return rows


# ── Server ───────────────────────────────────────────────────────────────────

def health(payload: dict) -> dict:
    """Machine-readable readiness. `ok` means the meter can be trusted.

    A source being `unavailable` is not a failure of the meter — it is an
    honest report about that agent. The meter is unhealthy only when it cannot
    read its OWN store or the totals stop adding up.
    """
    t = payload["totals"]
    agents = (payload.get("agents") or {}).get("sources") or {}
    problems = []

    if not state_db().exists():
        problems.append("state.db missing")
    if t["total"] <= 0:
        problems.append("no tokens in window")
    if t["total"] != t["prompt"] + t["output"]:
        problems.append("total does not reconcile")
    if not (0 <= t["priced_pct"] <= 100):
        problems.append("coverage out of range")

    return {
        "ok": not problems,
        "problems": problems,
        "store": {"path": state_db().name, "readable": state_db().exists()},
        "totals": {"tokens": t["total"], "cost_usd": round(t["cost_usd"], 4),
                   "coverage_pct": t["priced_pct"]},
        "agents": {k: v.get("status") for k, v in agents.items()},
        "generated_at": payload["generated_at"],
    }


class Handler(BaseHTTPRequestHandler):
    days = 30

    def _send(self, body: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        try:
            payload = collect(self.days)
            rollup(payload)
            record_sample(payload)
        except Exception as exc:  # noqa: BLE001 — show the failure, don't hang
            err = (
                "<html><body style='background:#0b0d10;color:#e6edf3;"
                "font:13px system-ui;padding:24px'>"
                "<h2 style='color:#f85149;font-size:15px'>Token Meter could not read the store</h2>"
                f"<pre style='color:#8b949e;white-space:pre-wrap'>{html.escape(str(exc))}</pre>"
                f"<p style='color:#5c6670'>Looked for: {html.escape(str(state_db()))}</p>"
                "</body></html>"
            ).encode("utf-8")
            self._send(err, "text/html; charset=utf-8", 500)
            return

        if path == "/health":
            # A supervisor must be able to distinguish "process alive" from
            # "serving correct data", so health re-runs the collectors and
            # reports per-source state rather than just 200.
            body = json.dumps(health(payload), indent=2).encode("utf-8")
            status = 200 if body and json.loads(body)["ok"] else 503
            self._send(body, "application/json", status)
            return

        if path == "/export.json":
            body = json.dumps({"generated_at": payload["generated_at"],
                               "days": payload["days"],
                               "totals": payload["totals"],
                               "rows": export_rows(payload)}, indent=2).encode("utf-8")
            self._send(body, "application/json")
            return

        if path == "/export.csv":
            buf = io.StringIO()
            rows = export_rows(payload)
            if rows:
                w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
            self._send(buf.getvalue().encode("utf-8"), "text/csv")
            return

        self._send(render(payload).encode("utf-8"), "text/html; charset=utf-8")

    def log_message(self, *args):
        pass


class Server(ThreadingHTTPServer):
    """Single-owner server.

    On Windows, SO_REUSEADDR lets several processes bind the same port, so a
    stale instance keeps answering alongside the new one and the browser shows
    a mix of old and new markup. Refuse to share the port instead.
    """
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def free_port(preferred: int = 0) -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", preferred))
        return s.getsockname()[1]


def open_app_window(url: str) -> bool:
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    for exe in (pf + r"\Google\Chrome\Application\chrome.exe",
                pf86 + r"\Google\Chrome\Application\chrome.exe",
                pf86 + r"\Microsoft\Edge\Application\msedge.exe",
                pf + r"\Microsoft\Edge\Application\msedge.exe"):
        if Path(exe).exists():
            profile = Path(os.environ.get("LOCALAPPDATA", ".")) / "hermes-token-meter" / "browser"
            try:
                subprocess.Popen(
                    [exe, f"--app={url}", f"--user-data-dir={profile}",
                     "--window-size=430,940", "--no-first-run",
                     "--no-default-browser-check"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                return True
            except OSError:
                continue
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Token Meter v2 — token & cost monitor")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--set-budget", nargs="+", metavar="KEY=VAL",
                    help="persist caps, e.g. daily=5 monthly=50 alert=80")
    args = ap.parse_args()

    if args.set_budget:
        cfg = load_config()
        alias = {"daily": "daily_usd", "monthly": "monthly_usd", "alert": "alert_at_pct"}
        for pair in args.set_budget:
            if "=" not in pair:
                print(f"ignoring {pair!r} (expected key=value)", file=sys.stderr)
                continue
            k, v = pair.split("=", 1)
            k = alias.get(k.strip(), k.strip())
            if k not in DEFAULT_CONFIG:
                print(f"unknown key {k!r} — use daily, monthly, alert", file=sys.stderr)
                continue
            try:
                cfg[k] = v if k == "currency" else float(v)
            except ValueError:
                print(f"bad value for {k}: {v!r}", file=sys.stderr)
                continue
        save_config(cfg)
        print(f"saved → {config_path()}")
        for k in ("daily_usd", "monthly_usd", "alert_at_pct"):
            print(f"  {k}: {cfg[k]}")
        return 0

    if not state_db().exists():
        print(f"No session store at {state_db()}", file=sys.stderr)
        return 1

    Handler.days = args.days
    port = args.port or free_port()

    # Warm everything the first request would otherwise pay for: the 6 MB
    # pricing catalog (~200 ms) and the cross-agent log cache (a cold parse of
    # a large Claude Code transcript can take seconds). Best effort — a
    # warmup failure must never stop the server from starting.
    try:
        load_pricing()
        _conn()
        agent_sources()
    except Exception:  # noqa: BLE001
        pass

    try:
        server = Server(("127.0.0.1", port), Handler)
    except OSError as exc:
        print(f"Port {port} is already in use ({exc.strerror or exc}).", file=sys.stderr)
        print("Another Token Meter is probably running — close it, or pick "
              "another port with --port.", file=sys.stderr)
        return 2
    url = f"http://127.0.0.1:{port}/"

    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Token Meter  →  {url}   (Ctrl+C to stop)")

    cfg = load_config()
    if cfg["daily_usd"] or cfg["monthly_usd"]:
        print(f"  budget: daily={cfg['daily_usd']} monthly={cfg['monthly_usd']}")
    else:
        print("  no budget set — try: --set-budget daily=5 monthly=50")

    if not args.no_open and not open_app_window(url):
        webbrowser.open(url)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopped")
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
