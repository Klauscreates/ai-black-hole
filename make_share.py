"""Build a single-file copy of the AI Black Hole page with today's data baked in.

The copy opens anywhere (a phone, someone else's computer, another AI's sandbox) with no
server and no Claude account: it shows the real data as of the snapshot date, marked as a
saved copy. Only the 3D engine loads from the internet (jsDelivr).

  python make_share.py            # writes share/ai_black_hole_<date>.html
  python make_share.py --pages    # also writes docs/index.html for GitHub Pages

Uses the local live server's latest state (with current stock quotes) when it is running,
otherwise live/latest_snapshot.json, otherwise the bundled sample/snapshot.json.
"""
import argparse
import json
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE / "ai_black_hole.html"
SNAPSHOT = HERE / "live" / "latest_snapshot.json"
SAMPLE = HERE / "sample" / "snapshot.json"
OUT_DIR = HERE / "share"


def load_snapshot():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8787/state.json", timeout=5) as resp:
            state = json.loads(resp.read().decode("utf-8"))
        snap, quotes = state.get("snapshot"), (state.get("live") or {}).get("quotes") or {}
        if snap and quotes:
            snap["stocks"] = {"date": snap["date"], "source": "Yahoo Finance (unofficial, delayed), via the local live server",
                              "quotes": [{k: q.get(k) for k in ("symbol", "price", "high52", "fromHigh", "dayChange")} for q in quotes.values()]}
            return snap, "live server"
    except OSError:
        pass
    src = SNAPSHOT if SNAPSHOT.exists() else SAMPLE
    return json.loads(src.read_text(encoding="utf-8")), src.name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", action="store_true", help="also write docs/index.html for GitHub Pages")
    ap.add_argument("--snapshot", help="build from this snapshot file instead of the live server or live/ folder")
    args = ap.parse_args()
    if args.snapshot:
        snap, origin = json.loads(Path(args.snapshot).read_text(encoding="utf-8")), args.snapshot
    else:
        snap, origin = load_snapshot()
    # "</" would end the script tag early; JSON allows the escaped form
    data = json.dumps(snap, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = PAGE.read_text(encoding="utf-8")
    tag = f'<script id="embedded-snapshot" type="application/json">{data}</script>\n'
    html = html.replace('<script src="https://cdn.jsdelivr.net/npm/three@', tag + '<script src="https://cdn.jsdelivr.net/npm/three@', 1)
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"ai_black_hole_{snap['date']}.html"
    out.write_text("<!doctype html>\n" + html, encoding="utf-8")
    if args.pages:
        (HERE / "docs").mkdir(exist_ok=True)
        (HERE / "docs" / "index.html").write_text("<!doctype html>\n" + html, encoding="utf-8")
    n = len((snap.get("stocks") or {}).get("quotes") or [])
    print(f"wrote {out} ({out.stat().st_size // 1024} KB) from {origin}: snapshot {snap['date']}, {n} stock quotes")


if __name__ == "__main__":
    main()
