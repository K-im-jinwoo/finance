"""Staged collector entry: retain original CLI/DB/alert semantics and share auth."""
import sys
from shared_toss_auth import SharedTossMarketDataClient

if __name__=='__main__':
    if len(sys.argv)<2 or sys.argv[1]!='refresh-intraday':
        raise SystemExit('SHARED_COLLECTOR_REQUIRES_REFRESH_INTRADAY')
    from stock_assistant import cli
    cli.TossMarketDataClient=SharedTossMarketDataClient
    raise SystemExit(cli.main())
