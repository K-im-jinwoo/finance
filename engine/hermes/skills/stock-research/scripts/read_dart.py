"""Read public DART originals for a holding review; no credentials or state writes."""
from __future__ import annotations

import argparse
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

HOST = "dart.fss.or.kr"
KST = timezone(timedelta(hours=9))
MAX_BYTES = 4_000_000
MAX_SECTIONS = 16
MAX_CHARS = 14_000
MAX_RESEARCH_REQUESTS = 32
SEARCH_PATH = "/dsab007/detailSearch.ax"


class SourceError(ValueError):
    pass


class VisibleText(HTMLParser):
    """Keep table cells on one row, including units and comparison headers."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.rows = []
        self.lines = []
        self.outside = []

    def flush(self):
        text = " ".join(" ".join(self.outside).split())
        if text:
            self.lines.append(text)
        self.outside = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if self.hidden:
            return
        if tag == "tr":
            self.flush()
            self.rows.append({"cells": [], "cell": []})
        elif tag in {"td", "th"} and self.rows:
            self.rows[-1]["cell"] = []
        elif tag in {"p", "br", "h1", "h2", "h3"} and not self.rows:
            self.flush()

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
            return
        if self.hidden:
            return
        if tag in {"td", "th"} and self.rows:
            row = self.rows[-1]
            row["cells"].append(" ".join(" ".join(row["cell"]).split()))
            row["cell"] = []
        elif tag == "tr" and self.rows:
            row = self.rows.pop()
            text = "\t".join(row["cells"])
            if text.strip():
                self.lines.append(text)
                if self.rows:
                    self.rows[-1]["cell"].append(text)
        elif tag in {"p", "h1", "h2", "h3"} and not self.rows:
            self.flush()

    def handle_data(self, data):
        if not self.hidden:
            (self.rows[-1]["cell"] if self.rows else self.outside).append(data)

    def text(self):
        self.flush()
        return "\n".join(self.lines)


def visible_text(raw):
    parser = VisibleText()
    parser.feed(raw)
    return parser.text()


def cash_reconciliation(text):
    """Check cash identities without replacing issuer labels or guessing merger flows."""
    rows = {}
    for line in text.splitlines():
        cells = line.split("\t")
        if len(cells) >= 3:
            rows[re.sub(r"\s+", "", cells[0])] = cells[1:3]
    labels = ("영업활동현금흐름", "투자활동현금흐름", "재무활동현금흐름",
              "현금및현금성자산에대한환율변동효과", "기초현금및현금성자산", "기말현금및현금성자산")
    def amount(value):
        value = value.replace(",", "").replace(" ", "")
        if value.startswith("(") and value.endswith(")"):
            value = "-" + value[1:-1]
        if not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value):
            raise InvalidOperation
        return Decimal(value)
    checks = []
    if not all(label in rows for label in labels):
        return checks
    for column in range(2):
        try:
            ocf, investing, financing, fx, opening, closing = (amount(rows[label][column]) for label in labels)
            total = ocf + investing + financing + fx
            change = closing - opening
            merger_labels = [label for label, values in rows.items()
                             if "합병" in label and amount(values[column]) == change]
        except InvalidOperation:
            continue
        checks.append({"comparison_column": column + 1, "opening_to_closing_change": str(change),
                       "operating_investing_financing_fx_sum": str(total), "cash_change_reconciles": total == change,
                       "merger_labeled_row_equals_total_change": merger_labels,
                       "additional_merger_inflow_not_supported_by_identity": bool(merger_labels) and total == change,
                       "scope": "Original table units; arithmetic check only. Verify notes before attributing a merger inflow."})
    return checks


def checked_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != HOST or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.fragment
            or parsed.path not in {"/dsaf001/main.do", "/report/viewer.do", SEARCH_PATH}):
        raise SourceError("OFFICIAL_DART_URL_REQUIRED")
    return url


class OfficialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        checked_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class PublicFetcher:
    def __init__(self, budget_seconds=70, max_requests=MAX_SECTIONS + 1):
        if not isinstance(max_requests, int) or not 1 <= max_requests <= MAX_RESEARCH_REQUESTS:
            raise SourceError("INVALID_REQUEST_LIMIT")
        self.deadline = time.monotonic() + budget_seconds
        self.requests = 0
        self.max_requests = max_requests
        self.opener = urllib.request.build_opener(OfficialRedirect())

    def __call__(self, url, *, data=None):
        checked_url(url)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or self.requests >= self.max_requests:
            raise SourceError("SOURCE_BUDGET_EXHAUSTED")
        self.requests += 1
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        if body is not None and urllib.parse.urlsplit(url).path != SEARCH_PATH:
            raise SourceError("PUBLIC_SEARCH_POST_ONLY")
        request = urllib.request.Request(url, data=body, headers={"User-Agent": "stock-assistant/holding-research",
                                                     "Cache-Control": "no-cache", "Pragma": "no-cache"})
        try:
            with self.opener.open(request, timeout=min(12, remaining)) as response:
                checked_url(response.geturl())
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise SourceError("SOURCE_TOO_LARGE")
                charset = (response.headers.get_content_charset() or "utf-8").lower()
                charset = {"ms949": "cp949", "ks_c_5601-1987": "euc-kr", "utf8": "utf-8"}.get(charset, charset)
                if charset not in {"utf-8", "euc-kr", "euc_kr", "cp949"}:
                    raise SourceError("UNSUPPORTED_SOURCE_ENCODING")
                return raw.decode(charset)
        except urllib.error.HTTPError as error:
            raise SourceError("DART_HTTP_" + str(error.code)) from None
        except (urllib.error.URLError, TimeoutError, OSError, UnicodeError):
            raise SourceError("DART_SOURCE_UNAVAILABLE") from None


def filing_identity(receipt, company, as_of):
    if not re.fullmatch(r"[0-9]{14}", receipt):
        raise SourceError("INVALID_RECEIPT")
    try:
        published = datetime.strptime(receipt[:8], "%Y%m%d").date()
    except ValueError:
        raise SourceError("INVALID_RECEIPT_DATE") from None
    if published > as_of:
        raise SourceError("FUTURE_FILING")
    if not company.strip() or len(company) > 80 or any(ord(c) < 32 for c in company):
        raise SourceError("INVALID_COMPANY")
    return published


def document_nodes(raw, receipt):
    # Parse only numeric viewer parameters; never execute the filing's JavaScript.
    nodes = []
    for match in re.finditer(r"node[123]\['text'\]\s*=\s*\"([^\"]+)\";", raw):
        block = raw[match.end():match.end() + 900]
        fields = {}
        for field in ("rcpNo", "dcmNo", "eleId", "offset", "length", "dtd"):
            found = re.search(r"\['" + field + r"'\]\s*=\s*\"([^\"]+)\"", block)
            if found:
                fields[field] = found.group(1)
        if fields.get("rcpNo") != receipt or set(fields) != {"rcpNo", "dcmNo", "eleId", "offset", "length", "dtd"}:
            continue
        if fields["dtd"] != "dart4.xsd" or not all(fields[k].isdigit() for k in ("dcmNo", "eleId", "offset", "length")):
            continue
        nodes.append({"title": html.unescape(match.group(1)), "params": fields})
    return nodes


def read_filing(receipt, company, *, sections="financials,funding", as_of=None, fetch=None):
    observed = datetime.now(timezone.utc)
    as_of = as_of or observed.astimezone(KST).date()
    if as_of > observed.astimezone(KST).date():
        raise SourceError("FUTURE_AS_OF")
    published = filing_identity(receipt, company, as_of)
    wanted = set(sections.split(","))
    if not wanted or not wanted <= {"financials", "funding", "cashflow_notes"}:
        raise SourceError("INVALID_SECTION_SELECTION")
    fetch = fetch or PublicFetcher()
    source = "https://" + HOST + "/dsaf001/main.do?rcpNo=" + receipt
    raw = fetch(source)
    title = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    title = html.unescape(title.group(1)).strip() if title else ""
    if title.split("/", 1)[0].strip() != company.strip():
        raise SourceError("FILING_COMPANY_MISMATCH")
    nodes = document_nodes(raw, receipt)
    selected = []
    for node in nodes:
        name = node["title"]
        if (("financials" in wanted and re.search(r"^[24]-[14]\.\s*(?:연결 )?(?:재무상태표|현금흐름표)", name))
                or ("funding" in wanted and re.search(r"당기손익.*금융자산|차입금|전환사채|사용이 제한된 금융자산|우발부채.*약정|(?:재무위험관리|금융위험관리|유동성위험|위험관리)(?:\s*\(연결\))?$", name))
                or ("cashflow_notes" in wanted and re.match(r"[0-9]+\.\s*현금흐름표", name))):
            if node not in selected:
                selected.append(node)
    warnings = []
    if not nodes:
        # Exchange-style announcements have a single HTML viewer instead of a tree.
        initial = re.search(r'viewDoc\("' + receipt + r'",\s*"(\d+)",\s*"(\d+)",\s*"(\d+)",\s*"(\d+)",\s*"HTML"', raw)
        if not initial:
            raise SourceError("DART_VIEWER_NOT_FOUND")
        selected = [{"title": title, "params": dict(zip(("dcmNo", "eleId", "offset", "length"), initial.groups()), rcpNo=receipt, dtd="HTML")}]
    elif not selected:
        raise SourceError("REQUESTED_SECTIONS_NOT_FOUND")
    if len(selected) > MAX_SECTIONS:
        selected = selected[:MAX_SECTIONS]
        warnings.append("SECTION_LIMIT_REACHED")
    documents = []
    for node in selected:
        url = "https://" + HOST + "/report/viewer.do?" + urllib.parse.urlencode(node["params"])
        try:
            text = visible_text(fetch(url))
            if not text.strip():
                raise SourceError("EMPTY_SOURCE")
            truncated = len(text) > MAX_CHARS
            document = {"title": node["title"], "source_url": url, "text": text[:MAX_CHARS], "truncated": truncated}
            if re.match(r"[24]-4\.", node["title"]):
                document["cash_reconciliation"] = cash_reconciliation(text)
            documents.append(document)
            if truncated:
                warnings.append("TRUNCATED_SECTION: " + node["title"])
        except SourceError as error:
            warnings.append(str(error) + ": " + node["title"])
    return {"schema": "HOLDING_DART_SOURCE_V1", "status": "UNAVAILABLE" if not documents else "PARTIAL" if warnings else "FETCHED",
            "company": company, "receipt": receipt, "filing_date": published.isoformat(),
            "observed_at": observed.isoformat(), "source_url": source, "filing_title": title,
            "sections": documents, "warnings": warnings,
            "scope": "Selected original sections only; no investment verdict or coverage certification."}


class FilingList(HTMLParser):
    """Parse public search rows; numeric corporate IDs are data, never executable JS."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.link = None
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = {}
        elif tag == "a" and self.row is not None:
            href = dict(attrs).get("href", "")
            corp = re.match(r"javascript:openCorpInfoNew\('([0-9]{8})'", href)
            parsed = urllib.parse.urlsplit(href)
            if corp:
                self.row["corp_code"] = corp.group(1)
                self.link = "company"
            elif parsed.path == "/dsaf001/main.do" and parsed.netloc in ("", HOST):
                receipts = urllib.parse.parse_qs(parsed.query).get("rcpNo", [])
                if len(receipts) == 1 and re.fullmatch(r"[0-9]{14}", receipts[0]):
                    self.row["receipt"] = receipts[0]
                    self.link = "title"
            self.parts = []

    def handle_data(self, data):
        if self.link:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.link and self.row is not None:
            self.row[self.link] = " ".join(" ".join(self.parts).split())
            self.link = None
        elif tag == "tr" and self.row is not None:
            if self.row.get("receipt"):
                if not all(self.row.get(k) for k in ("corp_code", "company", "title")):
                    raise SourceError("MALFORMED_PUBLIC_LIST_ROW")
                self.rows.append(self.row)
            self.row = None
            self.link = None


