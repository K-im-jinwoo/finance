import argparse
import json
from bootstrap import ROOT
from research_dashboard.rules import Policy
from research_dashboard.store import Store
from research_dashboard.notifications import drafts

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=str(ROOT / "datasets/validation.json"))
    parser.add_argument("--cost-bps", default="30")
    parser.add_argument("--compare", action="store_true", help="10/30/60bps are explicitly labeled sensitivity assumptions")
    args = parser.parse_args()
    with open(args.dataset, encoding="utf-8") as stream:
        data = json.load(stream)
    store = Store(ROOT / ".runtime/paper.sqlite3")
    results = []
    for cost in (["10", "30", "60"] if args.compare else [args.cost_bps]):
        eid = store.create(data, Policy(round_trip_bps=cost))
        store.apply(eid, "start")
        result = store.apply(eid, limit=len(data["events"]))
        result["telegram_drafts"] = drafts(result)
        (ROOT / f"artifacts/{eid}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        results.append({"experiment_id": eid, "mode": result["coverage"]["mode"], "cost_bps": cost, "cost_role": "USER_BASELINE" if cost == "30" else "SENSITIVITY_ASSUMPTION", "metrics": result["metrics"], "latest_equity": result["state"]["equity"][-1] if result["state"]["equity"] else None})
    (ROOT / "artifacts/validation-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()

