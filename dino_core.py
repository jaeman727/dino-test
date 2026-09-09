"""
디노테스트 코어 로직.

원본 터미널 스크립트에서 계산/조회 부분만 떼어낸 모듈입니다.
이 파일에는 print() 와 input() 이 하나도 없습니다.
모든 함수는 값을 '반환'만 하고, 화면 표시는 app.py 가 담당합니다.

진행 상황은 progress(done, total, message) 형태의 콜백으로 전달합니다.
"""

from __future__ import annotations

import html
import io
import os
import re
import time
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

import pandas as pd
import requests

# ------------------------------------------------------------------
# 선택적 의존성
# ------------------------------------------------------------------
try:
    from pykrx import stock as pykrx_stock
    HAS_PYKRX = True
except Exception:
    pykrx_stock = None
    HAS_PYKRX = False

try:
    import yfinance as yf
    HAS_YFINANCE = True
except Exception:
    yf = None
    HAS_YFINANCE = False


# ==================================================================
# 1. 설정
# ==================================================================
MIN_COMPANY_SCORE = 16
MIN_PRICE_SCORE = 8
NAVER_HISTORY_COUNT = 2000

STRATEGIES = [
    (16, 8), (15, 8), (16, 7), (15, 7),
    (14, 7), (14, 6), (13, 7), (13, 6),
    (12, 7), (12, 6),
]

DEFAULT_MULTI_DATES = [
    "2024-08-01", "2024-11-01", "2025-02-01",
    "2025-05-01", "2025-08-01", "2025-11-01",
    "2026-02-01",
]

USER_AGENT = "Mozilla/5.0 (compatible; DinoTest/1.0)"


@dataclass
class Keys:
    """API 키. 코드에 직접 쓰지 않고 환경변수나 st.secrets 로만 받습니다."""
    dart: str = ""
    naver_id: str = ""
    naver_secret: str = ""

    @property
    def has_dart(self) -> bool:
        return bool(self.dart.strip())

    @property
    def has_naver(self) -> bool:
        return bool(self.naver_id.strip() and self.naver_secret.strip())


def keys_from_env() -> Keys:
    return Keys(
        dart=os.environ.get("DART_API_KEY", ""),
        naver_id=os.environ.get("NAVER_CLIENT_ID", ""),
        naver_secret=os.environ.get("NAVER_CLIENT_SECRET", ""),
    )


# ==================================================================
# 2. 공통 유틸
# ==================================================================
def to_number(v):
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return None


def normalize_company_name(name) -> str:
    if name is None:
        return ""
    s = str(name)
    for word in ["주식회사", "(주)", "㈜"]:
        s = s.replace(word, "")
    return re.sub(r"[\s\-_.,]", "", s).strip().lower()


def parse_listing_date(v) -> Optional[datetime]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    if s.endswith(".0"):
        s = s[:-2]
    digits = re.sub(r"[^0-9]", "", s)
    if len(digits) == 8:
        try:
            return datetime.strptime(digits, "%Y%m%d")
        except ValueError:
            pass
    try:
        x = pd.to_datetime(v, errors="coerce")
        return None if pd.isna(x) else x.to_pydatetime()
    except Exception:
        return None


def clean_text(text) -> str:
    if text is None:
        return ""
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def calculate_return(start_price, end_price):
    if start_price in (None, 0) or end_price is None:
        return None
    return (end_price - start_price) / start_price * 100


def _noop(done: int, total: int, message: str = "") -> None:
    return None


ProgressFn = Callable[[int, int, str], None]


# ==================================================================
# 3. 기준 데이터 (DART 회사코드 + KRX 상장사)
#    원본은 실행할 때마다 새로 받았지만, 여기서는 한 번 받아 객체에 담고
#    app.py 가 캐시합니다.
# ==================================================================
@dataclass
class MarketIndex:
    krx_companies: list = field(default_factory=list)
    dart_by_stock_code: dict = field(default_factory=dict)
    dart_by_name: dict = field(default_factory=dict)
    loaded_at: Optional[datetime] = None
    krx_source: str = ""

    # -------------------- 조회 --------------------
    def resolve(self, krx_company: dict) -> Optional[dict]:
        code = krx_company["stock_code"]
        d = self.dart_by_stock_code.get(code)
        if d:
            return {
                **d,
                "stock_code": code,
                "match_method": "종목코드",
                "listing_date": krx_company.get("listing_date"),
                "market": krx_company.get("market", ""),
            }
        candidates = self.dart_by_name.get(
            normalize_company_name(krx_company["company_name"]), []
        )
        if candidates:
            listed = [x for x in candidates if x.get("stock_code")]
            d = listed[0] if listed else candidates[0]
            return {
                **d,
                "stock_code": code,
                "match_method": "회사명",
                "listing_date": krx_company.get("listing_date"),
                "market": krx_company.get("market", ""),
            }
        return None

    def find_company(self, company_name: str) -> Optional[dict]:
        target = normalize_company_name(company_name)
        if not target:
            return None
        exact = [c for c in self.krx_companies if c["normalized_name"] == target]
        if exact:
            return self.resolve(exact[0])
        partial = [c for c in self.krx_companies if target in c["normalized_name"]]
        if not partial:
            return None
        partial.sort(key=lambda c: abs(len(c["normalized_name"]) - len(target)))
        return self.resolve(partial[0])

    def search_names(self, keyword: str, limit: int = 20) -> list:
        target = normalize_company_name(keyword)
        if not target:
            return []
        hits = [c for c in self.krx_companies if target in c["normalized_name"]]
        hits.sort(key=lambda c: abs(len(c["normalized_name"]) - len(target)))
        return hits[:limit]


def _fetch_dart_corp_map(dart_key: str) -> tuple[dict, dict]:
    r = requests.get(
        "https://opendart.fss.or.kr/api/corpCode.xml",
        params={"crtfc_key": dart_key},
        timeout=30,
    )
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    root = ET.fromstring(z.read("CORPCODE.xml"))

    by_code, by_name = {}, {}
    for item in root.findall("list"):
        name = (item.findtext("corp_name") or "").strip()
        corp_code = (item.findtext("corp_code") or "").strip()
        stock_code = (item.findtext("stock_code") or "").strip()
        d = {"corp_name": name, "corp_code": corp_code, "stock_code": stock_code}
        if stock_code:
            by_code[stock_code] = d
        n = normalize_company_name(name)
        if n:
            by_name.setdefault(n, []).append(d)
    return by_code, by_name


