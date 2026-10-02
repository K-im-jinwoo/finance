import argparse
import threading
from bootstrap import ROOT
from research_dashboard.server import DashboardServer

if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument('--hermes-live',action='store_true',help='Read Hermes finals and existing production price cache over SSH')
    parser.add_argument('--direct-market',action='store_true',help='Requires approved remote public-data service and coordinated shared authentication')
    parser.add_argument('--enable-open-fills',action='store_true',help='Enable only after approval and live verification of the raw-open capture policy')
    args=parser.parse_args()
    if args.direct_market and not args.hermes_live:
        parser.error('--direct-market requires --hermes-live')
    if args.enable_open_fills and not args.direct_market:
        parser.error('--enable-open-fills requires --direct-market')
    server=DashboardServer(ROOT,hermes_live=args.hermes_live,direct_market=args.direct_market,allow_open_fills=args.enable_open_fills)
    threading.Thread(target=server.run_worker,daemon=True).start()
    if server.live_worker:
        threading.Thread(target=server.live_worker.run,daemon=True).start()
    print("Local validation dashboard: http://127.0.0.1:8765 ; no orders, deployments or Telegram sends",flush=True)
    try:
        server.serve_forever()
    finally:
        server.stopping.set()
        server.server_close()

