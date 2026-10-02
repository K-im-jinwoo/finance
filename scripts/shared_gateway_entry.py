"""Staged shared-key profile; never copy, register or rotate the existing keys."""
from pathlib import Path
import sys
sys.path.insert(0,'/opt/dashboard')
import market_gateway
import market_read_actor
from shared_toss_auth import SharedTossMarketDataClient

if __name__=='__main__':
    cid=Path('/run/secrets/toss-client-id').read_text().strip()
    secret=Path('/run/secrets/toss-client-secret').read_text().strip()
    market_read_actor.client=SharedTossMarketDataClient(cid,secret)
    server=market_gateway.PublicDataServer(('0.0.0.0',9130),auth_status_provider=lambda:'AUTH_REGISTERED_NOT_VERIFIED')
    try:server.serve_forever()
    finally:server.server_close()