def discover_filings(company, *, as_of=None, fetch=None):
    """Find an exact issuer and recent filings from the public DART search form."""
    today = datetime.now(KST).date()
    as_of = as_of or today
    if as_of > today:
        raise SourceError("FUTURE_AS_OF")
    if (not company.strip() or len(company) > 80 or any(ord(c) < 32 for c in company)
            or re.search(r"[!%='\"<>|/]", company) or "--" in company):
        raise SourceError("INVALID_DISCOVERY_COMPANY")
    fetch = fetch or PublicFetcher(budget_seconds=90, max_requests=MAX_RESEARCH_REQUESTS)
    start = as_of - timedelta(days=365)
    filings, warnings, codes = [], [], set()
    total_pages = 1
    for page in range(1, 4):
        raw = fetch("https://" + HOST + SEARCH_PATH, data={
            "option": "corp", "textCrpNm": company.strip(), "textCrpCik": next(iter(codes), ""),
            "startDate": start.strftime("%Y%m%d"), "endDate": as_of.strftime("%Y%m%d"),
            "finalReport": "recent", "maxResults": "100", "currentPage": str(page),
            "sort": "date", "series": "desc", "autoSearch": "Y", "businessCode": "all"})
        parser = FilingList()
        parser.feed(raw)
        exact = [row for row in parser.rows if row["company"] == company.strip()]
        codes.update(row["corp_code"] for row in exact)
        if len(codes) > 1:
            raise SourceError("AMBIGUOUS_PUBLIC_ISSUER")
        if not exact:
            raise SourceError("EXACT_PUBLIC_ISSUER_NOT_FOUND")
        for row in exact:
            published = filing_identity(row["receipt"], company, as_of)
            if published < start:
                raise SourceError("PUBLIC_LIST_DATE_MISMATCH")
            if filings and row["receipt"][:8] > filings[-1]["receipt"][:8]:
                raise SourceError("PUBLIC_LIST_NOT_DATE_DESCENDING")
            row = dict(row, filing_date=published.isoformat(), source_url="https://" + HOST + "/dsaf001/main.do?rcpNo=" + row["receipt"])
            if not any(r["receipt"] == row["receipt"] for r in filings):
                filings.append(row)
        info = re.search(r'class=["\x27]pageInfo["\x27][^>]*>(.*?)</div>', raw, re.S | re.I)
        pages = re.search(r"\[\s*([0-9]+)\s*/\s*([0-9]+)\s*\]", visible_text(info.group(1))) if info else None
        if not pages or int(pages[1]) != page or not page <= int(pages[2]) <= 10_000:
            raise SourceError("PUBLIC_LIST_PAGINATION_UNKNOWN")
        total_pages = int(pages[2])
        if page >= total_pages:
            break
    if total_pages > 3:
        warnings.append("PUBLIC_LIST_PAGE_LIMIT_REACHED")
    return {"company": company.strip(), "corp_code": next(iter(codes)), "filings": filings,
            "observed_at": datetime.now(timezone.utc).isoformat(), "start_date": start.isoformat(),
            "end_date": as_of.isoformat(), "source_url": "https://" + HOST + SEARCH_PATH,
            "warnings": warnings, "scope": "Public final-report search, at most three pages of 100 rows over one year; not historical coverage certification."}


