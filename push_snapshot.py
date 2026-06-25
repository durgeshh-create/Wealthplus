#!/usr/bin/env python3
"""
push_snapshot.py
================
Pushes a single status JSON file to gh-pages via the GitHub Contents API.
No git, no worktree, no rebase — just one HTTPS PUT call (~1-2 s).

Usage (called from the workflow shell loop):
    python3 push_snapshot.py /tmp/status_rd1858.json status_rd1858.json
    python3 push_snapshot.py /tmp/status_ps5673.json status_ps5673.json

Environment variables (already present in Actions env):
    GITHUB_TOKEN   — repo-scoped token injected by Actions
    GH_REPO        — "owner/repo"  e.g. "durgeshh-create/Wealthplus"
"""

import base64
import json
import os
import sys
import time
import urllib.request
import urllib.error

# ── Args ──────────────────────────────────────────────────────────────────────
if len(sys.argv) != 3:
    print(f"Usage: {sys.argv[0]} <local_json_path> <gh-pages_filename>", file=sys.stderr)
    sys.exit(1)

LOCAL_PATH  = sys.argv[1]   # e.g. /tmp/status_rd1858.json
REMOTE_FILE = sys.argv[2]   # e.g. status_rd1858.json  (placed at root of gh-pages)

TOKEN   = os.environ.get("GITHUB_TOKEN", "")
GH_REPO = os.environ.get("GH_REPO", "")

if not TOKEN or not GH_REPO:
    print("[push_snapshot] ERROR: GITHUB_TOKEN or GH_REPO not set", file=sys.stderr)
    sys.exit(1)

if not os.path.exists(LOCAL_PATH):
    print(f"[push_snapshot] {LOCAL_PATH} does not exist yet — skipping", file=sys.stderr)
    sys.exit(0)

BRANCH  = "gh-pages"
API_URL = f"https://api.github.com/repos/{GH_REPO}/contents/{REMOTE_FILE}"
HEADERS = {
    "Authorization": f"token {TOKEN}",
    "Accept":        "application/vnd.github.v3+json",
    "Content-Type":  "application/json",
    "User-Agent":    "WealthAlgo-PushSnapshot/2.0",
}

# ── Read local file ───────────────────────────────────────────────────────────
with open(LOCAL_PATH, "rb") as f:
    raw_bytes = f.read()

b64_content = base64.b64encode(raw_bytes).decode()

# ── Helper: fetch current SHA from gh-pages ───────────────────────────────────
def fetch_sha():
    """Return current blob SHA from gh-pages, or None if file doesn't exist yet."""
    try:
        req = urllib.request.Request(f"{API_URL}?ref={BRANCH}", headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get("sha")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None   # file doesn't exist yet — first create
        raise
    # Other exceptions propagate to caller

# ── PUT (create or update) with retry on 409 stale-SHA conflict ───────────────
commit_msg = f"snapshot {REMOTE_FILE.replace('status_','').replace('.json','')} {time.strftime('%H:%M UTC', time.gmtime())}"

MAX_RETRIES = 4
for attempt in range(1, MAX_RETRIES + 1):
    try:
        current_sha = fetch_sha()
    except Exception as ex:
        print(f"[push_snapshot] GET error: {ex}", file=sys.stderr)
        sys.exit(1)

    body: dict = {
        "message": commit_msg,
        "content": b64_content,
        "branch":  BRANCH,
    }
    if current_sha:
        body["sha"] = current_sha

    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode(),
        headers=HEADERS,
        method="PUT",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            commit_url = result.get("commit", {}).get("html_url", "")
            print(f"[push_snapshot] ✅ {REMOTE_FILE} pushed → {commit_url or 'ok'}")
            sys.exit(0)
    except urllib.error.HTTPError as e:
        if e.code == 409 and attempt < MAX_RETRIES:
            # Stale SHA — another writer committed between our GET and PUT.
            # Re-fetch fresh SHA and retry immediately.
            wait = attempt * 0.5
            print(f"[push_snapshot] 409 conflict (stale SHA) — retry {attempt}/{MAX_RETRIES - 1} in {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        body_text = e.read().decode(errors="replace")[:300]
        print(f"[push_snapshot] ❌ PUT failed: HTTP {e.code} — {body_text}", file=sys.stderr)
        sys.exit(1)
    except Exception as ex:
        print(f"[push_snapshot] ❌ PUT error: {ex}", file=sys.stderr)
        sys.exit(1)