def _fetch_krx_from_kind() -> list:
    url = "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download"
    r = requests.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    tables = pd.read_html(io.BytesIO(r.content))
    if not tables:
        return []
    df = tables[0]

    company_col = code_col = listing_col = market_col = None
    for c in df.columns:
        t = str(c).strip()
        if "회사명" in t:
            company_col = c
        if "종목코드" in t or "종목 코드" in t:
            code_col = c
        if "상장일" in t:
            listing_col = c
        if "시장구분" in t or "시장 구분" in t:
            market_col = c
    if company_col is None or code_col is None:
        return []

    out = []
    for _, row in df.iterrows():
        if pd.isna(row[company_col]) or pd.isna(row[code_col]):
            continue
        name = str(row[company_col]).strip()
        code = re.sub(r"[^0-9]", "", str(row[code_col]).replace(".0", "").strip()).zfill(6)
        if not code:
            continue
        listing = parse_listing_date(row[listing_col]) if listing_col is not None else None
        market = ""
        if market_col is not None and not pd.isna(row[market_col]):
            market = str(row[market_col]).strip()
        out.append({
            "company_name": name,
            "normalized_name": normalize_company_name(name),
            "stock_code": code,
            "listing_date": listing,
            "market": market,
        })
    return out


def _fetch_krx_from_pykrx() -> list:
    """KIND 다운로드가 막혔을 때의 예비 경로. 상장일은 얻을 수 없습니다."""
    if not HAS_PYKRX:
        return []
    today = datetime.now().strftime("%Y%m%d")
    out = []
    for market in ("KOSPI", "KOSDAQ"):
        try:
            tickers = pykrx_stock.get_market_ticker_list(today, market=market)
        except Exception:
            continue
        for code in tickers:
            try:
                name = pykrx_stock.get_market_ticker_name(code)
            except Exception:
                continue
            out.append({
                "company_name": str(name).strip(),
                "normalized_name": normalize_company_name(name),
                "stock_code": str(code).zfill(6),
                "listing_date": None,
                "market": market,
            })
    return out


def load_market_index(dart_key: str) -> MarketIndex:
    """DART 회사코드 + KRX 상장사 목록을 한 번에 준비합니다."""
    by_code, by_name = _fetch_dart_corp_map(dart_key)

    krx, source = [], ""
    try:
        krx = _fetch_krx_from_kind()
        source = "KRX KIND"
    except Exception:
        krx = []
    if not krx:
        krx = _fetch_krx_from_pykrx()
        source = "pykrx (상장일 없음)"
    if not krx:
        raise RuntimeError("상장사 목록을 가져오지 못했습니다. 네트워크나 KRX 접근을 확인하세요.")

    return MarketIndex(
        krx_companies=krx,
        dart_by_stock_code=by_code,
        dart_by_name=by_name,
        loaded_at=datetime.now(),
        krx_source=source,
    )


# ==================================================================
# 4. 주가 데이터 계층
#    원본은 yfinance 를 먼저 썼지만, 서버(클라우드) 환경에서 Yahoo 가
#    데이터센터 IP를 막는 경우가 많아 pykrx 를 1순위로 둡니다.
# ==================================================================
REQUIRED_COLS = ["Open", "High", "Low", "Close", "Volume"]


def _clean_frame(data) -> Optional[pd.DataFrame]:
    if data is None or len(data) == 0:
        return None
    if isinstance(data.columns, pd.MultiIndex):
        try:
            if "Close" in data.columns.get_level_values(0):
                data.columns = data.columns.get_level_values(0)
            elif "Close" in data.columns.get_level_values(-1):
                data.columns = data.columns.get_level_values(-1)
        except Exception:
            pass
    if any(c not in data.columns for c in REQUIRED_COLS):
        return None
    data = data[REQUIRED_COLS].copy().dropna(subset=["Close"])
    if data.empty:
        return None
    if getattr(data.index, "tz", None) is not None:
        data.index = data.index.tz_localize(None)
    return data


def _price_from_pykrx(code, start, end) -> Optional[pd.DataFrame]:
    if not HAS_PYKRX:
        return None
    try:
        df = pykrx_stock.get_market_ohlcv(
            pd.Timestamp(start).strftime("%Y%m%d"),
            pd.Timestamp(end).strftime("%Y%m%d"),
            code,
        )
    except Exception:
        return None
    if df is None or df.empty:
        return None
    rename = {"시가": "Open", "고가": "High", "저가": "Low", "종가": "Close", "거래량": "Volume"}
    df = df.rename(columns=rename)
    return _clean_frame(df)


