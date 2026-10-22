#!/usr/bin/env python3
"""Token Meter — multi-agent adapters.

Hermes writes token usage to `state.db`. Other agents do not; they write their
own logs, each in its own format. This module normalises them into one shape so
the meter can report total spend across every agent on the machine.

Verified formats (see `--probe`):

  claude-code   ~/.claude/projects/<slug>/<uuid>.jsonl
                one JSON object per line; assistant records carry
                message.usage = {input_tokens, output_tokens,
                cache_read_input_tokens, cache_creation_input_tokens}
                and message.model. This is the richest of the three.

  codex         ~/.codex/sessions/**/rollout-*.jsonl (newer builds) or
                ~/.codex/history.jsonl. Event records with a token_count or
                usage payload; shapes differ between versions, so the parser
                is defensive and reports what it could not read.

  cursor / vscode
                No per-request token log on disk. VS Code's globalStorage has
                chat state but no usage counters. These are reported as
                UNAVAILABLE with a reason rather than as zero — a zero would
                read as "spent nothing", which is a different claim.

Design rules
------------
1. Never invent numbers. An unreadable source is `unavailable`, not 0.
2. Never double count. Each adapter declares a `source` tag that is carried
   through to the aggregate.
3. Never block. A malformed line is skipped; a missing dir is skipped.
4. Cheap re-reads. Files are tracked by (size, mtime) so an unchanged log is
   not re-parsed on every refresh.

Usage:
    python agent_adapters.py --probe          # what exists on this machine
    python agent_adapters.py --json           # aggregate usage by source
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path


# ── Common shape ─────────────────────────────────────────────────────────────

@dataclass
class Usage:
    """One normalised usage sample. All adapters produce these."""
    source: str
    model: str
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0
    ts: float = 0.0
    session: str = ""

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write


@dataclass
class SourceReport:
    """What an adapter found. `status` is honest about coverage."""
    source: str
    status: str                      # ok | partial | unavailable | absent
    detail: str = ""
    records: int = 0
    samples: list[Usage] = field(default_factory=list)

    @property
    def totals(self) -> dict:
        t = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0}
        for s in self.samples:
            for k in t:
                t[k] += getattr(s, k)
        t["total"] = t["input"] + t["output"] + t["cache_read"] + t["cache_write"]
        t["records"] = len(self.samples)
        return t


def _home() -> Path:
    return Path(os.path.expanduser("~"))


# ── Incremental file cache ───────────────────────────────────────────────────

_CACHE_PATH = None


def _cache_file() -> Path:
    global _CACHE_PATH
    if _CACHE_PATH is None:
        home = Path(os.environ.get("HERMES_HOME")
                    or (Path(os.environ["LOCALAPPDATA"]) / "hermes"
                        if os.environ.get("LOCALAPPDATA") else _home() / ".hermes"))
        _CACHE_PATH = home / "token_meter_adapters.json"
    return _CACHE_PATH


def _load_cache() -> dict:
    try:
        return json.loads(_cache_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        _cache_file().write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass


def _stamp(path: str) -> str:
    """Identity of a file's contents, cheap enough to check every refresh."""
    try:
        st = os.stat(path)
        return f"{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        return ""


# ── Claude Code ──────────────────────────────────────────────────────────────

def read_claude_code(limit_files: int = 200, use_cache: bool = True) -> SourceReport:
    """Parse ~/.claude/projects/*/*.jsonl.

    Verified on this machine: 3 files / 66K lines / 308 usage records /
    37.1M tokens. Only `assistant` records with a non-empty usage block count;
    zero-token usage blocks (streaming placeholders) are skipped so the record
    count reflects real API calls.

    Parsing 66K lines takes ~1.5 s, far too slow for a 10 s auto-refresh. Each
    file's extracted samples are cached against (size, mtime), so an unchanged
    transcript costs one stat() instead of a full re-parse.
    """
    root = _home() / ".claude" / "projects"
    if not root.is_dir():
        return SourceReport("claude-code", "absent", f"no {root}")

    files = sorted(glob.glob(str(root / "*" / "*.jsonl")))[-limit_files:]
    if not files:
        return SourceReport("claude-code", "absent", "no .jsonl under projects/")

    cache = _load_cache() if use_cache else {}
    cc_cache = cache.setdefault("claude-code", {})
    fresh: dict[str, dict] = {}
    samples: list[Usage] = []
    skipped = 0
    parsed = 0

    for path in files:
        stamp = _stamp(path)
        hit = cc_cache.get(path)
        if hit and hit.get("stamp") == stamp:
            rows = hit.get("samples", [])
        else:
            rows = _parse_claude_file(path, counters := {"skipped": 0})
            skipped += counters["skipped"]
            parsed += 1
        fresh[path] = {"stamp": stamp, "samples": rows}
        for r in rows:
            samples.append(Usage(source="claude-code", **r))

    if use_cache:
        cache["claude-code"] = fresh
        _save_cache(cache)

    status = "ok" if samples else "unavailable"
    detail = f"{len(files)} file(s), {len(samples)} usage records"
    if parsed:
        detail += f", {parsed} re-parsed"
    if skipped:
        detail += f", {skipped} unreadable"
    return SourceReport("claude-code", status, detail, len(samples), samples)


