import ast
import hashlib
import json
import unittest
from datetime import datetime, timezone, timedelta
from bootstrap import ROOT
from research_dashboard.engine import Engine
from research_dashboard.rules import Policy

if __name__ == "__main__":
    for path in list((ROOT/"src").rglob("*.py"))+list((ROOT/"scripts").glob("*.py"))+list((ROOT/"tests").glob("*.py")):
        ast.parse(path.read_text(encoding="utf-8"),filename=str(path))
    manifest=json.loads((ROOT/"artifacts/source-audit.json").read_text(encoding="utf-8"))
    for item in manifest["snapshot_files"]:
        path=ROOT/"vendor/stock-assistant"/item["path"]
        if hashlib.sha256(path.read_bytes()).hexdigest()!=item["sha256"]:
            raise AssertionError("vendored snapshot drift")
    suite=unittest.defaultTestLoader.discover(str(ROOT/"tests"))
    outcome=unittest.TextTestRunner(verbosity=1).run(suite)
    data=json.loads((ROOT/"datasets/validation.json").read_text(encoding="utf-8"))
    first=Engine(data,Policy()).run()
    second=Engine(data,Policy()).run()
    assert first==second, "deterministic replay failed"
    assert not first["coverage"]["three_year_verified"]
    observed = None
    if (ROOT/'datasets/observed-public-prices.json').exists():
        from validate_observed_prices import calculate
        observed = calculate()
    financial = None
    if (ROOT/'datasets/observed-public-financial-sample.json').exists():
        from validate_observed_financials import calculate as calculate_financial
        financial = calculate_financial()
    record={"checked_at_kst":datetime.now(timezone(timedelta(hours=9))).isoformat(),"tests_run":outcome.testsRun,"errors":len(outcome.errors),"failures":len(outcome.failures),
            "deterministic_replay_equal":first==second,"source_snapshot_hashes":len(manifest["snapshot_files"]),"dataset_id":first["coverage"]["dataset_id"],"mode":"SYNTHETIC", "actual_three_year_verified":False,
            "baseline_metrics":first["metrics"],"baseline_latest_equity":first["state"]["equity"][-1],
            "actual_price_formula_validation":None if observed is None else {"symbols":len(observed['results']),"calculation_dates_total":observed['calculation_dates_total'],"all_equal":all(r['source_features_equal_independent_formulas'] for r in observed['results'])},
            "actual_financial_validation":None if financial is None else {"symbol":financial['symbol'],"checks":len(financial['checks']),"all_equal":financial['all_equal']}}
    (ROOT/"artifacts/local-verification.json").write_text(json.dumps(record,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    raise SystemExit(0 if outcome.wasSuccessful() else 1)