def _price_from_naver(code, start, end, count=NAVER_HISTORY_COUNT) -> Optional[pd.DataFrame]:
    try:
        r = requests.get(
            "https://fchart.stock.naver.com/sise.nhn",
            params={"symbol": code, "timeframe": "day", "count": count, "requestType": "0"},
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        r.raise_for_status()
        text = r.content.decode("euc-kr", errors="replace")
        text = re.sub(r"<\?xml[^>]*\?>", "", text).strip()
        root = ET.fromstring(text)
        rows = []
        for item in root.findall(".//item"):
            parts = item.attrib.get("data", "").split("|")
            if len(parts) < 6:
                continue
            try:
                rows.append({
                    "Date": pd.to_datetime(parts[0], format="%Y%m%d"),
                    "Open": float(parts[1]), "High": float(parts[2]),
                    "Low": float(parts[3]), "Close": float(parts[4]),
                    "Volume": float(parts[5]),
                })
            except ValueError:
                continue
        if not rows:
            return None
        df = (pd.DataFrame(rows).drop_duplicates(subset=["Date"])
              .sort_values("Date").set_index("Date"))
        df = df[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
        return _clean_frame(df)
    except Exception:
        return None


def _price_from_yahoo(code, start, end) -> Optional[pd.DataFrame]:
    if not HAS_YFINANCE:
        return None
    for ticker in (code + ".KS", code + ".KQ"):
        try:
            df = yf.Ticker(ticker).history(
                start=start, end=end, interval="1d", auto_adjust=False, actions=False
            )
            cleaned = _clean_frame(df)
            if cleaned is not None:
                return cleaned
        except Exception:
            pass
        time.sleep(0.05)
    return None


def get_price_history(stock_code, start, end) -> dict:
    """pykrx → 네이버 → Yahoo 순으로 시도하고 어디서 가져왔는지 함께 돌려줍니다."""
    code = str(stock_code).strip().zfill(6)
    for name, fn in (
        ("pykrx", _price_from_pykrx),
        ("네이버 차트", _price_from_naver),
        ("Yahoo Finance", _price_from_yahoo),
    ):
        df = fn(code, start, end)
        if df is not None and len(df) > 0:
            return {"success": True, "source": name, "data": df, "reason": None}
    return {"success": False, "source": None, "data": None, "reason": "주가 데이터 없음"}


def get_row_on_or_after(data: pd.DataFrame, target_date):
    target = pd.Timestamp(target_date)
    if getattr(target, "tzinfo", None) is not None:
        target = target.tz_localize(None)
    result = data[data.index >= target]
    if result.empty:
        return None
    value = result["Close"].iloc[0]
    if isinstance(value, pd.Series):
        value = value.iloc[0]
    return {"date": result.index[0], "price": float(value)}


def get_market_snapshot(stock_code, base_date=None) -> Optional[dict]:
    """시가총액·PER·PBR. pykrx 우선, 실패 시 Yahoo info."""
    code = str(stock_code).strip().zfill(6)
    end = pd.Timestamp(base_date or datetime.now())
    start = end - timedelta(days=15)

    if HAS_PYKRX:
        try:
            cap = pykrx_stock.get_market_cap(
                start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), code
            )
            fun = pykrx_stock.get_market_fundamental(
                start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), code
            )
            market_cap = shares = per = pbr = None
            if cap is not None and not cap.empty:
                last = cap.iloc[-1]
                market_cap = float(last.get("시가총액")) if "시가총액" in cap.columns else None
                shares = float(last.get("상장주식수")) if "상장주식수" in cap.columns else None
            if fun is not None and not fun.empty:
                last = fun.iloc[-1]
                per = float(last.get("PER")) if "PER" in fun.columns else None
                pbr = float(last.get("PBR")) if "PBR" in fun.columns else None
            per = per if per and per > 0 else None
            pbr = pbr if pbr and pbr > 0 else None
            if market_cap:
                return {"market_cap": market_cap, "shares": shares,
                        "per": per, "pbr": pbr, "source": "pykrx"}
        except Exception:
            pass

    if HAS_YFINANCE:
        for ticker in (code + ".KS", code + ".KQ"):
            try:
                info = yf.Ticker(ticker).get_info()
                if info and info.get("marketCap"):
                    return {
                        "market_cap": info.get("marketCap"),
                        "shares": info.get("sharesOutstanding"),
                        "per": info.get("trailingPE"),
                        "pbr": info.get("priceToBook"),
                        "source": "Yahoo Finance",
                    }
            except Exception:
                continue
    return None


# ==================================================================
# 5. DART 조회
# ==================================================================
def dart_list(dart_key, company, start_date, end_date, pblntf_ty=None) -> dict:
    """
    pblntf_ty 를 인자로 뺐습니다.
    - 사업보고서/정기보고서를 찾을 때는 "A"
    - 유상증자·공급계약 같은 재료성 공시를 볼 때는 None (전체 유형)

    원본 코드는 공시 점수 계산에도 "A"(정기공시)를 넣고 있어서,
    '단일판매ㆍ공급계약체결', '유상증자결정' 같은 항목이 결과에 들어올 수 없었습니다.
    그래서 재료점수가 사실상 항상 2점으로 고정되던 문제가 있습니다.
    """
    params = {
        "crtfc_key": dart_key,
        "corp_code": company["corp_code"],
        "bgn_de": pd.Timestamp(start_date).strftime("%Y%m%d"),
        "end_de": pd.Timestamp(end_date).strftime("%Y%m%d"),
        "sort": "date",
        "sort_mth": "desc",
        "page_count": 100,
    }
    if pblntf_ty:
        params["pblntf_ty"] = pblntf_ty
    try:
        return requests.get(
            "https://opendart.fss.or.kr/api/list.json", params=params, timeout=20
        ).json()
    except Exception:
        return {}


REVENUE_NAMES = ("매출액", "수익(매출액)", "영업수익", "매출")
OPERATING_NAMES = ("영업이익", "영업이익(손실)", "영업손실")
NET_NAMES = ("당기순이익", "당기순이익(손실)", "연결당기순이익", "당기순손실",
             "당기순이익(당기순손실)")


def _pick_accounts(rows, fs_div=None) -> dict:
    """DART 계정 목록에서 필요한 5개 항목을 뽑습니다."""
    revenue = op = net = liabilities = equity = None
    for row in rows:
        if fs_div and row.get("fs_div") != fs_div:
            continue
        name = str(row.get("account_nm", "")).strip()
        amount = to_number(row.get("thstrm_amount"))
        if amount is None:
            continue
        if name in REVENUE_NAMES and revenue is None:
            revenue = amount
        elif name in OPERATING_NAMES and op is None:
            op = amount
        elif name in NET_NAMES and net is None:
            net = amount
        elif name == "부채총계" and liabilities is None:
            liabilities = amount
        elif name == "자본총계" and equity is None:
            equity = amount
    return {"revenue": revenue, "operating_income": op, "net_income": net,
            "liabilities": liabilities, "equity": equity}


def _build_finance(picked: dict, year, source: str) -> dict:
    liabilities, equity = picked["liabilities"], picked["equity"]
    op, revenue, net = picked["operating_income"], picked["revenue"], picked["net_income"]

    debt_ratio = (liabilities / equity * 100
                  if liabilities is not None and equity not in (None, 0) else None)
    margin = (op / revenue * 100
              if op is not None and revenue not in (None, 0) else None)
    roe = (net / equity * 100
           if net is not None and equity not in (None, 0) else None)

    roe_score = (3 if roe is not None and roe >= 15 else
                 2 if roe is not None and roe >= 8 else
                 1 if roe is not None and roe >= 3 else 0)
    debt_score = (2 if debt_ratio is not None and debt_ratio <= 80 else
                  1 if debt_ratio is not None and debt_ratio <= 150 else 0)

    return {
        "success": True, "business_year": str(year), "source": source,
        "revenue": revenue, "operating_income": op, "net_income": net,
        "liabilities": liabilities, "equity": equity,
        "debt_ratio": debt_ratio, "operating_margin": margin, "roe": roe,
        "finance_score": roe_score + debt_score,
    }