def _parse_claude_file(path: str, counters: dict) -> list[dict]:
    """Extract usage dicts from one transcript. Returns JSON-safe rows.

    A large share of records are streaming placeholders whose usage block is
    all zeros (one verified file was 77.8 MB / 60,067 such records and zero
    real tokens). The zero check is therefore a fast path, not an edge case:
    it is what keeps a big transcript from costing seconds per parse.
    """
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                # Cheap pre-filter: most lines are prompts/tool output.
                if '"usage"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    counters["skipped"] = counters.get("skipped", 0) + 1
                    continue
                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue
                u = msg.get("usage")
                if not isinstance(u, dict):
                    continue
                inp = u.get("input_tokens") or 0
                out = u.get("output_tokens") or 0
                cr = u.get("cache_read_input_tokens") or 0
                cw = u.get("cache_creation_input_tokens") or 0
                if inp + out + cr + cw == 0:
                    counters["placeholders"] = counters.get("placeholders", 0) + 1
                    continue
                thinking = ((u.get("output_tokens_details") or {})
                            .get("thinking_tokens") or 0)
                rows.append({
                    "model": msg.get("model") or "",
                    "input": inp, "output": out, "cache_read": cr,
                    "cache_write": cw, "reasoning": thinking,
                    "ts": _ts(rec.get("timestamp")),
                    "session": str(rec.get("sessionId") or ""),
                })
    except OSError:
        counters["skipped"] = counters.get("skipped", 0) + 1
    return rows


# ── Codex ────────────────────────────────────────────────────────────────────

def read_codex(limit_files: int = 200) -> SourceReport:
    """Parse Codex session rollouts.

    Codex has shipped several shapes: a `token_count` event, a `usage` object
    on a response event, and older `history.jsonl` entries with no usage at
    all. Rather than assume one, every JSON object on a line is inspected for
    any of the known shapes. What cannot be read is counted, not guessed.
    """
    root = _home() / ".codex"
    if not root.is_dir():
        return SourceReport("codex", "absent", f"no {root}")

    files = sorted(glob.glob(str(root / "sessions" / "**" / "*.jsonl"), recursive=True))
    files += sorted(glob.glob(str(root / "*.jsonl")))
    if not files:
        # Directory exists but holds no session logs (this machine's case).
        return SourceReport("codex", "unavailable",
                            "~/.codex present but no session .jsonl found")

    samples: list[Usage] = []
    unreadable = 0
    for path in files[-limit_files:]:
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if "token" not in line and "usage" not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        unreadable += 1
                        continue
                    s = _codex_sample(rec, path)
                    if s:
                        samples.append(s)
        except OSError:
            unreadable += 1

    if not samples:
        return SourceReport("codex", "unavailable",
                            f"{len(files)} file(s), no recognisable usage records")
    detail = f"{len(files)} file(s), {len(samples)} usage records"
    if unreadable:
        detail += f", {unreadable} unreadable"
    return SourceReport("codex", "ok" if not unreadable else "partial",
                        detail, len(samples), samples)


def _codex_sample(rec: dict, path: str) -> Usage | None:
    """Best-effort extraction across Codex's known event shapes."""
    for key in ("usage", "token_usage", "token_count"):
        u = rec.get(key)
        if isinstance(u, dict):
            inp = u.get("input_tokens") or u.get("prompt_tokens") or 0
            out = u.get("output_tokens") or u.get("completion_tokens") or 0
            if inp or out:
                return Usage(
                    source="codex", model=str(rec.get("model") or ""),
                    input=inp, output=out,
                    cache_read=u.get("cached_tokens") or u.get("cache_read_input_tokens") or 0,
                    ts=_ts(rec.get("timestamp") or rec.get("ts")),
                    session=Path(path).stem,
                )
    # Some builds nest it under payload/info.
    for key in ("payload", "info", "data"):
        inner = rec.get(key)
        if isinstance(inner, dict):
            got = _codex_sample(inner, path)
            if got:
                return got
    return None


# ── VS Code / Cursor ─────────────────────────────────────────────────────────

_VSCODE_PROBE: bool | None = None