def compact_evidence(filing):
    """Keep cash-flow tables and parent funding notes; link other detail without repeating it."""
    core, references = [], []
    for section in filing["sections"]:
        title = section["title"]
        parent_note = ("(연결)" not in title and re.search(
            r"당기손익.*금융자산|사용이 제한된 금융자산|(?:재무위험관리|금융위험관리|유동성위험|위험관리)$", title))
        if re.match(r"^(?:2-4|4-[14])\.", title) or parent_note:
            core.append(section)
        else:
            references.append({"title": title, "source_url": section["source_url"]})
    return {k: v for k, v in filing.items() if k != "sections"} | {
        "sections": core, "additional_detail_links": references,
        "evidence_characters": {"before_selection": len(json.dumps(filing["sections"], ensure_ascii=False)),
                                "selected": len(json.dumps(core, ensure_ascii=False))},
        "scope": "Selected CFS cash flow and OFS cash/position/funding evidence. Other linked details are not reproduced. No investment verdict."}


def event_terms(event):
    """Separate a named event subject from generic request wording, without stripping issuer names."""
    terms = event.split()
    generic = {"인수", "합병", "증자", "투자", "자금", "부담", "위험", "관련", "추진", "계획", "현황", "검토", "조건", "acquisition", "merger"}
    while len(terms) > 1 and terms[-1] in generic:
        terms.pop()
    if len(terms) == 1 and terms[0] not in generic and terms[0] != "유상증자":
        # Remove one action suffix only: an issuer name ending in '투자' must survive.
        terms[0] = re.sub(r"(?<=.)(?:인수부담|합병부담|인수자금|합병자금|인수|합병|증자)$", "", terms[0])
    terms = [re.sub(r"[\W_]", "", term) for term in terms]
    if not terms or not all(terms):
        raise SourceError("INVALID_EVENT_TERM")
    return terms


