"""Build one daily market snapshot for the AI Black Hole artifact.

Fetches FRED series (free FRED_API_KEY recommended; works without one, but FRED's
public CSV throttles), merges news events and optional stock quotes from JSON files,
and writes the snapshot document as JSON.

Usage:
  python market_snapshot.py --out snap.json [--events new_events.json]
                            [--prev previous_snapshot.json] [--stocks stocks.json]
                            [--analysis analysis.json]
"""
import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

SERIES = {
    "bbb": ("BAMLC0A4CBBB", "ICE BofA BBB US corporate bond spread (OAS), %"),
    "hy": ("BAMLH0A0HYM2", "ICE BofA US high-yield bond spread (OAS), %"),
    "t10": ("DGS10", "10-year US Treasury yield, %"),
}
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"
FRED_API = "https://api.stlouisfed.org/fred/series/observations?series_id={}&api_key={}&file_type=json"
EVENT_WINDOW_DAYS = 60
MAX_EVENTS = 25
HIST_POINTS = 120


def _fetch_series_once(series_id):
    key = os.environ.get("FRED_API_KEY")
    url = FRED_API.format(series_id, key) if key else FRED_CSV.format(series_id)
    req = urllib.request.Request(url, headers={"User-Agent": "ai-black-hole-snapshot/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        text = resp.read().decode("utf-8")
    rows = []
    if key:
        for o in json.loads(text).get("observations", []):
            try:
                rows.append((o["date"], float(o["value"])))
            except (KeyError, ValueError):
                continue  # FRED marks missing days with "."
    else:
        for row in csv.reader(io.StringIO(text)):
            if len(row) != 2 or row[0] == "observation_date":
                continue
            try:
                rows.append((row[0], float(row[1])))
            except ValueError:
                continue
    if not rows:
        raise RuntimeError(f"FRED returned no data for {series_id}")
    return rows


def fetch_series(series_id, tries=3):
    """FRED's official API when FRED_API_KEY is set (free, reliable), else the public CSV, which throttles."""
    for attempt in range(tries):
        try:
            return _fetch_series_once(series_id)
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(5 * (attempt + 1))


def summarize(rows, label, series_id):
    last_date, last_val = rows[-1]
    last_d = date.fromisoformat(last_date)
    year = [v for d, v in rows if date.fromisoformat(d) > last_d - timedelta(days=365)]
    month_ago = [v for d, v in rows if date.fromisoformat(d) <= last_d - timedelta(days=30)]
    return {
        "id": series_id,
        "label": label,
        "value": round(last_val, 3),
        "date": last_date,
        "avg1y": round(sum(year) / len(year), 3),
        "max1y": round(max(year), 3),
        "min1y": round(min(year), 3),
        "chg30": round(last_val - month_ago[-1], 3) if month_ago else None,
        "hist": [[d, v] for d, v in rows[-HIST_POINTS:]],
        "url": f"https://fred.stlouisfed.org/series/{series_id}",
    }


EIA_KEY_ENV = "EIA_API_KEY"
EIA_DAILY = ("https://api.eia.gov/v2/electricity/rto/daily-region-data/data/?api_key={key}&frequency=daily&data[0]=value"
             "&facets[respondent][]=PJM&facets[respondent][]=ERCO&facets[respondent][]=SRP&facets[respondent][]=US48&facets[type][]=D"
             "&facets[timezone][]=Eastern&start={start}&sort[0][column]=period&sort[0][direction]=desc&length=5000")
GRID_NAMES = {"PJM": "PJM (Mid-Atlantic, incl. Northern Virginia)", "ERCO": "ERCOT (Texas)", "SRP": "SRP (Phoenix area, Arizona)", "US48": "Lower 48 states"}
USDM_URL = ("https://usdmdataservices.unl.edu/api/CountyStatistics/GetDroughtSeverityStatisticsByAreaPercent"
            "?aoi={fips}&startdate={start}&enddate={end}&statisticsType=1")
DC_COUNTIES = [
    ("51107", "Loudoun, VA", "Data Center Alley"),
    ("51153", "Prince William, VA", "Northern Virginia expansion"),
    ("04013", "Maricopa, AZ", "Phoenix cluster"),
    ("48113", "Dallas, TX", "Dallas–Fort Worth cluster"),
    ("48441", "Taylor, TX", "Abilene Stargate campus"),
    ("39049", "Franklin, OH", "Columbus cluster"),
    ("13121", "Fulton, GA", "Atlanta cluster"),
    ("17031", "Cook, IL", "Chicago cluster"),
    ("06085", "Santa Clara, CA", "Silicon Valley"),
    ("55089", "Ozaukee, WI", "Port Washington campus (Oracle-leased)"),
    ("35013", "Doña Ana, NM", "Project Jupiter"),
]


def fetch_text(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "ai-black-hole-snapshot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def grid_summary(today):
    import os
    key = os.environ.get(EIA_KEY_ENV) or "DEMO_KEY"
    start = (today - timedelta(days=400)).isoformat()
    rows = json.loads(fetch_text(EIA_DAILY.format(key=key, start=start)))["response"]["data"]
    by = {}
    for r in rows:
        try:
            by.setdefault(r["respondent"], []).append((r["period"], float(r["value"]) / 24 / 1000))
        except (TypeError, ValueError, KeyError):
            continue
    out = {}
    for resp, pts in by.items():
        pts.sort()
        last_d = date.fromisoformat(pts[-1][0])
        recent = [v for d, v in pts if date.fromisoformat(d) > last_d - timedelta(days=28)]
        year_ago = [v for d, v in pts
                    if last_d - timedelta(days=393) < date.fromisoformat(d) <= last_d - timedelta(days=365)]
        avg28 = sum(recent) / len(recent)
        out[resp] = {
            "name": GRID_NAMES.get(resp, resp), "date": pts[-1][0], "unit": "GW (daily average)",
            "avg28": round(avg28, 2),
            "yoy": round(avg28 / (sum(year_ago) / len(year_ago)) - 1, 4) if year_ago else None,
            "hist": [[d, round(v, 2)] for d, v in pts[-90:]],
        }
    return out


def monthly_yoy(series_id, label):
    rows = fetch_series(series_id)
    last_date, last_val = rows[-1]
    ref = date.fromisoformat(last_date).replace(year=date.fromisoformat(last_date).year - 1).isoformat()
    prior = [v for d, v in rows if d <= ref]
    return {"id": series_id, "label": label, "value": last_val, "date": last_date,
            "yoy": round(last_val / prior[-1] - 1, 4) if prior else None,
            "hist": [[d, v] for d, v in rows[::-1][::step][:36][::-1]], "url": f"https://fred.stlouisfed.org/series/{series_id}"}


def county_drought(today):
    start = (today - timedelta(days=42)).strftime("%m/%d/%Y")
    end = today.strftime("%m/%d/%Y")
    out = []
    for fips, name, why in DC_COUNTIES:
        try:
            text = fetch_text(USDM_URL.format(fips=fips, start=start, end=end), timeout=30)
        except Exception as exc:
            out.append({"fips": fips, "name": name, "why": why, "error": str(exc)[:120]})
            continue
        rows = [r for r in csv.DictReader(io.StringIO(text)) if r.get("MapDate")]
        rows.sort(key=lambda r: r["MapDate"], reverse=True)
        if not rows:
            continue

        def stats(r):
            # statisticsType=1 values are cumulative (% of area in that category or worse),
            # so D1 is the share in drought and their sum is the Drought Severity and Coverage Index (0-500)
            d = [float(r[k]) for k in ("D0", "D1", "D2", "D3", "D4")]
            return round(d[1], 1), round(sum(d))

        drought, dsci = stats(rows[0])
        out.append({"fips": fips, "name": name, "why": why, "date": rows[0]["ValidStart"],
                    "droughtPct": drought, "dsci": dsci,
                    "dsci4wAgo": stats(rows[min(4, len(rows) - 1)])[1]})
    return out


SOCIETY = {
    "retail": ("RSAFS", "Retail and food services sales, $M per month"),
    "ecom": ("ECOMPCTSA", "E-commerce share of retail sales, %"),
    "vehicles": ("TOTALSA", "Light vehicle sales, millions per year (SAAR)"),
    "sentiment": ("UMCSENT", "University of Michigan consumer sentiment"),
    "studentLoans": ("SLOAS", "Student loans outstanding, $B"),
    "chinaImports": ("IMPCH", "US goods imports from China, $M per month"),
    "apparel": ("CPIAPPSL", "CPI: apparel"),
    "cardDelinq": ("DRCCLACBS", "Credit card delinquency rate, %"),
}


def year_change(series_id, label, step=1):
    """Latest value plus its change versus a year earlier (works for daily, weekly, monthly and quarterly series).
    step thins the history for daily or weekly series so it still covers a useful span."""
    rows = fetch_series(series_id)
    last_date, last_val = rows[-1]
    ref = (date.fromisoformat(last_date) - timedelta(days=360)).isoformat()
    prior = [v for d, v in rows if d <= ref]
    return {"id": series_id, "label": label, "value": last_val, "date": last_date,
            "yoy": round(last_val / prior[-1] - 1, 4) if prior and prior[-1] else None,
            "chg1y": round(last_val - prior[-1], 3) if prior else None,
            "hist": [[d, v] for d, v in rows[::-1][::step][:36][::-1]], "url": f"https://fred.stlouisfed.org/series/{series_id}"}


HOUSEHOLDS = {
    "wealthTop1": ("WFRBST01134", "Share of US net worth held by the top 1%, %"),
    "wealthTop01": ("WFRBSTP1300", "Share of US net worth held by the top 0.1%, %"),
    "wealthMid40": ("WFRBSN40188", "Share of US net worth held by the 50th-90th percentiles, %"),
    "wealthBottom50": ("WFRBSB50215", "Share of US net worth held by the bottom 50%, %"),
    "saving": ("PSAVERT", "Personal saving rate, %"),
    "income": ("DSPIC96", "Real disposable personal income, $B (2017 dollars, annual rate)"),
    "credit": ("TOTALSL", "Consumer credit outstanding, $M"),
    "cpiShelter": ("CUSR0000SAH1", "CPI: shelter"),
    "cpiFood": ("CPIUFDSL", "CPI: food"),
    "cpiEnergy": ("CPIENGSL", "CPI: energy"),
    "cpiMedical": ("CPIMEDSL", "CPI: medical care"),
    "cpiUsedCars": ("CUSR0000SETA02", "CPI: used cars and trucks"),
    "impMexico": ("IMPMX", "US goods imports from Mexico, $M per month"),
    "impVietnam": ("IMP5520", "US goods imports from Vietnam, $M per month"),
    "impCanada": ("IMPCA", "US goods imports from Canada, $M per month"),
    "customs": ("B235RC1Q027SBEA", "Federal customs duties collected, $B (annual rate)"),
    "freight": ("TSIFRGHT", "Freight transportation services index"),
}


FED = {
    "fedfunds": ("DFF", "Effective federal funds rate, %", 7),
    "targetUpper": ("DFEDTARU", "Federal funds target range, upper limit, %", 7),
    "targetLower": ("DFEDTARL", "Federal funds target range, lower limit, %", 7),
    "balance": ("WALCL", "Federal Reserve total assets, $M (weekly)", 2),
    "corePce": ("PCEPILFE", "Core PCE price index (the Fed's preferred inflation gauge)", 1),
    "mortgage": ("MORTGAGE30US", "30-year fixed mortgage rate, % (weekly)", 2),
    "unemployment": ("UNRATE", "Unemployment rate, %", 1),
}


def fed_section():
    out, errors = {}, []
    for key, (sid, label, step) in FED.items():
        try:
            out[key] = year_change(sid, label, step)
        except Exception as exc:
            errors.append(f"FRED {sid}: {exc}")
    out["errors"] = errors
    return out


def households_section():
    out, errors = {}, []
    for key, (sid, label) in HOUSEHOLDS.items():
        try:
            out[key] = year_change(sid, label)
        except Exception as exc:
            errors.append(f"FRED {sid}: {exc}")
    out["errors"] = errors
    return out


def society_section():
    out, errors = {}, []
    for key, (sid, label) in SOCIETY.items():
        try:
            out[key] = year_change(sid, label)
        except Exception as exc:
            errors.append(f"FRED {sid}: {exc}")
    out["errors"] = errors
    return out


def energy_section(today):
    errors = []
    section = {"asOf": today.isoformat(), "grid": None, "prices": {}, "water": None, "errors": errors}
    try:
        section["grid"] = grid_summary(today)
    except Exception as exc:
        errors.append(f"EIA grid demand: {exc}")
    for key, (sid, label, kind) in {
        "gas": ("DHHNGSP", "Henry Hub natural gas spot price, $/MMBtu", "daily"),
        "elecCpi": ("CUSR0000SEHF01", "CPI: electricity (index)", "monthly"),
        "kwh": ("APU000072610", "Average US residential electricity price, $/kWh", "monthly"),
    }.items():
        try:
            section["prices"][key] = summarize(fetch_series(sid), label, sid) if kind == "daily" else monthly_yoy(sid, label)
        except Exception as exc:
            errors.append(f"FRED {sid}: {exc}")
    try:
        counties = county_drought(today)
        ok = [c for c in counties if "droughtPct" in c]
        section["water"] = {"counties": counties,
                            "avgDroughtPct": round(sum(c["droughtPct"] for c in ok) / len(ok), 1) if ok else None,
                            "avgDsci": round(sum(c["dsci"] for c in ok) / len(ok)) if ok else None,
                            "source": "US Drought Monitor (droughtmonitor.unl.edu)"}
    except Exception as exc:
        errors.append(f"US Drought Monitor: {exc}")
    return section


def load_json(path):
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def merge_events(prev_events, new_events, today):
    cutoff = (today - timedelta(days=EVENT_WINDOW_DAYS)).isoformat()
    by_url = {}
    for e in (prev_events or []) + (new_events or []):
        if not e.get("url") or not e.get("date") or e["date"] < cutoff:
            continue
        by_url[e["url"]] = e
    return sorted(by_url.values(), key=lambda e: e["date"], reverse=True)[:MAX_EVENTS]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--events")
    ap.add_argument("--prev")
    ap.add_argument("--stocks")
    ap.add_argument("--analysis")
    args = ap.parse_args()

    today = datetime.now(timezone.utc).date()
    series = {key: summarize(fetch_series(sid), label, sid) for key, (sid, label) in SERIES.items()}
    prev = load_json(args.prev) or {}
    new_events = load_json(args.events) or []
    stocks = load_json(args.stocks) if args.stocks else None

    doc = {
        "date": today.isoformat(),
        "asOf": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "series": series,
        "stocks": stocks,
        "events": merge_events(prev.get("events"), new_events, today),
        "analysis": load_json(args.analysis) if args.analysis else prev.get("analysis"),
        "energy": energy_section(today),
        "society": society_section(),
        "households": households_section(),
        "fed": fed_section(),
        "sources": ["FRED (Federal Reserve Bank of St. Louis), ICE BofA indices",
                    "EIA Hourly Electric Grid Monitor (EIA-930) daily demand", "US Drought Monitor county statistics"]
                   + (["Alpha Vantage"] if stocks else [])
                   + (["News search"] if new_events or prev.get("events") else []),
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    s = series
    print(f"snapshot {doc['date']}: BBB {s['bbb']['value']}% (1y avg {s['bbb']['avg1y']}), "
          f"HY {s['hy']['value']}% (1y avg {s['hy']['avg1y']}), 10y {s['t10']['value']}%, "
          f"{len(doc['events'])} events, stocks {'yes' if stocks else 'no'}")
    en = doc["energy"]
    g = en.get("grid") or {}
    w = en.get("water") or {}
    print("energy: " + ", ".join(f"{k} {v['avg28']} GW ({v['yoy']:+.1%} y/y)" for k, v in g.items() if v.get("yoy") is not None)
          + f"; drought avg {w.get('avgDroughtPct')}% across {len(w.get('counties') or [])} data-center counties"
          + (f"; errors: {en['errors']}" if en["errors"] else ""))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"snapshot failed: {exc}", file=sys.stderr)
        sys.exit(1)
