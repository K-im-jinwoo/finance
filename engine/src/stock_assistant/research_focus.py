"""Bound paid research to ten issuers while retaining completed work and rotation."""
from .unified_research import financial_universe


def choose_focus(repository, store, *, as_of):
    eligible = {s.symbol: f for s, f, _ in financial_universe(repository, store, as_of=as_of)
                if f.gate == 'PASS' and f.financial_score >= 70}
    rounds = store.latest_by_symbol('search_round_completed', as_of=as_of)
    latest = store.latest('unified_report', 'ALL', as_of=as_of)
    monitored = [r['symbol'] for r in (latest or {}).get('payload', {}).get('candidates', [])
                 if r['symbol'] in eligible and r['action_decision'] in {'BUY_RECOMMENDATION', 'WATCH'}][:5]
    remaining = [symbol for symbol in eligible if symbol not in monitored]
    remaining.sort(key=lambda symbol: (symbol in rounds, (rounds.get(symbol) or {}).get('observed_at', ''),
                                       -eligible[symbol].financial_score, symbol))
    return monitored + remaining[:10-len(monitored)]
