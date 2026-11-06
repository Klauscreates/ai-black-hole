"""Publish a new daily snapshot to the public site.

Copies the snapshot to data/snapshot.json (without the ICE BofA spread history, which is
ICE's copyright), refreshes the offline copy in docs/, then commits and pushes. The push
redeploys the site through .github/workflows/live.yml.

  python publish_snapshot.py path/to/latest_snapshot.json
  python publish_snapshot.py path/to/latest_snapshot.json --no-push
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "data" / "snapshot.json"
TRAILER = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"


def git(*args):
    return subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("snapshot")
    ap.add_argument("--no-push", action="store_true", help="commit only")
    args = ap.parse_args()

    snap = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    if not snap.get("date") or not snap.get("analysis"):
        sys.exit("not a complete snapshot (missing date or analysis); nothing published")
    if OUT.exists() and json.loads(OUT.read_text(encoding="utf-8")).get("date", "") > snap["date"]:
        sys.exit(f"data/snapshot.json is already newer than {snap['date']}; nothing published")
    for k in ("bbb", "hy"):
        if k in (snap.get("series") or {}):
            snap["series"][k]["hist"] = []
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
    subprocess.run([sys.executable, str(HERE / "make_share.py"), "--pages", "--snapshot", str(OUT)], cwd=HERE, check=True, capture_output=True)
    for p in (HERE / "share").glob("*.html"):
        p.unlink()

    git("add", "data/snapshot.json", "docs/index.html")
    if not git("diff", "--cached", "--quiet").returncode:
        print(f"snapshot {snap['date']} is already published; nothing to commit")
        return
    res = git("commit", "-m", f"Daily snapshot {snap['date']}", "-m", TRAILER)
    if res.returncode:
        sys.exit(f"commit failed: {res.stderr.strip()}")
    if args.no_push:
        print(f"committed snapshot {snap['date']} (not pushed)")
        return
    res = git("push")
    if res.returncode:
        sys.exit(f"committed, but the push failed: {res.stderr.strip()[-400:]}")
    print(f"published snapshot {snap['date']}; the site redeploys in about a minute")


if __name__ == "__main__":
    main()