def analyze_finance(dart_key, company, year, reprt_code="11011") -> dict:
    """
    재무제표를 세 경로로 시도합니다.

    1) 주요계정 (fnlttSinglAcnt)                 — 가장 가볍고 빠름
    2) 전체 재무제표 연결 (fnlttSinglAcntAll, CFS)
    3) 전체 재무제표 개별 (fnlttSinglAcntAll, OFS)

    종속회사가 없어 연결재무제표를 만들지 않는 소형주는 1)에서 빈 응답이
    돌아오는 경우가 있습니다. 원래는 여기서 바로 실패 처리했지만,
    이제 2)와 3)까지 내려가며 개별재무제표로 다시 찾습니다.

    항상 dict 를 돌려줍니다. 실패 시 {"success": False, "reason": ...}.
    """
    base = {"crtfc_key": dart_key, "corp_code": company["corp_code"],
            "bsns_year": str(year), "reprt_code": reprt_code}
    statuses = []

    # 1) 주요계정
    try:
        data = requests.get("https://opendart.fss.or.kr/api/fnlttSinglAcnt.json",
                            params=base, timeout=20).json()
        statuses.append(f"주요계정 {data.get('status')}")
        if data.get("status") == "000":
            rows = data.get("list", [])
            fs_div = "CFS" if any(x.get("fs_div") == "CFS" for x in rows) else "OFS"
            picked = _pick_accounts(rows, fs_div)
            if picked["equity"] is not None:
                return _build_finance(picked, year, f"주요계정({fs_div})")
    except Exception as e:
        statuses.append(f"주요계정 오류 {type(e).__name__}")

    # 2)~3) 전체 재무제표
    for fs_div in ("CFS", "OFS"):
        try:
            data = requests.get("https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json",
                                params={**base, "fs_div": fs_div}, timeout=30).json()
            statuses.append(f"전체{fs_div} {data.get('status')}")
            if data.get("status") != "000":
                continue
            picked = _pick_accounts(data.get("list", []))
            if picked["equity"] is not None:
                return _build_finance(picked, year, f"전체재무제표({fs_div})")
        except Exception as e:
            statuses.append(f"전체{fs_div} 오류 {type(e).__name__}")

    return {"success": False,
            "reason": f"{year}년 재무정보 없음 ({' / '.join(statuses)})"}



def find_latest_annual_report_asof(dart_key, company, base_date) -> Optional[dict]:
    data = dart_list(dart_key, company, base_date - timedelta(days=1200), base_date, "A")
    if data.get("status") != "000":
        return None
    for item in data.get("list", []):
        name = item.get("report_nm", "")
        if "사업보고서" not in name:
            continue
        m = re.search(r"\((\d{4})\.", name)
        if m:
            return {"business_year": m.group(1), "report_name": name,
                    "receipt_date": item.get("rcept_dt")}
    return None


def find_latest_periodic_report_asof(dart_key, company, base_date) -> Optional[dict]:
    data = dart_list(dart_key, company, base_date - timedelta(days=800), base_date, "A")
    if data.get("status") != "000":
        return None
    for item in data.get("list", []):
        name = item.get("report_nm", "")
        code = None
        if "1분기보고서" in name:
            code = "11013"
        elif "반기보고서" in name:
            code = "11012"
        elif "3분기보고서" in name:
            code = "11014"
        elif "사업보고서" in name:
            code = "11011"
        if code is None:
            continue
        m = re.search(r"\((\d{4})\.", name)
        if m:
            return {"business_year": m.group(1), "report_code": code}
    return None


def get_issued_shares(dart_key, company, business_year, report_code):
    try:
        data = requests.get(
            "https://opendart.fss.or.kr/api/stockTotqySttus.json",
            params={"crtfc_key": dart_key, "corp_code": company["corp_code"],
                    "bsns_year": str(business_year), "reprt_code": report_code},
            timeout=20,
        ).json()
    except Exception:
        return None
    if data.get("status") != "000":
        return None
    for key in ("보통주", "합계"):
        for row in data.get("list", []):
            if key in str(row.get("se", "")):
                shares = to_number(row.get("istc_totqy"))
                if shares not in (None, 0):
                    return shares
    return None


# ==================================================================
# 6. 점수 계산 (원본 로직 그대로)
# ==================================================================
def score_valuation(per, pbr) -> dict:
    per_score = (3 if per is not None and 0 < per <= 10 else
                 2 if per is not None and per <= 15 else
                 1 if per is not None and per <= 25 else 0)
    pbr_score = (2 if pbr is not None and 0 < pbr <= 1 else
                 1 if pbr is not None and pbr <= 2 else 0)
    value_score = 0
    if per is not None:
        value_score += 2 if 0 < per <= 10 else 1 if per <= 15 else 0
    if pbr is not None and 0 < pbr <= 1:
        value_score += 1
    return {"per": per, "pbr": pbr,
            "company_valuation_score": per_score + pbr_score,
            "price_value_score": value_score}


