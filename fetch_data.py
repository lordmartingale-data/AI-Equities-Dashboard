#!/usr/bin/env python3
"""
Reads the ticker tree in tickers.json, pulls ~2y of daily bars for every symbol
in it, and writes data.json.

Standard library only - nothing to pip install, so the GitHub Action stays fast
and can't break when a dependency updates.

Sources, tried in order per symbol:
  1. Stooq - no key, no rate limit, US listings only
  2. Yahoo - no key, unofficial endpoint, covers foreign listings (.KS, .T)

A symbol can set "stooq": false to skip straight to Yahoo, and "yahoo": "005930.KS"
to override the symbol used there. Per-symbol failures are caught and recorded;
one dead ticker never kills the run.

Usage:
    python fetch_data.py            # normal run, writes data.json
    python fetch_data.py --check    # report which symbols resolve, write nothing
"""

import csv
import io
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

CONFIG_FILE = "tickers.json"
OUTPUT_FILE = "data.json"

# Calendar-day lookbacks, so "7D" means seven actual days ago, not seven sessions.
PERIODS = {"1d": 1, "7d": 7, "14d": 14, "30d": 30, "1y": 365}

# Yahoo symbols for FX, used to convert foreign market caps into USD.
FX_PAIRS = {"KRW": "KRW=X", "JPY": "JPY=X", "EUR": "EUR=X", "TWD": "TWD=X"}

USER_AGENT = "Mozilla/5.0 (compatible; equity-dashboard/1.0)"
TIMEOUT = 25


# ----------------------------------------------------------------- fetching


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def from_stooq(symbol):
    """[(date, close, volume), ...] oldest first, or None."""
    text = _get(f"https://stooq.com/q/d/l/?s={symbol.lower()}.us&i=d")
    if not text.strip() or text.strip().lower().startswith("<"):
        return None
    bars = []
    for row in csv.DictReader(io.StringIO(text)):
        date, close = row.get("Date"), row.get("Close")
        if not date or not close or close in ("N/A", "-"):
            continue
        try:
            vol = float(row.get("Volume") or 0)
            bars.append((date, float(close), vol))
        except ValueError:
            continue
    return bars or None


def from_yahoo(symbol):
    """[(date, close, volume), ...] oldest first, or None."""
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{urllib.parse.quote(symbol)}?range=2y&interval=1d"
    )
    payload = json.loads(_get(url))
    result = payload.get("chart", {}).get("result")
    if not result:
        return None
    node = result[0]
    stamps = node.get("timestamp") or []
    quote = node.get("indicators", {}).get("quote", [{}])[0]
    closes = quote.get("close") or []
    vols = quote.get("volume") or [None] * len(closes)
    bars = []
    for ts, close, vol in zip(stamps, closes, vols):
        if close is None:
            continue
        date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        bars.append((date, float(close), float(vol or 0)))
    return bars or None


def fetch_bars(entry):
    """entry is a dict from tickers.json. Returns (bars, source, error)."""
    symbol = entry["symbol"]
    plan = []
    if entry.get("stooq", True):
        plan.append(("stooq", from_stooq, symbol))
    plan.append(("yahoo", from_yahoo, entry.get("yahoo", symbol)))

    errors = []
    for label, fn, sym in plan:
        try:
            bars = fn(sym)
            if bars and len(bars) >= 2:
                return bars, label, None
            errors.append(f"{label}({sym}): empty")
        except Exception as exc:
            errors.append(f"{label}({sym}): {type(exc).__name__}")
        time.sleep(0.4)
    return None, None, "; ".join(errors)


def fetch_fx():
    """USD value of one unit of each foreign currency."""
    rates = {"USD": 1.0}
    for code, sym in FX_PAIRS.items():
        try:
            bars = from_yahoo(sym)
            if bars:
                # Yahoo quotes these as units-per-USD, so invert.
                per_usd = bars[-1][1]
                if per_usd > 0:
                    rates[code] = 1.0 / per_usd
        except Exception:
            pass
        time.sleep(0.3)
    return rates


# ---------------------------------------------------------------- metrics


def pct(new, old):
    if old is None or new is None or old == 0:
        return None
    return (new / old - 1.0) * 100.0


def close_on_or_before(dates, closes, target):
    """Last close at or before target date. None if history doesn't reach back."""
    if not dates or dates[0] > target:
        return None
    lo, hi, found = 0, len(dates) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if dates[mid] <= target:
            found = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return closes[found] if found is not None else None


def returns(dates, closes):
    """Percent return over each calendar-day period."""
    last_date = datetime.strptime(dates[-1], "%Y-%m-%d").date()
    last = closes[-1]
    out = {}
    for label, days in PERIODS.items():
        if label == "1d":
            out[label] = pct(last, closes[-2]) if len(closes) > 1 else None
            continue
        target = (last_date - timedelta(days=days)).strftime("%Y-%m-%d")
        out[label] = pct(last, close_on_or_before(dates, closes, target))
    return out