def research_holding(company, *, event=None, period="latest", as_of=None, fetch=None):
    """One bounded, fresh discovery + original-reading operation; no issuer/receipt hints."""
    started = time.monotonic()
    if period not in {"latest", "half", "quarter", "annual"}:
        raise SourceError("INVALID_REPORT_PERIOD")
    if event is not None and (not event.strip() or len(event) > 80 or any(ord(c) < 32 for c in event)):
        raise SourceError("INVALID_EVENT_TERM")
    matched_terms = event_terms(event) if event else []
    fetch = fetch or PublicFetcher(budget_seconds=90, max_requests=MAX_RESEARCH_REQUESTS)
    discovery = discover_filings(company, as_of=as_of, fetch=fetch)
    after_discovery = time.monotonic()
    pattern = {"latest": r"(?:사업|반기|분기)보고서", "half": r"반기보고서",
               "quarter": r"분기보고서", "annual": r"사업보고서"}[period]
    candidates = [r for r in discovery["filings"] if re.search(pattern, r["title"])]
    def reporting_period(row):
        found = re.search(r"\((20[0-9]{2})[.]\s*(0[1-9]|1[0-2])\)", row["title"])
        return tuple(map(int, found.groups())) if found else (0, 0)
    selected = max(candidates, key=reporting_period, default=None)
    if not selected:
        raise SourceError("PERIODIC_FILING_NOT_FOUND_IN_SEARCH_WINDOW")
    if reporting_period(selected) > ((as_of or datetime.now(KST).date()).year, (as_of or datetime.now(KST).date()).month):
        raise SourceError("FUTURE_REPORT_PERIOD")
    financial = compact_evidence(read_filing(selected["receipt"], company, sections="financials,funding,cashflow_notes", as_of=as_of, fetch=fetch))
    warnings = list(discovery["warnings"]) + list(financial["warnings"])
    titles = [s["title"] for s in financial["sections"]]
    coverage = {"cfs_cash_flow": any(re.match(r"2-4\.", t) for t in titles),
                "ofs_balance_sheet": any(re.match(r"4-1\.", t) for t in titles),
                "ofs_cash_flow": any(re.match(r"4-4\.", t) for t in titles),
                "ofs_financial_investments": any("당기손익" in t and "금융자산" in t for t in titles),
                "ofs_restrictions": any("사용이 제한된 금융자산" in t for t in titles),
                "ofs_maturity_note": any(re.search(r"(?:재무위험관리|금융위험관리|유동성위험|위험관리)$", t) for t in titles)}
    warnings.extend("EVIDENCE_NOT_LOCATED: " + name for name, found in coverage.items() if not found)
    if reporting_period(selected) == (0, 0):
        warnings.append("REPORT_PERIOD_NOT_PARSED")
    elif any(reporting_period(r) == (0, 0) for r in candidates):
        warnings.append("UNPARSED_PERIODIC_CANDIDATE")
    if not financial["sections"]:
        warnings.append("FINANCIAL_ORIGINALS_UNAVAILABLE")
    after_financial = time.monotonic()
    events, scanned = [], 0
    if event:
        for candidate in discovery["filings"]:
            if not re.search(r"해명|타법인|양수|인수|주요사항", candidate["title"]):
                continue
            if scanned >= 6:
                warnings.append("EVENT_CANDIDATE_LIMIT_REACHED")
                break
            scanned += 1
            try:
                evidence = read_filing(candidate["receipt"], company, as_of=as_of, fetch=fetch)
            except SourceError as error:
                warnings.append(str(error) + ": " + candidate["receipt"])
                continue
            if any(all(term in re.sub(r"[\W_]", "", s["text"]) for term in matched_terms) for s in evidence["sections"]):
                events.append(evidence)
                warnings.extend(evidence["warnings"])
                break
            if evidence["status"] != "FETCHED":
                warnings.append("EVENT_CANDIDATE_INCOMPLETE: " + candidate["receipt"])
        if not events:
            warnings.append("EVENT_ORIGINAL_NOT_FOUND_IN_BOUNDED_SEARCH")
    finished = time.monotonic()
    return {"schema": "HOLDING_DART_RESEARCH_V1", "status": "UNAVAILABLE" if not financial["sections"] else "PARTIAL" if warnings else "FETCHED",
            "company": company, "observed_at": datetime.now(timezone.utc).isoformat(),
            "discovery": {k: v for k, v in discovery.items() if k != "filings"} | {
                "filing_count": len(discovery["filings"]), "latest_filings": discovery["filings"][:6],
                "selected_periodic": selected},
            "financial": financial, "event_term": event, "matched_event_terms": matched_terms, "events": events,
            "event_candidates_read": scanned, "evidence_located": coverage, "warnings": warnings,
            "requests_used": getattr(fetch, "requests", None),
            "timings_seconds": {"discovery": round(after_discovery-started, 3), "financial": round(after_financial-after_discovery, 3),
                                "events": round(finished-after_financial, 3), "total": round(finished-started, 3)},
            "scope": "Fresh public originals in a bounded search. Missing items and partial evidence are not negative events or an investment verdict."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt")
    parser.add_argument("--company", required=True)
    parser.add_argument("--sections", default="financials,funding")
    parser.add_argument("--event")
    parser.add_argument("--period", choices=("latest", "half", "quarter", "annual"), default="latest")
    args = parser.parse_args(argv)
    try:
        result = (read_filing(args.receipt, args.company, sections=args.sections) if args.receipt
                  else research_holding(args.company, event=args.event, period=args.period))
    except SourceError as error:
        result = {"schema": "HOLDING_DART_SOURCE_V1" if args.receipt else "HOLDING_DART_RESEARCH_V1",
                  "status": "UNAVAILABLE", "reason": str(error), "attempted_at": datetime.now(timezone.utc).isoformat()}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("sections") or result.get("financial", {}).get("sections") else 1


if __name__ == "__main__":
    raise SystemExit(main())
