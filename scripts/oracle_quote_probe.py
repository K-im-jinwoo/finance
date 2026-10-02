"""One public price read, using existing in-container credentials; no repository."""
import json
from datetime import datetime, timezone
from pathlib import Path

result={'observed_at':datetime.now(timezone.utc).isoformat(),'scope':'public quote only','production_db_changed':False}
try:
    from stock_assistant.providers.toss import TossMarketDataClient
    from stock_assistant.providers.http import get_json
    from stock_assistant.models import to_json_value
    def fetch(*args,**kwargs):
        response=get_json(*args,**kwargs)
        rows=response.payload.get('result',[])
        result['price_field_names']=[sorted(row.keys()) for row in rows if isinstance(row,dict)]
        return response
    client=TossMarketDataClient(Path('/run/secrets/toss-client-id').read_text().strip(),
                                Path('/run/secrets/toss-client-secret').read_text().strip(),fetch_json=fetch)
    result['quotes']=to_json_value(client.prices(['240810'],observed_at=datetime.now(timezone.utc)))
except Exception as error:
    result['error_type']=type(error).__name__
print(json.dumps(result,ensure_ascii=False,indent=2))
