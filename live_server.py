"""Local real-time server for the AI Black Hole page.

Polls free sources on their own schedules, keeps the latest state in memory,
and streams it to the page over Server-Sent Events. Standard library only.

  python live_server.py            # http://127.0.0.1:8787
  python live_server.py --lan      # also reachable from other devices on your network

Feeds: Yahoo Finance chart API (unofficial, delayed quotes), ERCOT and CAISO public
dashboards (5-minute), EIA-930 hourly demand (EIA_API_KEY env var or DEMO_KEY), and the
daily snapshot Claude writes to live/latest_snapshot.json (FRED, drought, news, analysis).
"""
import argparse
import copy
import csv
import io
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE / "ai_black_hole.html"
SNAPSHOT = HERE / "live" / "latest_snapshot.json"
SAMPLE = HERE / "sample" / "snapshot.json"  # used until a daily snapshot exists
TICKERS = {"NVDA": "nvidia", "ORCL": "cloud", "CRWV": "cloud", "MSFT": "bigtech", "GOOGL": "bigtech", "AMZN": "bigtech", "META": "bigtech",
           "SPCX": "spacex", "TSLA": "tesla", "AAPL": "consumer", "WMT": "retail", "SHOP": "retail", "PDD": "retail",
           "GM": "autos", "F": "autos", "TM": "autos", "NKE": "fashion", "LVMUY": "fashion",
           "JPM": "banks", "MUFG": "banks", "APO": "banks", "BX": "banks",
           "ETSY": "retail", "EBAY": "retail", "BABA": "retail",
           "TSM": "chips", "ASML": "chips", "AVGO": "chips", "AMD": "chips", "INTC": "chips",
           "XOM": "power", "CVX": "power", "NEE": "power", "CEG": "power", "GEV": "power",
           "UNH": "healthco", "LLY": "healthco", "JNJ": "healthco", "BA": "industrials", "CAT": "industrials", "LMT": "industrials",
           "V": "payments", "MA": "payments", "PYPL": "payments"}
# The rest of the Nasdaq-100 and the QQQ funds: quoted, but no intraday history or move alerts (keeps the feed small)
NDX_EXTRA = {"MU": "nvidia", "SNDK": "nvidia", "WDC": "nvidia", "STX": "nvidia", "ALAB": "nvidia", "LITE": "nvidia", "ARM": "nvidia", "MRVL": "nvidia", "AMAT": "chips", "LRCX": "chips", "KLAC": "chips", "TER": "chips", "QCOM": "chips", "TXN": "chips", "ADI": "chips", "MCHP": "chips", "NXPI": "chips", "MPWR": "chips", "SNPS": "chips", "CDNS": "chips", "PLTR": "labs", "APP": "labs", "ADBE": "labs", "INTU": "labs", "ADSK": "labs", "WDAY": "labs", "TRI": "labs", "NBIS": "cloud", "CSCO": "cloud", "DDOG": "cloud", "CRWD": "cloud", "PANW": "cloud", "FTNT": "cloud", "ROP": "cloud", "GOOG": "bigtech", "RKLB": "spacex", "NFLX": "consumer", "CMCSA": "consumer", "TMUS": "consumer", "TTWO": "consumer", "BKNG": "consumer", "ABNB": "consumer", "MAR": "consumer", "SBUX": "consumer", "COST": "retail", "MELI": "retail", "ROST": "retail", "DASH": "retail", "PEP": "retail", "MDLZ": "retail", "KDP": "retail", "MNST": "retail", "CCEP": "retail", "ORLY": "autos", "CPRT": "autos", "PCAR": "autos", "AMGN": "healthco", "GILD": "healthco", "VRTX": "healthco", "REGN": "healthco", "ALNY": "healthco", "MRNA": "healthco", "ISRG": "healthco", "DXCM": "healthco", "IDXX": "healthco", "GEHC": "healthco", "HON": "industrials", "HONA": "industrials", "LIN": "industrials", "CTAS": "industrials", "FAST": "industrials", "ODFL": "industrials", "CSX": "industrials", "AXON": "industrials", "FER": "industrials", "AEP": "power", "XEL": "power", "EXC": "power", "FANG": "power", "BKR": "power", "ADP": "payments", "PAYX": "payments", "MSTR": "payments", "QQQ": "ndx", "TQQQ": "ndx", "SQQQ": "ndx"}
ALL_TICKERS = {**TICKERS, **NDX_EXTRA}
FULL_DETAIL = set(TICKERS) | {"QQQ", "TQQQ", "SQQQ"}
UA = {"User-Agent": "Mozilla/5.0 (ai-black-hole-local)"}
EIA_HOURLY = ("https://api.eia.gov/v2/electricity/rto/region-data/data/?api_key={key}&frequency=hourly&data[0]=value"
              "&facets[respondent][]=PJM&facets[respondent][]=ERCO&facets[respondent][]=SRP&facets[respondent][]=US48&facets[type][]=D"
              "&start={start}&sort[0][column]=period&sort[0][direction]=desc&length=500")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch(url, timeout=25):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


