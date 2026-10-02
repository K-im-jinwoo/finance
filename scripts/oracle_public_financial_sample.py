"""Bounded public DART accounts; original DB is read-only and keys stay on Oracle."""
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

result = {
    'schema_version': 1, 'purpose': 'OBSERVED_PUBLIC_FINANCIAL_CALCULATION_VALIDATION',
    'retrieved_at': datetime.now(timezone.utc).isoformat(), 'symbol': '240810',
    'source_database_changed': False, 'historical_point_in_time_certified': False,
    'statements': [], 'stored_snapshot': None,
}
try:
    from stock_assistant.providers.dart import DartClient, DART_FINANCIAL_URL
    client = DartClient(Path('/run/secrets/dart-api-key').read_text().strip())
    corp = client.corp_codes().get(result['symbol'])
    if not corp:
        raise ValueError('corp mapping unavailable')
    ids = {'dart_operatingincomeloss', 'ifrs-full_profitlossfromoperatingactivities',
           'ifrs-full_cashflowsfromusedinoperatingactivities', 'dart_cashflowsfromusedinoperatingactivities'}
    names = {'영업이익', '영업이익(손실)', '영업활동으로인한현금흐름', '영업활동현금흐름'}
    fields = ('rcept_no', 'bsns_year', 'reprt_code', 'fs_div', 'sj_div', 'account_id',
              'account_nm', 'thstrm_nm', 'thstrm_amount', 'thstrm_add_amount',
              'frmtrm_nm', 'frmtrm_amount', 'frmtrm_add_amount', 'currency')
    for year, report, period in [(2025, '11011', '2025-12-31'),
                                  (2026, '11012', '2026-06-30'),
                                  (2025, '11012', '2025-06-30')]:
        payload = client.fetch_json(DART_FINANCIAL_URL, query={
            'crtfc_key': client.api_key, 'corp_code': corp, 'bsns_year': str(year),
            'reprt_code': report, 'fs_div': 'CFS',
        }).payload
        rows = [row for row in payload.get('list', [])
                if row.get('account_id', '').strip().lower() in ids
                or row.get('account_nm', '').replace(' ', '') in names]
        result['statements'].append({
            'business_year': year, 'report_code': report, 'division': 'CFS',
            'period_end': period, 'status': payload.get('status'),
            'public_rows': [{key: row.get(key) for key in fields} for row in rows],
            'source_response_sha256': hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
        })
    with closing(sqlite3.connect(Path('/var/lib/stock/stock-assistant.sqlite3').as_uri()+'?mode=ro', uri=True)) as connection:
        connection.execute('PRAGMA query_only=ON')
        row = connection.execute('SELECT payload_json FROM financial_snapshots WHERE symbol=? ORDER BY published_at DESC LIMIT 1', (result['symbol'],)).fetchone()
        if row:
            payload = json.loads(row[0])
            result['stored_snapshot'] = {key: payload.get(key) for key in (
                'symbol', 'period_end', 'published_at', 'operating_income', 'operating_cash_flow',
                'annual_operating_income', 'ttm_operating_income', 'ttm_period_end',
                'source_url', 'ttm_source_url')}
except Exception as error:
    result['error_type'] = type(error).__name__
result['dataset_id'] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
print(json.dumps(result, ensure_ascii=False, indent=2))
