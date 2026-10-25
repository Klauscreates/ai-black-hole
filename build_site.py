"""Build the hosted (GitHub Pages) version of the AI Black Hole into _site/.

Runs on a schedule in GitHub Actions (.github/workflows/live.yml), with no server:
  1. Start from the snapshot the site is already serving (its data.json), or sample/snapshot.json.
  2. On a full refresh (each morning, or a manual run), rebuild the daily snapshot with
     market_snapshot.py: FRED, the Fed, EIA grid, drought and economy data. News analysis and
     events carry forward; a newer data/analysis.json in the repo replaces the analysis.
  3. Add live data the same way live_server.py does: stock quotes, ERCOT, CAISO and EIA hourly.
  4. Write _site/data.json (which open pages check every 5 minutes) and _site/index.html.

  python build_site.py               # live data only
  FULL_REFRESH=true python build_site.py
"""
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import live_server as ls

HERE = Path(__file__).resolve().parent
SITE = HERE / "_site"
SAMPLE = HERE / "sample" / "snapshot.json"
ANALYSIS = HERE / "data" / "analysis.json"


def site_url():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if os.environ.get("SITE_URL"):
        return os.environ["SITE_URL"].rstrip("/") + "/"
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/"
    return None


def previous_snapshot():
    url = site_url()
    if url:
        try:
            with urllib.request.urlopen(url + "data.json", timeout=20) as resp:
                snap = json.loads(resp.read().decode("utf-8")).get("snapshot")
            if snap and snap.get("date"):
                return snap, "deployed site"
        except (OSError, ValueError) as exc:
            print(f"no deployed data.json yet ({exc}); using the sample")
    return json.loads(SAMPLE.read_text(encoding="utf-8")), "sample"


def full_refresh(prev):
    with tempfile.TemporaryDirectory() as tmp:
        prev_path, events_path, out_path = Path(tmp, "prev.json"), Path(tmp, "events.json"), Path(tmp, "snapshot.json")
        prev_path.write_text(json.dumps(prev), encoding="utf-8")
        events_path.write_text("[]", encoding="utf-8")
        cmd = [sys.executable, str(HERE / "market_snapshot.py"), "--out", str(out_path), "--prev", str(prev_path), "--events", str(events_path)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        print(res.stdout.strip())
        if res.returncode != 0:
            print(f"full refresh failed, keeping the previous snapshot: {res.stderr.strip()[-500:]}")
            return prev
        return json.loads(out_path.read_text(encoding="utf-8"))


def main():
    snap, origin = previous_snapshot()
    print(f"base snapshot {snap.get('date')} from {origin}")
    if os.environ.get("FULL_REFRESH", "").lower() == "true":
        snap = full_refresh(snap)
    if ANALYSIS.exists():
        an = json.loads(ANALYSIS.read_text(encoding="utf-8"))
        if str(an.get("asOf", "")) > str((snap.get("analysis") or {}).get("asOf", "")):
            snap["analysis"] = an
            print(f"analysis replaced with data/analysis.json ({an.get('asOf')})")

    hub = ls.Hub()
    hub.update("snapshot", snap)
    for name, fn in (("quotes", ls.poll_quotes), ("ercot", ls.poll_ercot), ("caiso", ls.poll_caiso), ("eiaHourly", ls.poll_eia)):
        try:
            fn(hub)
        except Exception as exc:
            hub.update(name, error=exc)
            print(f"{name} failed: {exc}")
    state = hub._compose()

    SITE.mkdir(exist_ok=True)
    (SITE / "data.json").write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (SITE / ".nojekyll").write_text("", encoding="utf-8")
    data = json.dumps(state["snapshot"], ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = (HERE / "ai_black_hole.html").read_text(encoding="utf-8")
    html = html.replace("<title>", "<script>window.LIVE_POLL = 'data.json';</script>\n<title>", 1)
    html = html.replace('<script src="https://cdn.jsdelivr.net/npm/three@',
                        f'<script id="embedded-snapshot" type="application/json">{data}</script>\n<script src="https://cdn.jsdelivr.net/npm/three@', 1)
    (SITE / "index.html").write_text("<!doctype html>\n" + html, encoding="utf-8")
    live = state["live"]
    print(f"built _site: snapshot {state['snapshot'].get('date')}, {len(live.get('quotes') or {})} quotes, "
          + ", ".join(f"{k} {'ok' if v.get('ok') else 'failed'}" for k, v in (live.get("status") or {}).items()))


if __name__ == "__main__":
    main()