def calculate_technical(data: pd.DataFrame, as_of=None) -> dict:
    """현재 시점과 과거 시점을 하나의 함수로 통합했습니다."""
    past = data
    if as_of is not None:
        target = pd.Timestamp(as_of)
        if getattr(target, "tzinfo", None) is not None:
            target = target.tz_localize(None)
        past = data[data.index <= target]
    if len(past) < 120:
        return {"success": False, "reason": "120거래일 미만"}

    close, high, low, volume = past["Close"], past["High"], past["Low"], past["Volume"]
    current = float(close.iloc[-1])
    ma20 = float(close.tail(20).mean())
    ma60 = float(close.tail(60).mean())
    ma120 = float(close.tail(120).mean())
    high52 = float(high.tail(252).max())
    low52 = float(low.tail(252).min())

    rng = high52 - low52
    position = (current - low52) / rng * 100 if rng > 0 else 50
    pos_score = 3 if position <= 30 else 2 if position <= 50 else 1 if position <= 75 else 0

    gap = (current - ma20) / ma20 * 100 if ma20 > 0 else 0
    gap_score = 2 if -5 <= gap <= 5 else 1 if (-10 <= gap < -5 or 5 < gap <= 15) else 0

    change = close.diff()
    gain = change.clip(lower=0)
    loss = -change.clip(upper=0)
    rs = gain.rolling(14).mean() / loss.rolling(14).mean()
    rsi = float((100 - 100 / (1 + rs)).iloc[-1])
    if pd.isna(rsi):
        rsi = 50.0
    rsi_price_score = 2 if 40 <= rsi <= 55 else 1 if (30 <= rsi < 40 or 55 < rsi <= 65) else 0

    avg_volume = volume.tail(20).mean()
    volume_ratio = float(volume.iloc[-1]) / avg_volume if avg_volume > 0 else 0

    company_score = sum([current > ma20, ma20 > ma60, ma60 > ma120,
                         40 <= rsi <= 70, volume_ratio >= 1.5])

    return {
        "success": True, "current_price": current,
        "ma20": ma20, "ma60": ma60, "ma120": ma120,
        "high_52week": high52, "low_52week": low52,
        "price_position": position, "gap_from_ma20": gap,
        "rsi": rsi, "volume_ratio": volume_ratio,
        "technical_company_score": int(company_score),
        "technical_price_score": pos_score + gap_score + rsi_price_score,
    }


# ------------------------------------------------------------------
# 뉴스 / 공시 재료 점수
# ------------------------------------------------------------------
POSITIVE_NEWS = ["흑자전환", "사상 최대", "최대 실적", "대규모 수주", "수주", "공급계약",
                 "실적 개선", "매출 증가", "영업이익 증가", "신사업", "신제품", "승인",
                 "허가", "협력", "자사주 매입", "배당 확대"]
NEGATIVE_NEWS = ["횡령", "배임", "상장폐지", "거래정지", "부도", "파산", "적자전환",
                 "영업손실", "순손실", "실적 악화", "매출 감소", "유상증자", "과징금",
                 "영업정지", "화재", "사고"]


def get_news(keys: Keys, company_name: str) -> list:
    if not keys.has_naver:
        return []
    try:
        r = requests.get(
            "https://naverapihub.apigw.ntruss.com/search/v1/news",
            headers={"X-NCP-APIGW-API-KEY-ID": keys.naver_id,
                     "X-NCP-APIGW-API-KEY": keys.naver_secret},
            params={"query": company_name, "display": 20, "start": 1,
                    "sort": "date", "format": "json"},
            timeout=15,
        )
        if r.status_code != 200:
            return []
        return [{"title": clean_text(x.get("title")),
                 "description": clean_text(x.get("description"))}
                for x in r.json().get("items", [])[:10]]
    except Exception:
        return []


def score_news(news: list) -> dict:
    total = 0
    for a in news:
        t = (a["title"] + " " + a["description"]).lower()
        total += sum(1 for w in POSITIVE_NEWS if w.lower() in t)
        total -= sum(1 for w in NEGATIVE_NEWS if w.lower() in t)
    score = (5 if total >= 8 else 4 if total >= 4 else 3 if total > 0 else
             2 if total == 0 else 1 if total > -4 else 0)
    return {"raw_score": total, "material_score": score, "article_count": len(news)}


STRONG_POSITIVE_DISC = ["단일판매ㆍ공급계약체결", "단일판매·공급계약체결",
                        "자기주식취득결정", "주식소각결정"]
POSITIVE_DISC = ["무상증자결정", "현금ㆍ현물배당결정", "현금·현물배당결정"]
WARNING_DISC = ["유상증자결정", "전환사채권발행결정", "신주인수권부사채권발행결정",
                "감자결정", "최대주주변경"]
STRONG_NEGATIVE_DISC = ["상장폐지", "횡령", "배임", "파산", "회생절차개시신청",
                        "영업정지", "감사의견거절"]


def score_disclosures(disclosures: list) -> dict:
    raw = risk = 0
    for item in disclosures:
        name = item.get("report_nm", "")
        if any(w in name for w in STRONG_NEGATIVE_DISC):
            raw -= 2
            risk += 1
            continue
        if any(w in name for w in WARNING_DISC):
            raw -= 1
            continue
        if any(w in name for w in STRONG_POSITIVE_DISC):
            raw += 2
            continue
        if any(w in name for w in POSITIVE_DISC):
            raw += 1
    material = (5 if raw >= 4 else 4 if raw >= 2 else 3 if raw > 0 else
                2 if raw == 0 else 1 if raw > -3 else 0)
    return {"raw_score": raw, "material_score": material,
            "strong_risk_count": risk, "disclosure_count": len(disclosures)}


# ==================================================================
# 7. 종목 단위 분석
# ==================================================================
def analyze_company_now(keys: Keys, company: dict, year) -> dict:
    """현재 시점 분석. 실패 시 success=False 와 사유를 돌려줍니다."""
    finance = analyze_finance(keys.dart, company, year)
    if not finance.get("success"):
        return {"success": False, "reason": finance.get("reason"), "company": company}

    end = datetime.now() + timedelta(days=1)
    price = get_price_history(company["stock_code"], end - timedelta(days=500), end)
    if not price["success"]:
        return {"success": False, "reason": "주가 조회 실패", "company": company}

    tech = calculate_technical(price["data"])
    if not tech.get("success"):
        return {"success": False, "reason": tech.get("reason"), "company": company}

    snapshot = get_market_snapshot(company["stock_code"]) or {}
    market_cap = snapshot.get("market_cap")
    per = snapshot.get("per")
    pbr = snapshot.get("pbr")
    if per is None and market_cap and finance["net_income"]:
        per = market_cap / finance["net_income"] if finance["net_income"] > 0 else None
    if pbr is None and market_cap and finance["equity"]:
        pbr = market_cap / finance["equity"] if finance["equity"] > 0 else None

    valuation = score_valuation(per, pbr)
    news_items = get_news(keys, company["corp_name"])
    news = score_news(news_items)

    company_score = (finance["finance_score"] + valuation["company_valuation_score"]
                     + tech["technical_company_score"] + news["material_score"])
    price_score = tech["technical_price_score"] + valuation["price_value_score"]

    return {
        "success": True, "company": company, "finance": finance,
        "technical": tech, "valuation": valuation, "news": news,
        "news_items": news_items, "market_cap": market_cap,
        "price_source": price["source"], "snapshot_source": snapshot.get("source"),
        "company_score": company_score, "price_score": price_score,
        "strong_risk_count": 0,
        "dino_passed": (company_score >= MIN_COMPANY_SCORE
                        and price_score >= MIN_PRICE_SCORE),
    }