def read_vscode_family() -> list[SourceReport]:
    """Report, honestly, that these expose no token counters on disk.

    VS Code stores chat sessions in globalStorage, but as conversation state
    with no per-request usage block. Cursor keeps usage server-side. Both are
    therefore UNAVAILABLE rather than zero — the meter must not claim these
    agents spent nothing.

    The globalStorage scan reads the head of up to 400 JSON files, so its
    answer is cached in-process: the presence of token counters there does not
    change between refreshes, and this check must not cost 100 ms per render.
    """
    global _VSCODE_PROBE
    out: list[SourceReport] = []

    gs = _home() / "AppData" / "Roaming" / "Code" / "User" / "globalStorage"
    if gs.is_dir():
        if _VSCODE_PROBE is None:
            _VSCODE_PROBE = _scan_for_usage(gs)
        out.append(SourceReport(
            "vscode", "unavailable",
            "globalStorage present; chat state carries no token counters"
            + (" (found a 'usage' key — needs a dedicated adapter)" if _VSCODE_PROBE else ""),
        ))
    else:
        out.append(SourceReport("vscode", "absent", f"no {gs}"))

    cur = _home() / ".cursor"
    out.append(SourceReport(
        "cursor",
        "unavailable" if cur.is_dir() else "absent",
        "usage is server-side; no local token log" if cur.is_dir() else f"no {cur}",
    ))
    return out


def _scan_for_usage(gs: Path, limit: int = 400) -> bool:
    for p in glob.glob(str(gs / "**" / "*.json"), recursive=True)[:limit]:
        try:
            if '"usage"' in Path(p).read_text(encoding="utf-8", errors="ignore")[:20000]:
                return True
        except OSError:
            continue
    return False


# ── Hermes (reference adapter) ───────────────────────────────────────────────

def read_hermes() -> SourceReport:
    """Hermes is the native source; it is read from state.db, not from logs.

    This adapter exists so every source reports through the same shape, and so
    `--probe` can show Hermes beside the others.
    """
    home = Path(os.environ.get("HERMES_HOME")
                or (Path(os.environ["LOCALAPPDATA"]) / "hermes"
                    if os.environ.get("LOCALAPPDATA") else _home() / ".hermes"))
    db = home / "state.db"
    if not db.exists():
        return SourceReport("hermes", "absent", f"no {db}")
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute("""
                SELECT model, SUM(input_tokens) i, SUM(output_tokens) o,
                       SUM(cache_read_tokens) cr, SUM(cache_write_tokens) cw,
                       SUM(reasoning_tokens) r, COUNT(*) n
                  FROM sessions GROUP BY model
            """).fetchall()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        return SourceReport("hermes", "unavailable", f"read failed: {exc}")

    samples = [Usage(source="hermes", model=r["model"] or "",
                     input=r["i"] or 0, output=r["o"] or 0,
                     cache_read=r["cr"] or 0, cache_write=r["cw"] or 0,
                     reasoning=r["r"] or 0)
               for r in rows]
    return SourceReport("hermes", "ok", f"{db.name}, {len(samples)} models",
                        len(samples), samples)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _ts(v) -> float:
    """Epoch seconds from an ISO-8601 string, epoch number, or None."""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def collect_all() -> list[SourceReport]:
    reports = [read_hermes(), read_claude_code(), read_codex()]
    reports.extend(read_vscode_family())
    return reports


def aggregate(reports: list[SourceReport]) -> dict:
    """Fold every available source into one total, keeping source tags."""
    by_source = {}
    grand = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
             "reasoning": 0, "total": 0}
    for r in reports:
        if r.status in ("ok", "partial") and r.samples:
            t = r.totals
            by_source[r.source] = {"status": r.status, "detail": r.detail, **t}
            for k in grand:
                grand[k] += t[k]
        else:
            by_source[r.source] = {"status": r.status, "detail": r.detail,
                                   "total": 0, "records": 0}
    return {"sources": by_source, "grand_total": grand}


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Token Meter — agent adapters")
    ap.add_argument("--probe", action="store_true", help="show what each source exposes")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    reports = collect_all()
    agg = aggregate(reports)

    if args.json:
        print(json.dumps(agg, indent=2))
        return 0

    icon = {"ok": "OK  ", "partial": "PART", "unavailable": "N/A ",
            "absent": "----"}
    print(f"{'source':<12} {'status':<5} {'records':>8} {'tokens':>10}  detail")
    print("-" * 78)
    for r in reports:
        t = r.totals
        print(f"{r.source:<12} {icon.get(r.status, r.status):<5} "
              f"{t['records']:>8} {_fmt(t['total']):>10}  {r.detail}")

    g = agg["grand_total"]
    print("-" * 78)
    print(f"{'TOTAL':<12} {'':<5} {'':>8} {_fmt(g['total']):>10}")
    print(f"\n  input {_fmt(g['input'])} · output {_fmt(g['output'])} · "
          f"cache_read {_fmt(g['cache_read'])} · reasoning {_fmt(g['reasoning'])}")

    missing = [r.source for r in reports if r.status in ("unavailable", "absent")]
    if missing:
        print(f"\n  Not counted (reported, not zeroed): {', '.join(missing)}")
    return 0


def _fmt(n) -> str:
    v = float(n or 0)
    for u, d in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= d:
            return f"{v / d:.1f}{u}"
    return str(int(v))


if __name__ == "__main__":
    raise SystemExit(main())
