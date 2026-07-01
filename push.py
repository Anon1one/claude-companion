#!/usr/bin/env python3
"""Instant push: write one or more partner-lines to feed.json right now (mid-turn).

Usage:
  push.py "single line"
  push.py "line one" "line two" "line three"   # each arg = one bubble line
"""
import sys, json, os
HERE = os.path.dirname(os.path.abspath(__file__))
FEED = os.environ.get("COMPANION_FEED", os.path.join(HERE, "feed.json"))
lines = [a.strip() for a in sys.argv[1:] if a.strip()]
if not lines:
    sys.exit(0)
prev = {}
try:
    with open(FEED) as f:
        prev = json.load(f)
except Exception:
    pass
out = {"id": prev.get("id", -1) + 1, "lines": lines, "src": "manual"}
for k in ("model", "cmd"):          # carry the event baseline forward
    if k in prev:
        out[k] = prev[k]
with open(FEED, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False)
