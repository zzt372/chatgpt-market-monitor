#!/usr/bin/env python3
"""Race-safe publisher for validated Nasdaq-100 forward P/E readings.

Another workflow may update main during this job. Never attempt git rebase of
the same generated JSON; instead re-read the remote and recreate the commit.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

from fetch import parse_utc_timestamp, validate_payload

ROOT = Path(__file__).resolve().parents[2]
FILE = ROOT / "monitors/nasdaq-forward-pe/latest.json"
GIT_PATH = "monitors/nasdaq-forward-pe/latest.json"


def git(*args, check=True, capture=False):
    result = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        text=True,
        capture_output=capture,
        check=check,
    )
    return result.stdout if capture else result.returncode


def good_remote(remote, candidate) -> bool:
    """Skip older or redundant local commits when the remote is already fresh."""
    try:
        validate_payload(remote, max_fetch_age_seconds=3 * 3600)
        local_date = candidate["data_date"]
        remote_date = remote["data_date"]
        local_ts = parse_utc_timestamp(candidate["fetched_at"])
        remote_ts = parse_utc_timestamp(remote["fetched_at"])
        if remote_date > local_date:
            return True
        if remote_date < local_date:
            return False
        if remote_ts >= local_ts:
            return True
        if (
            remote["forward_pe"] == candidate["forward_pe"]
            and remote["percentile_10y"] == candidate["percentile_10y"]
            and local_ts - remote_ts <= timedelta(minutes=25)
        ):
            return True
    except Exception:
        return False
    return False


def main():
    candidate = json.loads(FILE.read_text(encoding="utf-8"))
    validate_payload(candidate, max_fetch_age_seconds=120)
    bytes_to_publish = (json.dumps(candidate, indent=2) + "\n").encode("utf-8")
    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")

    for attempt in range(1, 6):
        git("fetch", "--quiet", "origin", "main")
        try:
            remote = json.loads(git("show", "origin/main:" + GIT_PATH, capture=True))
        except Exception:
            remote = {}
        if good_remote(remote, candidate):
            print("Remote already has an equivalent/newer validated reading; skip redundant commit.")
            return 0

        # Preserve validated in-memory output while rebasing on the newest main.
        # This avoids fatal rebase conflicts on concurrent latest.json updates.
        git("reset", "--hard", "origin/main")
        FILE.write_bytes(bytes_to_publish)
        git("add", GIT_PATH)
        if git("diff", "--cached", "--quiet", check=False) == 0:
            print("No change to publish.")
            return 0
        git("commit", "-m", "Update Nasdaq 100 forward P/E")
        if git("push", "origin", "HEAD:main", check=False) == 0:
            print(f"Published on attempt {attempt}.")
            return 0
        print(f"Concurrent push detected: retrying against current main ({attempt}/5).", file=sys.stderr)
        time.sleep(attempt * 2)

    print("Unable to publish validated latest.json after 5 attempts.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
