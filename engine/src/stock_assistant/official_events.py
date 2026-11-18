"""Conservative machine review of structured official contract disclosures."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import re
import zipfile
from xml.etree import ElementTree
from html.parser import HTMLParser

PARSER_VERSION='dart-contract-tables-v2'

class TableRows(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.frames=[]; self.cells=[]; self.rows=[]
    def handle_starttag(self,tag,attrs):
        if tag=='tr':self.frames.append([])
        elif tag in ('td','th','te') and self.frames:self.cells.append((self.frames[-1],[]))
    def handle_data(self,data):
        if self.cells:self.cells[-1][1].append(data)
    def handle_endtag(self,tag):
        if tag in ('td','th','te') and self.cells:
            frame,parts=self.cells.pop();frame.append(''.join(parts).strip())
        elif tag=='tr' and self.frames:
            row=self.frames.pop()
            if len(row)>1:self.rows.append(row)

from .dart_research import receipt, stored_filings
from .models import Evidence, to_json_value
from .news_research import ReviewedEvent
from .providers.http import get_bytes, ProviderError


def contract_fields(raw: bytes) -> dict:
    if len(raw) > 5_000_000:
        raise ValueError("DART_DOCUMENT_SIZE_LIMIT")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        files = [i for i in archive.infolist() if i.filename.lower().endswith('.xml')]
        if not files or sum(i.file_size for i in files) > 10_000_000:
            raise ValueError("DART_DOCUMENT_EXPANSION_LIMIT")
        rows = []
        for item in files:
            body = archive.read(item)
            if b'<!ENTITY' in body.upper():
                raise ValueError("DART_DOCUMENT_ENTITY_REJECTED")
            declared=re.search(br'encoding=[\"\x27]([^\"\x27]+)',body[:200],re.I)
            encoding=declared.group(1).decode('ascii') if declared else 'utf-8'
            try:text=body.decode(encoding)
            except (UnicodeError,LookupError) as exc:raise ValueError('DART_DOCUMENT_ENCODING_UNAVAILABLE') from exc
            parser=TableRows();parser.feed(text);rows.extend(parser.rows)
    fields = {}
    labels = {'party': ('계약상대방','계약상대'), 'description': ('체결계약명', '판매공급계약내용', '판매ㆍ공급계약내용'),
              'ratio': ('매출액대비',), 'start': ('시작일',), 'end': ('종료일',), 'amount': ('확정계약금액', '계약금액')}
    for cells in rows:
        for key, names in labels.items():
            for i, cell in enumerate(cells[:-1]):
                normalized = re.sub(r'[\s·ㆍ()%]', '', cell)
                if any(name in normalized for name in names):
                    value = next((v for v in reversed(cells[i+1:]) if v and v != '-'), '')
                    if value:
                        if key in fields and fields[key] != value:
                            raise ValueError('CONTRACT_AMBIGUOUS_FIELD')
                        fields[key] = value
    if not {'party', 'description', 'ratio', 'start', 'end', 'amount'}.issubset(fields):
        raise ValueError("CONTRACT_STRUCTURED_FIELDS_UNAVAILABLE")
    for key in ('amount', 'ratio'):
        try:
            value = Decimal(fields[key].replace(',', '').replace('%', '').strip())
        except InvalidOperation as exc:
            raise ValueError("CONTRACT_NUMERIC_FIELD_INVALID") from exc
        if not value.is_finite() or value <= 0:
            raise ValueError("CONTRACT_NUMERIC_FIELD_INVALID")
        fields[key] = value
    for key in ('start', 'end'):
        match = re.search(r'(20\d{2})[-./년\s]+(\d{1,2})[-./월\s]+(\d{1,2})', fields[key])
        if not match:
            raise ValueError("CONTRACT_DATE_FIELD_INVALID")
        fields[key] = datetime(*map(int, match.groups()), tzinfo=timezone(timedelta(hours=9)))
    if fields['end'] < fields['start']:
        raise ValueError("CONTRACT_PERIOD_INVALID")
    return fields


def review_official_contracts(store, *, api_key, as_of, limit=5, fetch_bytes=get_bytes, clock=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("official review limit must be between 1 and 20")
    reviews, failures = [], []
    cursors = store.latest_by_symbol('dart_cursor', as_of=as_of)
    pending = []
    for symbol, cursor in cursors.items():
        for source in stored_filings(store, symbol, as_of=as_of, cursor=cursor):
            title = source.title.replace(' ', '')
            if '단일판매' in title and ('공급계약체결' in title or '공급계약해지' in title):
                checked = store.latest('official_review', symbol + ':' + receipt(source.url), as_of=as_of)
                if checked and (checked['payload'].get('status')=='VERIFIED' or checked['payload'].get('parser_version')==PARSER_VERSION):
                    continue
                pending.append((source.published_at, symbol, source))
    pending.sort(reverse=True, key=lambda x: (x[0], x[1], x[2].url))
    for _, symbol, source in pending[:limit]:
        key = symbol + ':' + receipt(source.url)
        try:
            response = fetch_bytes('https://opendart.fss.or.kr/api/document.xml',
                                   query={'crtfc_key': api_key, 'rcept_no': receipt(source.url)})
            fields = contract_fields(response.payload)
            observed = clock()
            identity = 'CONTRACT:' + hashlib.sha256((symbol + '|' + fields['party'] + '|' + fields['description'] + '|' + fields['start'].date().isoformat()).encode()).hexdigest()[:24]
            cancelled = '해지' in source.title
            primary = Evidence('DART', source.title, source.url, source.published_at, observed, True,
                               (f'ISSUER:{symbol}', 'STRUCTURED_CONTRACT_FIELDS_VERIFIED', f"SALES_RATIO:{fields['ratio']}"))
            event = ReviewedEvent(symbol, identity, 'BUSINESS_DAMAGE' if cancelled else 'CONTRACT',
                                  'NEGATIVE' if cancelled else 'POSITIVE', 'ACTIVE',
                                  'HIGH' if fields['ratio'] >= 20 else 'MEDIUM' if fields['ratio'] >= 5 else 'LOW', True, False,
                                  source.published_at, observed, None if cancelled else fields['end'] + timedelta(days=1),
                                  'dart-contract-tables-v1', '원문 구조화 표에서 상대방·계약명·금액·매출비율·기간 확인', (primary,))
            # A corrected/cancelled contract uses the same economic-event key.
            existing = [r for r in store.records('reviewed_event', symbol, as_of=observed) if r['payload']['event_id'] == identity]
            if not existing or max(datetime.fromisoformat(r['payload']['announced_at']) for r in existing) <= source.published_at:
                store.put('reviewed_event', symbol, to_json_value(event), observed_at=observed)
            store.put('official_review', key, {'status': 'VERIFIED', 'receipt_id': receipt(source.url), 'parser_version':PARSER_VERSION}, observed_at=observed)
            reviews.append({'symbol': symbol, 'receipt_id': receipt(source.url), 'category': event.category})
        except (ValueError, zipfile.BadZipFile, ElementTree.ParseError, ProviderError) as exc:
            code = exc.code if isinstance(exc, ProviderError) else str(exc)
            failures.append({'symbol': symbol, 'receipt_id': receipt(source.url), 'error_code': code})
            # Cache unsupported documents to avoid burning the same calls each cycle.
            if not isinstance(exc, ProviderError):
                store.put('official_review', key, {'status': 'VERIFY_PENDING', 'error_code': code, 'parser_version':PARSER_VERSION}, observed_at=clock())
    return {'verified': reviews, 'pending': failures}