def downsample(values, target=70):
    if len(values) <= target:
        return values
    step = len(values) / target
    return [values[int(i * step)] for i in range(target)]


def build_row(entry, bars, source, fx, bench_returns):
    dates = [b[0] for b in bars]
    closes = [b[1] for b in bars]
    vols = [b[2] for b in bars]
    last = closes[-1]
    currency = entry.get("currency", "USD")

    rets = returns(dates, closes)

    # Relative return vs the benchmark, per period.
    rel = {}
    for label in PERIODS:
        a, b = rets.get(label), bench_returns.get(label)
        rel[label] = (a - b) if (a is not None and b is not None) else None

    shares_m = entry.get("shares_m")
    rate = fx.get(currency)
    mcap_usd = None
    if shares_m and rate:
        mcap_usd = last * shares_m * 1e6 * rate

    # Median volume over the last 20 sessions is a stabler read than the single
    # most recent session, which can be a half-day or a holiday.
    recent = sorted(v for v in vols[-20:] if v > 0)
    med_vol = recent[len(recent) // 2] if recent else None

    return {
        "symbol": entry["symbol"],
        "name": entry.get("name", entry["symbol"]),
        "status": "ok",
        "source": source,
        "currency": currency,
        "note": entry.get("note"),
        "last": round(last, 2),
        "as_of": dates[-1],
        "bars": len(closes),
        "ret": {k: (round(v, 2) if v is not None else None) for k, v in rets.items()},
        "rel": {k: (round(v, 2) if v is not None else None) for k, v in rel.items()},
        "volume": vols[-1] if vols else None,
        "volume_med": med_vol,
        "mcap": mcap_usd,
        "spark": [round(c, 2) for c in downsample(closes[-120:])],
    }


# ------------------------------------------------------------------- walk


def walk(node, fn):
    """Depth-first over the tree, calling fn on every ticker entry."""
    for entry in node.get("tickers", []):
        fn(entry)
    for child in node.get("children", []):
        walk(child, fn)


def main():
    check_only = "--check" in sys.argv

    with open(CONFIG_FILE) as fh:
        config = json.load(fh)

    entries = []
    walk(config["tree"], entries.append)
    print(f"{len(entries)} symbols in tree\n")

    print("fx:")
    fx = fetch_fx()
    for code, rate in sorted(fx.items()):
        print(f"  {code} = {rate:.6f} USD")

    # Benchmark first - every relative figure depends on it.
    bench_cfg = config["benchmark"]
    print("\nbenchmark:")
    bench_bars, bench_src, bench_err = fetch_bars(bench_cfg)
    if bench_bars:
        bench_rets = returns([b[0] for b in bench_bars], [b[1] for b in bench_bars])
        print(f"  ok    {bench_cfg['symbol']} via {bench_src}")
    else:
        bench_rets = {}
        print(f"  FAIL  {bench_cfg['symbol']} - {bench_err}")
        print("        relative column will be blank")

    print("\ntickers:")
    prices = {}
    failures = []
    for entry in entries:
        bars, source, error = fetch_bars(entry)
        if bars is None:
            print(f"  FAIL  {entry['symbol']:8s} {error}")
            failures.append(entry["symbol"])
            prices[entry["symbol"]] = {
                "symbol": entry["symbol"],
                "name": entry.get("name", entry["symbol"]),
                "status": "error",
                "error": error,
            }
            continue
        row = build_row(entry, bars, source, fx, bench_rets)
        mc = f"{row['mcap'] / 1e9:8.1f}B" if row["mcap"] else "       -"
        print(
            f"  ok    {entry['symbol']:8s} {row['last']:>10.2f} {row['currency']}"
            f" {mc}  {row['bars']:>4d} bars  via {source}"
        )
        prices[entry["symbol"]] = row
        time.sleep(0.3)

    if check_only:
        print(f"\n{len(failures)} failure(s): {', '.join(failures) or 'none'}")
        return 1 if failures else 0

    now = datetime.now(timezone.utc)
    payload = {
        "generated_utc": now.strftime("%Y-%m-%d %H:%M"),
        "generated_bkk": (now + timedelta(hours=7)).strftime("%Y-%m-%d %H:%M"),
        "benchmark": bench_cfg["symbol"],
        "bench_ret": {
            k: (round(v, 2) if v is not None else None) for k, v in bench_rets.items()
        },
        "fx": fx,
        "tree": config["tree"],
        "prices": prices,
    }

    with open(OUTPUT_FILE, "w") as fh:
        json.dump(payload, fh, indent=1)

    print(f"\nWrote {OUTPUT_FILE} ({len(failures)} failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
