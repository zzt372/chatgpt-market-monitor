#!/usr/bin/env python3
"""NDX finalized-close history collector."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from index_drawdown import IndexCollector

collector = IndexCollector("NDX", Path(__file__).with_name("drawdown.json"))
build_payload = collector.build_payload
validate_payload = collector.validate_payload
fetch_all = collector.fetch_all

if __name__ == "__main__":
    raise SystemExit(collector.main())
