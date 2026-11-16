"""Offline scan, synthetic simulation, audited reports and legacy replay."""
import argparse
import json
from pathlib import Path
import sys

from .bot import run_paper_session, scan_dataset
from .contracts import canonical
from .ledger import DaytradeLedger
from .report import build_trading_report, render_markdown_report
from .replay import replay_dataset
from .stream import run_observed_stream


def _read(path):
    if path.stat().st_size > 10_000_000:
        raise ValueError("bounded offline input required")
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    replay = sub.add_parser("replay", help="no network; never overwrites an existing ledger")
    replay.add_argument("input", type=Path)
    replay.add_argument("--root", required=True, type=Path)
    scan = sub.add_parser("scan", help="rankings discovery only; no ledger writes")
    scan.add_argument("input", type=Path)
    scan.add_argument("--quotes", type=Path)
    for command, help_text in (("simulate", "synthetic fixtures only; creates an isolated ledger"),
                               ("run-stream", "offline public Toss stream; provisional development costs")):
        simulate = sub.add_parser(command, help=help_text)
        simulate.add_argument("input", type=Path)
        simulate.add_argument("--root", required=True, type=Path)
        simulate.add_argument("--report-md", type=Path)
        simulate.add_argument("--report-json", type=Path)
    report_cmd = sub.add_parser("report", help="rebuild report from the existing isolated ledger")
    report_cmd.add_argument("--root", required=True, type=Path)
    report_cmd.add_argument("--format", choices=("markdown", "json"), default="markdown")
    args = parser.parse_args(argv)
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if args.command == "report":
            ledger = DaytradeLedger(args.root)
            try:
                report = build_trading_report(ledger)
            finally:
                ledger.close()
            print(render_markdown_report(report) if args.format == "markdown" else canonical(report))
            return 0
        dataset = _read(args.input)
        if args.command == "scan":
            result = scan_dataset(dataset, quotes=_read(args.quotes) if args.quotes else None)
            print(canonical({"selected": result.selected, "rejected": result.rejected,
                             "mode": "OBSERVE_ONLY", "orders_enabled": False}))
            return 0
        if args.command == "replay":
            report = replay_dataset(dataset, args.root)
        else:
            outputs = [p for p in (args.report_md, args.report_json) if p is not None]
            reserved = {(args.root / ".runtime" / "intraday-paper.sqlite3").resolve(), args.input.resolve()}
            if (len({p.resolve() for p in outputs}) != len(outputs)
                    or any(p.exists() or p.resolve() in reserved or not p.parent.is_dir() for p in outputs)):
                raise ValueError("distinct new report files in existing directories required")
            runner = run_observed_stream if args.command == "run-stream" else run_paper_session
            report = runner(dataset, args.root)
            for path, content in ((args.report_md, render_markdown_report(report)),
                                   (args.report_json, canonical(report) + "\n")):
                if path is not None:
                    with path.open("x", encoding="utf-8") as output:
                        output.write(content)
        print(canonical(report))
        return 0
    except Exception as exc:
        # No dataset contents, file paths, credentials, or tracebacks in output.
        print(json.dumps({"status": "FAILED", "error_class": type(exc).__name__, "mode": "OBSERVE_ONLY"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
