"""Initialize, update, or control the isolated long-term paper ledger."""
import argparse
import json
from pathlib import Path

from bootstrap import ROOT
from research_dashboard.longterm_ledger import LongTermAccounts
from research_dashboard.phase_b_db import DB_ERRORS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("action", choices=("refresh", "init", "pause", "resume"), nargs="?", default="refresh")
    args = parser.parse_args()
    ledger = LongTermAccounts(args.root)
    try:
        if args.action == "refresh":
            if ledger.overview()["status"] == "NO_ACCOUNT":
                ledger.initialize()
            ledger.refresh()
        elif args.action == "init":
            ledger.initialize()
        else:
            ledger.control(args.action)
        print(json.dumps(ledger.overview(), ensure_ascii=True))
    except DB_ERRORS as exc:
        parser.exit(1, f"Long-term paper ledger unavailable ({type(exc).__name__}). Check the configured databases.\n")


if __name__ == "__main__":
    main()
