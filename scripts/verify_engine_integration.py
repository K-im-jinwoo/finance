"""Compare canonical and archived engines in separate offline Python processes."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from bootstrap import ROOT

RUNNER = r"""
import json, pathlib, sys
root, source = map(pathlib.Path, sys.argv[1:3])
sys.path[:0] = [str(root/'scripts'), str(root/'src')]
import bootstrap
sources = {(root/'engine/src').resolve(), (root/'vendor/stock-assistant/src').resolve()}
sys.path[:] = [str(source)] + [p for p in sys.path if pathlib.Path(p or '.').resolve() not in sources]
import stock_assistant
if not pathlib.Path(stock_assistant.__file__).resolve().is_relative_to(source.resolve()):
    raise AssertionError('comparison process imported the wrong engine')
from make_validation_dataset import make
from research_dashboard.engine import Engine
from research_dashboard.rules import Policy
from validate_observed_prices import calculate as prices
from validate_observed_financials import calculate as financials
dataset = make()
stored = json.loads((root/'datasets/validation.json').read_text(encoding='utf-8'))
if dataset['events'] != stored['events'] or dataset['securities'] != stored['securities']:
    raise AssertionError('engine changed the original synthetic decisions')
price, financial = prices(save=False), financials(save=False)
price.pop('checked_at_kst')
financial.pop('checked_at_kst')
print(json.dumps({'dataset':dataset,'generated_replay':Engine(dataset,Policy()).run(),
                  'stored_replay':Engine(stored,Policy()).run(),
                  'prices':price,'financials':financial},sort_keys=True,ensure_ascii=False))
"""


def compare():
    results=[]
    for source in (ROOT/'vendor/stock-assistant/src', ROOT/'engine/src'):
        output=subprocess.check_output(
            [sys.executable,'-B','-X','utf8','-c',RUNNER,str(ROOT),str(source)],
            text=True,encoding='utf-8',timeout=60)
        results.append(json.loads(output))
    if results[0] != results[1]:
        different=[name for name in results[0] if results[0][name] != results[1][name]]
        raise AssertionError('engine compatibility mismatch: '+', '.join(different))
    result=results[1]
    digest=hashlib.sha256(json.dumps(result,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return {'canonical_and_snapshot_equal':True,'comparison_sha256':digest,
            'synthetic_events':len(result['dataset']['events']),
            'price_calculation_dates':result['prices']['calculation_dates_total'],
            'financial_checks':len(result['financials']['checks']),
            'real_orders':False,'external_calls':False}


if __name__=='__main__':
    print(json.dumps(compare(),ensure_ascii=False))