class Hub:
    def __init__(self):
        self.cond = threading.Condition()
        self.state = {"quotes": None, "ercot": None, "caiso": None, "eiaHourly": None, "snapshot": None}
        self.status = {}
        self.events = {}
        self.version = 0
        self.payload = "{}"

    def update(self, key, value=None, error=None):
        with self.cond:
            if error is None:
                self.state[key] = value
                self.status[key] = {"ok": True, "updated": now_iso()}
            else:
                prev = self.status.get(key, {})
                self.status[key] = {"ok": False, "updated": prev.get("updated"), "error": str(error)[:200]}
            self._detect_events()
            self.version += 1
            self.payload = json.dumps(self._compose(), separators=(",", ":"))
            self.cond.notify_all()

    def _event(self, eid, title, node):
        if eid not in self.events:
            self.events[eid] = {"id": eid, "time": now_iso(), "title": title, "node": node}
            if len(self.events) > 60:
                for k in sorted(self.events, key=lambda k: self.events[k]["time"])[:-60]:
                    del self.events[k]

    def _detect_events(self):
        day = datetime.now(timezone.utc).date().isoformat()
        for sym, q in (self.state["quotes"] or {}).items():
            ch = q.get("dayChange")
            if sym in TICKERS and ch is not None and abs(ch) >= 0.03:
                self._event(f"{sym}-{day}-{'up' if ch > 0 else 'down'}",
                            f"{sym} is {'up' if ch > 0 else 'down'} {abs(ch):.1%} today at ${q['price']:.2f}", TICKERS[sym])
        er = self.state["ercot"] or {}
        if er.get("price") is not None and er["price"] >= 200:
            self._event(f"ercot-price-{day}-{er.get('priceTime', '')[:13]}",
                        f"ERCOT real-time power price spiked to ${er['price']:.0f}/MWh", "citizens")
        if er.get("reserve") is not None and er["reserve"] < 0.10:
            self._event(f"ercot-tight-{day}", f"ERCOT reserves are tight: only {er['reserve']:.0%} spare capacity", "citizens")

    def _compose(self):
        snap = copy.deepcopy(self.state["snapshot"]) if self.state["snapshot"] else None
        quotes = self.state["quotes"] or {}
        if snap is not None and quotes:
            snap["stocks"] = {"date": now_iso()[:10], "source": "Yahoo Finance (unofficial, delayed)",
                              "quotes": [{"symbol": s, "price": q["price"], "high52": q.get("high52"), "fromHigh": q.get("fromHigh"), "dayChange": q.get("dayChange")}
                                         for s, q in quotes.items()]}
        return {"snapshot": snap,
                "live": {"quotes": quotes, "ercot": self.state["ercot"], "caiso": self.state["caiso"],
                         "eiaHourly": self.state["eiaHourly"], "status": self.status,
                         "events": sorted(self.events.values(), key=lambda e: e["time"]), "serverTime": now_iso()}}


