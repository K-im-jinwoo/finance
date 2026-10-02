"""Independent period/account comparison of a small currently observed public sample."""
import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from bootstrap import ROOT
from stock_assistant.providers.dart import normalize_dart_financial_statement, normalize_dart_operating_income_periods


def account(statement, account_id):
    rows = [row for row in statement['public_rows'] if row['account_id'] == account_id]
    if len(rows) != 1:
        raise ValueError('unique independently selected account required')
    row = rows[0]
    if row['currency'] != 'KRW' or int(row['bsns_year']) != statement['business_year'] or row['reprt_code'] != statement['report_code']:
        raise ValueError('currency/year/report mismatch')
    return row


def cumulative(row, previous=False):
    fields = ('frmtrm_add_amount', 'frmtrm_amount') if previous else ('thstrm_add_amount', 'thstrm_amount')
    for field in fields:
        value = row.get(field)
        if value is not None and str(value).strip() not in {'', '-'}:
            amount = Decimal(str(value).replace(',', ''))
            if not amount.is_finite():
                raise ValueError('non-finite financial amount')
            return amount
    raise ValueError('cumulative amount unavailable')


def calculate():
    path = ROOT/'datasets/observed-public-financial-sample.json'
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if data.get('error_type'):
        raise ValueError('public financial retrieval failed: '+data['error_type'])
    if len(data['statements']) != 3 or any(item['status'] != '000' or item['division'] != 'CFS' for item in data['statements']):
        raise ValueError('three matching connected statements required')
    annual = next(item for item in data['statements'] if (item['business_year'],item['report_code']) == (2025,'11011'))
    current = next(item for item in data['statements'] if (item['business_year'],item['report_code']) == (2026,'11012'))
    previous = next(item for item in data['statements'] if (item['business_year'],item['report_code']) == (2025,'11012'))
    annual_row = account(annual, 'dart_OperatingIncomeLoss')
    current_row = account(current, 'dart_OperatingIncomeLoss')
    previous_row = account(previous, 'dart_OperatingIncomeLoss')
    ocf_row = account(annual, 'ifrs-full_CashFlowsFromUsedInOperatingActivities')
    annual_income, annual_ocf = cumulative(annual_row), cumulative(ocf_row)
    current_income, previous_income = cumulative(current_row), cumulative(previous_row)
    previous_comparative = cumulative(current_row, previous=True)
    ttm = annual_income + current_income - previous_income
    annual_url = 'https://dart.fss.or.kr/dsaf001/main.do?rcpNo='+annual_row['rcept_no']
    current_url = 'https://dart.fss.or.kr/dsaf001/main.do?rcpNo='+current_row['rcept_no']
    # Receipt date is known; exact publication time and historical revisions are not certified.
    receipt_date = datetime.strptime(annual_row['rcept_no'][:8], '%Y%m%d').date()
    conservative_publication = datetime.combine(receipt_date,time.max,tzinfo=timezone(timedelta(hours=9)))
    normalized = normalize_dart_financial_statement(
        {'status':'000','list':annual['public_rows']},symbol=data['symbol'],
        period_end=date.fromisoformat(annual['period_end']),published_at=conservative_publication,source_url=annual_url)
    periods = normalize_dart_operating_income_periods({'status':'000','list':current['public_rows']},source_url=current_url)
    stored = data['stored_snapshot']
    if stored is None:
        raise ValueError('stored public snapshot unavailable')
    checks = {
        'original_annual_income_equal': normalized.annual_operating_income == annual_income,
        'original_annual_ocf_equal': normalized.operating_cash_flow == annual_ocf,
        'original_interim_cumulative_equal': periods.current_cumulative == current_income,
        'previous_comparative_equal_separate_prior_year_report': previous_comparative == previous_income,
        'original_ttm_formula_equal': normalized.annual_operating_income + periods.current_cumulative - periods.previous_cumulative == ttm,
        'stored_annual_income_equal': Decimal(stored['annual_operating_income']) == annual_income,
        'stored_ttm_income_equal': Decimal(stored['ttm_operating_income']) == ttm,
        'stored_operating_income_equal': Decimal(stored['operating_income']) == ttm,
        'stored_annual_ocf_equal': Decimal(stored['operating_cash_flow']) == annual_ocf,
        'stored_periods_equal': stored['period_end'] == annual['period_end'] and stored['ttm_period_end'] == current['period_end'],
        'stored_receipt_links_equal': stored['source_url'] == annual_url and stored['ttm_source_url'] == current_url,
    }
    result = {
        'checked_at_kst':datetime.now(timezone(timedelta(hours=9))).isoformat(),
        'mode':'OBSERVED_PUBLIC_FINANCIAL_CALCULATION_VALIDATION','symbol':data['symbol'],
        'dataset_id':data['dataset_id'],'input_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'observed_at':data['retrieved_at'],'division':'CFS','currency':'KRW',
        'periods':{'annual':annual['period_end'],'current_cumulative':current['period_end'],'previous_cumulative':previous['period_end']},
        'values':{'annual_operating_income':str(annual_income),'current_cumulative_income':str(current_income),
                  'previous_cumulative_income':str(previous_income),'ttm_operating_income':str(ttm),'annual_operating_cash_flow':str(annual_ocf)},
        'checks':checks,'all_equal':all(checks.values()),
        'receipts':[annual_row['rcept_no'],current_row['rcept_no'],previous_row['rcept_no']],
        'original_ocf_basis':'annual; not changed to interim or TTM OCF',
        'production_db_changed':False,'historical_point_in_time_certified':False,'automatic_invalidation_certified':False,
        'limitation':'current observation only; exact publication time, revisions and full historical coverage are not certified',
    }
    (ROOT/'artifacts/observed-financial-calculation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    if not result['all_equal']:
        raise AssertionError('public financial comparison failed; inspect saved checks')
    return result


if __name__ == '__main__':
    print(json.dumps(calculate(),ensure_ascii=False,indent=2))