def analyze_company_asof(keys: Keys, company: dict, base_date: datetime) -> dict:
    """과거 기준일 분석 + 이후 1/3/6개월 수익률."""
    query_start = base_date - timedelta(days=550)
    query_end = min(base_date + timedelta(days=220), datetime.now() + timedelta(days=1))

    price = get_price_history(company["stock_code"], query_start, query_end)
    if not price["success"]:
        return {"success": False, "reason": price.get("reason") or "주가 조회 실패",
                "company": company}

    data = price["data"]
    base_row = get_row_on_or_after(data, base_date)
    if base_row is None:
        return {"success": False, "reason": "기준일 주가 없음", "company": company}

    actual_date = pd.Timestamp(base_row["date"])
    if (actual_date.date() - base_date.date()).days > 7:
        return {"success": False, "reason": "기준일 부근 거래자료 없음", "company": company}
    base_price = base_row["price"]

    tech = calculate_technical(data, as_of=actual_date)
    if not tech.get("success"):
        return {"success": False, "reason": tech.get("reason"), "company": company}

    annual = find_latest_annual_report_asof(keys.dart, company, base_date)
    if annual is None:
        return {"success": False, "reason": "기준일 이전 사업보고서 없음", "company": company}
    finance = analyze_finance(keys.dart, company, annual["business_year"])
    if not finance.get("success"):
        return {"success": False, "reason": finance.get("reason"), "company": company}

    periodic = find_latest_periodic_report_asof(keys.dart, company, base_date)
    if periodic is None:
        return {"success": False, "reason": "정기보고서 없음", "company": company}
    shares = get_issued_shares(keys.dart, company,
                               periodic["business_year"], periodic["report_code"])
    if shares is None:
        return {"success": False, "reason": "발행주식수 조회 실패", "company": company}

    market_cap = base_price * shares
    per = (market_cap / finance["net_income"]
           if finance["net_income"] and finance["net_income"] > 0 else None)
    pbr = (market_cap / finance["equity"]
           if finance["equity"] and finance["equity"] > 0 else None)
    valuation = score_valuation(per, pbr)

    disc_raw = dart_list(keys.dart, company, base_date - timedelta(days=90), base_date)
    disclosures = disc_raw.get("list", []) if disc_raw.get("status") == "000" else []
    disclosure = score_disclosures(disclosures)

    company_score = (finance["finance_score"] + valuation["company_valuation_score"]
                     + tech["technical_company_score"] + disclosure["material_score"])
    price_score = tech["technical_price_score"] + valuation["price_value_score"]

    r1 = get_row_on_or_after(data, actual_date + timedelta(days=30))
    r3 = get_row_on_or_after(data, actual_date + timedelta(days=90))
    r6 = get_row_on_or_after(data, actual_date + timedelta(days=180))

    return {
        "success": True, "company": company, "base_date": base_date,
        "actual_date": actual_date, "base_price": base_price,
        "price_source": price["source"], "finance": finance,
        "technical": tech, "valuation": valuation, "disclosure": disclosure,
        "market_cap": market_cap, "shares": shares,
        "finance_score": finance["finance_score"],
        "valuation_score": valuation["company_valuation_score"],
        "technical_score": tech["technical_company_score"],
        "material_score": disclosure["material_score"],
        "strong_risk_count": disclosure["strong_risk_count"],
        "company_score": company_score, "price_score": price_score,
        "return_1m": calculate_return(base_price, r1["price"]) if r1 else None,
        "return_3m": calculate_return(base_price, r3["price"]) if r3 else None,
        "return_6m": calculate_return(base_price, r6["price"]) if r6 else None,
    }


# ==================================================================
# 8. 모집단 / 표본
# ==================================================================
def build_historical_universe(index: MarketIndex, base_date: datetime) -> dict:
    eligible, future, unknown = [], [], []
    for company in index.krx_companies:
        listing = company.get("listing_date")
        if listing is None:
            unknown.append(company)
        elif listing.date() <= base_date.date():
            eligible.append(company)
        else:
            future.append(company)
    return {"eligible": eligible, "future_listing": future, "unknown_listing": unknown}