def _parse_chart(sym, r):
    """One Yahoo chart result (from /v8/chart or /v7/spark) -> our quote dict, plus whether the market is open."""
    m = r["meta"]
    price, prev, hi = m.get("regularMarketPrice"), m.get("chartPreviousClose") or m.get("previousClose"), m.get("fiftyTwoWeekHigh")
    if price is None:
        return None, False
    reg = (m.get("currentTradingPeriod") or {}).get("regular") or {}
    open_now = reg.get("start", 0) <= time.time() <= reg.get("end", 0)
    intraday = []
    if sym in FULL_DETAIL:
        closes = ((r.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        intraday = [[t, round(c, 2)] for t, c in zip(r.get("timestamp") or [], closes) if c is not None][-80:]
    return {"symbol": sym, "node": ALL_TICKERS[sym], "price": price, "prevClose": prev,
            "dayChange": round(price / prev - 1, 4) if price and prev else None,
            "high52": hi, "fromHigh": round(price / hi - 1, 4) if price and hi else None,
            "marketTime": datetime.fromtimestamp(m.get("regularMarketTime", 0), timezone.utc).isoformat(timespec="seconds"),
            "intraday": intraday}, open_now


def _spark_batch(syms):
    """Up to 20 symbols in one request (Yahoo's limit for /v7/finance/spark)."""
    url = "https://query1.finance.yahoo.com/v7/finance/spark?" + urllib.parse.urlencode(
        {"symbols": ",".join(syms), "range": "1d", "interval": "5m"})
    out = {}
    for item in (json.loads(fetch(url)).get("spark") or {}).get("result") or []:
        resp = item.get("response") or []
        if item.get("symbol") in ALL_TICKERS and resp:
            out[item["symbol"]] = resp[0]
    return out


def _chart_one(sym):
    data = json.loads(fetch(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range=1d&interval=5m"))
    res = (data.get("chart") or {}).get("result")
    return {sym: res[0]} if res else {}


def poll_quotes(hub):
    """All 130 symbols in ~7 batched requests run in parallel (about a second), with a per-symbol fallback.
    Polls every 30 seconds while the market is open (Yahoo quotes are delayed about 15 minutes anyway)."""
    syms = list(ALL_TICKERS)
    raw = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for fut in [pool.submit(_spark_batch, syms[i:i + 20]) for i in range(0, len(syms), 20)]:
            try:
                raw.update(fut.result())
            except Exception:
                pass  # that batch falls back to single-symbol requests below
        missing = [s for s in syms if s not in raw]
        for fut in [pool.submit(_chart_one, s) for s in missing]:
            try:
                raw.update(fut.result())
            except Exception:
                pass  # one bad symbol shouldn't drop the rest
    quotes, open_now = {}, False
    for sym, r in raw.items():
        try:
            q, is_open = _parse_chart(sym, r)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
        if q:
            quotes[sym] = q
            open_now = open_now or is_open
    if not quotes:
        raise RuntimeError("no quotes returned")
    hub.update("quotes", quotes)
    return 30 if open_now else 900


def poll_ercot(hub):
    sd = json.loads(fetch("https://www.ercot.com/api/1/services/read/dashboards/supply-demand.json"))
    actual = [d for d in sd.get("data", []) if not d.get("forecast") and d.get("demand")]
    pr = json.loads(fetch("https://www.ercot.com/api/1/services/read/dashboards/systemWidePrices.json"))
    rt = [p for p in pr.get("rtSppData", []) if p.get("hbHubAvg") is not None]
    last = actual[-1] if actual else {}
    hub.update("ercot", {
        "demand": last.get("demand"), "capacity": last.get("capacity"), "time": last.get("timestamp"),
        "reserve": round((last["capacity"] - last["demand"]) / last["capacity"], 4) if last.get("capacity") else None,
        "demandHist": [[d["timestamp"], round(d["demand"] / 1000, 2)] for d in actual[::3]][-96:],
        "price": rt[-1]["hbHubAvg"] if rt else None, "priceTime": rt[-1].get("timestamp", "") if rt else "",
        "priceHist": [[p.get("timestamp", p.get("intervalEnding")), p["hbHubAvg"]] for p in rt][-96:],
    })
    return 300


def poll_caiso(hub):
    rows = [r for r in csv.DictReader(io.StringIO(fetch("https://www.caiso.com/outlook/current/demand.csv")))
            if (r.get("Current demand") or "").strip()]
    hist = [[r["Time"], round(float(r["Current demand"]) / 1000, 2)] for r in rows]
    hub.update("caiso", {"demand": float(rows[-1]["Current demand"]) if rows else None,
                         "time": rows[-1]["Time"] if rows else None, "demandHist": hist[::3][-96:]})
    return 300


def poll_eia(hub):
    key = os.environ.get("EIA_API_KEY") or "DEMO_KEY"
    start = (datetime.now(timezone.utc) - timedelta(hours=60)).strftime("%Y-%m-%dT%H")
    rows = json.loads(fetch(EIA_HOURLY.format(key=key, start=start)))["response"]["data"]
    out = {}
    for r in sorted(rows, key=lambda r: r["period"]):
        try:
            out.setdefault(r["respondent"], []).append([r["period"], round(float(r["value"]) / 1000, 2)])
        except (TypeError, ValueError):
            continue
    hub.update("eiaHourly", {k: {"latest": v[-1][1], "time": v[-1][0], "hist": v[-48:]} for k, v in out.items()})
    # EIA's public DEMO_KEY allows about 50 calls a day per computer, shared with the daily snapshot;
    # with your own free key (EIA_API_KEY) poll every 15 minutes
    return 900 if os.environ.get("EIA_API_KEY") else 3600


def poll_snapshot(hub, _seen={"key": None}):
    src = SNAPSHOT if SNAPSHOT.exists() else SAMPLE
    if src.exists():
        key = (src, src.stat().st_mtime)
        if key != _seen["key"]:
            hub.update("snapshot", json.loads(src.read_text(encoding="utf-8")))
            _seen["key"] = key
    return 30


def run_poller(hub, name, fn):
    while True:
        try:
            wait = fn(hub)
        except Exception as exc:
            hub.update(name, error=exc)
            wait = 3600 if "429" in str(exc) else 120  # rate-limited: back off for an hour
        time.sleep(wait)


def make_handler(hub):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                html = PAGE.read_text(encoding="utf-8").replace(
                    "<title>", "<script>window.LIVE_SOURCE = '/events';</script>\n<title>", 1)
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/state.json":
                self._send(200, hub.payload.encode("utf-8"), "application/json")
            elif path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                last = -1
                try:
                    while True:
                        with hub.cond:
                            hub.cond.wait_for(lambda: hub.version != last, timeout=20)
                            ver, data = hub.version, hub.payload
                        if ver != last:
                            self.wfile.write(f"event: state\ndata: {data}\n\n".encode("utf-8"))
                            last = ver
                        else:
                            self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    return
            else:
                self._send(404, b"Not found", "text/plain")
    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("AIBH_PORT", "8787")))
    ap.add_argument("--lan", action="store_true", help="listen on all network interfaces, not just this computer")
    args = ap.parse_args()
    hub = Hub()
    for name, fn in (("snapshot", poll_snapshot), ("quotes", poll_quotes), ("ercot", poll_ercot),
                     ("caiso", poll_caiso), ("eiaHourly", poll_eia)):
        threading.Thread(target=run_poller, args=(hub, name, fn), daemon=True).start()
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    server = ThreadingHTTPServer((host, args.port), make_handler(hub))
    server.daemon_threads = True
    print(f"AI Black Hole live server on http://127.0.0.1:{args.port}/" + ("  (also on your local network)" if args.lan else ""))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
