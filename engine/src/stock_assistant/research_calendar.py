"""Calendar reads using an already issued token; never issue or replace tokens."""
from .providers.toss import TossMarketDataClient
from .providers.http import get_json


class ExistingTokenCalendarClient(TossMarketDataClient):
    def __init__(self, token: str, *, fetch_json=get_json):
        if not token.strip() or len(token) > 8192 or any(ord(c) < 32 for c in token):
            raise ValueError("existing market token is invalid")
        super().__init__("existing-token", "no-token-issuance", fetch_json=fetch_json)
        self._existing_token = token

    def _token(self):
        return self._existing_token