def select_spread_sample(companies: list, limit: int) -> list:
    if limit <= 0 or limit >= len(companies):
        return list(companies)
    if limit == 1:
        return [companies[len(companies) // 2]]
    max_index = len(companies) - 1
    return [companies[round(i * max_index / (limit - 1))] for i in range(limit)]


# ==================================================================
# 9. 스캔 / 백테스트 실행
# ==================================================================
def scan_market(keys: Keys, index: MarketIndex, year, limit: int = 100,
                progress: ProgressFn = _noop, stop_flag: Callable[[], bool] = lambda: False) -> dict:
    companies = select_spread_sample(index.krx_companies, limit)
    total = len(companies)
    results, failed = [], []

    for i, krx_company in enumerate(companies, 1):
        if stop_flag():
            break
        progress(i, total, krx_company["company_name"])
        company = index.resolve(krx_company)
        if company is None:
            failed.append({"회사명": krx_company["company_name"],
                           "종목코드": krx_company["stock_code"],
                           "사유": "DART 매칭 실패"})
            continue
        result = analyze_company_now(keys, company, year)
        if not result.get("success"):
            failed.append({"회사명": company["corp_name"],
                           "종목코드": company["stock_code"],
                           "사유": result.get("reason", "기타")})
            continue
        results.append(result)

    results.sort(key=lambda x: (x["company_score"] + x["price_score"],
                                x["company_score"], x["price_score"]), reverse=True)
    return {"results": results, "failed": failed, "scanned": total}


def run_historical_snapshot(keys: Keys, index: MarketIndex, base_date: datetime,
                            scan_limit: int = 30, progress: ProgressFn = _noop,
                            stop_flag: Callable[[], bool] = lambda: False) -> dict:
    universe = build_historical_universe(index, base_date)
    selected = select_spread_sample(universe["eligible"], scan_limit)
    total = len(selected)
    analyzed, failed = [], []

    for i, krx_company in enumerate(selected, 1):
        if stop_flag():
            break
        progress(i, total, krx_company["company_name"])
        company = index.resolve(krx_company)
        if company is None:
            failed.append({"회사명": krx_company["company_name"],
                           "종목코드": krx_company["stock_code"],
                           "사유": "DART 매칭 실패"})
            continue
        result = analyze_company_asof(keys, company, base_date)
        if not result.get("success"):
            failed.append({"회사명": company["corp_name"],
                           "종목코드": company["stock_code"],
                           "사유": result.get("reason", "기타")})
            continue
        analyzed.append(result)
        time.sleep(0.02)

    return {
        "base_date": base_date, "selected_count": total,
        "analyzed_results": analyzed, "failed_results": failed,
        "eligible_count": len(universe["eligible"]),
        "future_listing_count": len(universe["future_listing"]),
        "unknown_listing_count": len(universe["unknown_listing"]),
    }


# ==================================================================
# 10. 전략 통계
# ==================================================================
def get_strategy_candidates(results, company_min, price_min) -> list:
    return [r for r in results
            if r["company_score"] >= company_min
            and r["price_score"] >= price_min
            and r.get("strong_risk_count", 0) == 0]


def summarize_values(values: list) -> Optional[dict]:
    if not values:
        return None
    wins = sum(v > 0 for v in values)
    return {"count": len(values), "average": sum(values) / len(values),
            "median": float(pd.Series(values).median()),
            "win_rate": wins / len(values) * 100,
            "best": max(values), "worst": min(values)}


def calculate_period_stats(candidates, key) -> Optional[dict]:
    return summarize_values([r[key] for r in candidates if r.get(key) is not None])


def build_strategy_table(results: list) -> pd.DataFrame:
    """단일 기준일 결과를 전략별 표로."""
    rows = []
    for a, b in STRATEGIES:
        cands = get_strategy_candidates(results, a, b)
        s1 = calculate_period_stats(cands, "return_1m")
        s3 = calculate_period_stats(cands, "return_3m")
        s6 = calculate_period_stats(cands, "return_6m")
        rows.append({
            "전략": f"{a}/{b}", "후보수": len(cands),
            "1개월평균": s1["average"] if s1 else None,
            "3개월평균": s3["average"] if s3 else None,
            "3개월승률": s3["win_rate"] if s3 else None,
            "6개월평균": s6["average"] if s6 else None,
            "6개월승률": s6["win_rate"] if s6 else None,
        })
    return pd.DataFrame(rows)


def build_score_table(results: list) -> pd.DataFrame:
    rows = []
    for r in results:
        rows.append({
            "회사명": r["company"]["corp_name"],
            "종목코드": r["company"]["stock_code"],
            "기업점수": r["company_score"], "가격매력": r["price_score"],
            "재무": r.get("finance_score"), "밸류": r.get("valuation_score"),
            "기술": r.get("technical_score"), "재료": r.get("material_score"),
            "1개월수익률": r.get("return_1m"),
            "3개월수익률": r.get("return_3m"),
            "6개월수익률": r.get("return_6m"),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["기업점수", "가격매력"], ascending=False)
    return df


# ==================================================================
# 11. 다중시점 누적
# ==================================================================
def run_multi_date_backtest(keys: Keys, index: MarketIndex, dates: list,
                            scan_limit: int = 30,
                            progress: ProgressFn = _noop,
                            stop_flag: Callable[[], bool] = lambda: False) -> dict:
    summary = {(a, b): {"dates_tested": 0, "dates_with_candidates": 0,
                        "candidate_signals": 0, "r1": [], "r3": [], "r6": []}
               for a, b in STRATEGIES}
    event_rows, date_rows = [], []

    total_units = max(len(dates), 1)
    for idx, base_date in enumerate(dates, 1):
        if stop_flag():
            break

        def inner(done, total, msg, _idx=idx, _bd=base_date):
            overall = ((_idx - 1) + (done / total if total else 0)) / total_units
            progress(int(overall * 100), 100,
                     f"{_bd.strftime('%Y-%m-%d')} · {done}/{total} · {msg}")

        snap = run_historical_snapshot(keys, index, base_date, scan_limit,
                                       inner, stop_flag)
        results = snap["analyzed_results"]
        rate = (len(results) / snap["selected_count"] * 100
                if snap["selected_count"] else 0)
        date_rows.append({"기준일": base_date.strftime("%Y-%m-%d"),
                          "선정종목": snap["selected_count"],
                          "정상분석": len(results),
                          "분석실패": len(snap["failed_results"]),
                          "정상분석률": rate})

        for a, b in STRATEGIES:
            cands = get_strategy_candidates(results, a, b)
            s = summary[(a, b)]
            s["dates_tested"] += 1
            s["candidate_signals"] += len(cands)
            if cands:
                s["dates_with_candidates"] += 1
            for r in cands:
                for key, dest in (("return_1m", "r1"), ("return_3m", "r3"),
                                  ("return_6m", "r6")):
                    if r.get(key) is not None:
                        s[dest].append(r[key])
                event_rows.append({
                    "기준일": base_date.strftime("%Y-%m-%d"), "전략": f"{a}/{b}",
                    "회사명": r["company"]["corp_name"],
                    "종목코드": r["company"]["stock_code"],
                    "기업점수": r["company_score"], "가격매력": r["price_score"],
                    "1개월수익률": r["return_1m"], "3개월수익률": r["return_3m"],
                    "6개월수익률": r["return_6m"],
                })

    cumulative = []
    for a, b in STRATEGIES:
        s = summary[(a, b)]
        s1, s3, s6 = (summarize_values(s["r1"]), summarize_values(s["r3"]),
                      summarize_values(s["r6"]))
        cumulative.append({
            "전략": f"{a}/{b}",
            "검증기준일수": s["dates_tested"],
            "후보발생기준일수": s["dates_with_candidates"],
            "총후보신호수": s["candidate_signals"],
            "1개월표본수": s1["count"] if s1 else 0,
            "1개월평균": s1["average"] if s1 else None,
            "3개월표본수": s3["count"] if s3 else 0,
            "3개월평균": s3["average"] if s3 else None,
            "3개월중앙값": s3["median"] if s3 else None,
            "3개월승률": s3["win_rate"] if s3 else None,
            "3개월최고": s3["best"] if s3 else None,
            "3개월최저": s3["worst"] if s3 else None,
            "6개월표본수": s6["count"] if s6 else 0,
            "6개월평균": s6["average"] if s6 else None,
            "6개월중앙값": s6["median"] if s6 else None,
            "6개월승률": s6["win_rate"] if s6 else None,
            "6개월최고": s6["best"] if s6 else None,
            "6개월최저": s6["worst"] if s6 else None,
        })

    add_quality_scores(cumulative)
    return {"cumulative": cumulative, "events": event_rows, "dates": date_rows}


# ==================================================================
# 12. 전략 종합평가 (수익성 / 안정성 / 표본신뢰도)
# ==================================================================
def clamp(value, low=0.0, high=5.0):
    return max(low, min(high, value))


def star_text(score) -> str:
    filled = int(round(clamp(score)))
    return "★" * filled + "☆" * (5 - filled)


def _profitability(row) -> float:
    score = 0.0
    avg6, med6 = row.get("6개월평균"), row.get("6개월중앙값")
    avg3, med3 = row.get("3개월평균"), row.get("3개월중앙값")
    if avg6 is not None:
        score += 2.0 if avg6 >= 15 else 1.7 if avg6 >= 10 else 1.3 if avg6 >= 5 else 0.8 if avg6 > 0 else 0
    if med6 is not None:
        score += 1.0 if med6 >= 10 else 0.8 if med6 >= 5 else 0.5 if med6 > 0 else 0
    if avg3 is not None:
        score += 1.0 if avg3 >= 10 else 0.8 if avg3 >= 6 else 0.6 if avg3 >= 3 else 0.3 if avg3 > 0 else 0
    if med3 is not None:
        score += 1.0 if med3 >= 8 else 0.8 if med3 >= 4 else 0.5 if med3 > 0 else 0
    return clamp(score)


def _stability(row) -> float:
    score = 0.0
    win6, win3 = row.get("6개월승률"), row.get("3개월승률")
    worst6, worst3 = row.get("6개월최저"), row.get("3개월최저")
    if win6 is not None:
        score += 2.0 if win6 >= 70 else 1.7 if win6 >= 60 else 1.4 if win6 >= 55 else 1.0 if win6 >= 50 else 0.5 if win6 >= 45 else 0
    if win3 is not None:
        score += 1.0 if win3 >= 70 else 0.8 if win3 >= 60 else 0.6 if win3 >= 55 else 0.4 if win3 >= 50 else 0
    if worst6 is not None:
        score += (1.5 if worst6 >= -10 else 1.3 if worst6 >= -15 else 1.1 if worst6 >= -20
                  else 0.9 if worst6 >= -25 else 0.7 if worst6 >= -30
                  else 0.4 if worst6 >= -40 else 0.1)
    if worst3 is not None:
        score += (0.5 if worst3 >= -10 else 0.4 if worst3 >= -20 else 0.3 if worst3 >= -30
                  else 0.2 if worst3 >= -40 else 0.1)
    return clamp(score)


def _confidence(row) -> float:
    score = 0.0
    sample = row.get("6개월표본수", 0) or 0
    tested = row.get("검증기준일수", 0) or 0
    occurred = row.get("후보발생기준일수", 0) or 0
    score += (3.0 if sample >= 100 else 2.7 if sample >= 70 else 2.4 if sample >= 50
              else 2.0 if sample >= 30 else 1.6 if sample >= 20 else 1.3 if sample >= 15
              else 0.9 if sample >= 10 else 0.5 if sample > 0 else 0)
    ratio = occurred / tested if tested else 0
    score += (2.0 if ratio >= 0.95 else 1.7 if ratio >= 0.80 else 1.4 if ratio >= 0.60
              else 1.0 if ratio >= 0.40 else 0.5 if ratio > 0 else 0)
    return clamp(score)


def evaluate_strategy_quality(row) -> dict:
    p, s, c = _profitability(row), _stability(row), _confidence(row)
    overall = p * 0.40 + s * 0.35 + c * 0.25
    if overall >= 4.2:
        grade, comment = "A", "수익·안정성·표본이 모두 강한 편"
    elif overall >= 3.6:
        grade, comment = "B+", "종합적으로 유망하나 위험요인 확인 필요"
    elif overall >= 3.0:
        grade, comment = "B", "검증가치 있음 / 위험관리 보완 필요"
    elif overall >= 2.4:
        grade, comment = "C+", "성과는 있으나 안정성이 부족"
    else:
        grade, comment = "C", "현재 기준에서는 보수적으로 접근"
    worst6 = row.get("6개월최저")
    if worst6 is not None and worst6 <= -40:
        comment += " / -40% 이하 사례 존재"
    return {"수익성점수": p, "안정성점수": s, "표본신뢰도점수": c,
            "종합점수": overall, "등급": grade, "종합의견": comment}


def add_quality_scores(rows: list) -> list:
    for row in rows:
        row.update(evaluate_strategy_quality(row))
    return rows


def rank_strategies(rows: list, min_sample=15, min_dates=2) -> list:
    eligible = [r for r in rows
                if (r.get("6개월표본수", 0) >= min_sample
                    and r.get("후보발생기준일수", 0) >= min_dates
                    and r.get("6개월평균") is not None)]
    return sorted(eligible,
                  key=lambda r: (r.get("종합점수", 0), r.get("안정성점수", 0),
                                 r.get("수익성점수", 0), r.get("표본신뢰도점수", 0)),
                  reverse=True)


DISCLAIMER = (
    "이 도구는 공개된 과거 데이터에 정해진 규칙을 적용해 보여주는 학습·연구용 화면입니다. "
    "투자 자문이나 종목 추천이 아니며, 과거 성과가 미래 수익을 보장하지 않습니다. "
    "현재 상장사 목록을 과거로 되돌리는 방식이라 상장폐지 종목이 빠지는 생존편향이 남아 있습니다."
)
