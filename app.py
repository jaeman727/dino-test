"""
디노테스트 웹 화면 (Streamlit).

실행:  streamlit run app.py

이 파일은 '보여주기'만 담당합니다. 계산은 전부 dino_core.py 에 있습니다.
"""

from datetime import datetime

import pandas as pd
import streamlit as st

import dino_core as core

st.set_page_config(page_title="디노테스트", page_icon="📊", layout="wide")


# ------------------------------------------------------------------
# 키 읽기 — 코드에 절대 적지 않습니다.
# .streamlit/secrets.toml 또는 환경변수에서만 가져옵니다.
# ------------------------------------------------------------------
def load_keys() -> core.Keys:
    keys = core.keys_from_env()
    try:
        secrets = st.secrets
    except Exception:
        secrets = {}
    if not keys.dart:
        keys.dart = secrets.get("DART_API_KEY", "")
    if not keys.naver_id:
        keys.naver_id = secrets.get("NAVER_CLIENT_ID", "")
    if not keys.naver_secret:
        keys.naver_secret = secrets.get("NAVER_CLIENT_SECRET", "")
    return keys


@st.cache_resource(show_spinner="상장사 목록과 DART 회사코드를 준비하는 중입니다…")
def get_index(dart_key: str) -> core.MarketIndex:
    return core.load_market_index(dart_key)


def csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


def download(df: pd.DataFrame, label: str, name: str):
    if df is None or df.empty:
        return
    st.download_button(label, csv_bytes(df),
                       file_name=f"{name}_{datetime.now():%Y%m%d_%H%M%S}.csv",
                       mime="text/csv")


def pct(v):
    return "—" if v is None or pd.isna(v) else f"{v:+.2f}%"


def make_progress():
    """진행률 막대 + 현재 처리 중인 종목명."""
    bar = st.progress(0.0)
    caption = st.empty()

    def fn(done, total, message=""):
        ratio = done / total if total else 0
        bar.progress(min(max(ratio, 0.0), 1.0))
        caption.caption(f"{done} / {total}  ·  {message}")

    return fn, bar, caption


# ==================================================================
# 사이드바
# ==================================================================
keys = load_keys()

with st.sidebar:
    st.subheader("연결 상태")
    st.write("DART 키", "연결됨" if keys.has_dart else "없음")
    st.write("네이버 뉴스 키", "연결됨" if keys.has_naver else "없음 (재료점수는 중립 2점 처리)")
    st.write("pykrx", "사용 가능" if core.HAS_PYKRX else "설치 안 됨")
    st.write("yfinance", "사용 가능" if core.HAS_YFINANCE else "설치 안 됨")

    if not keys.has_dart:
        st.error(
            "DART 키가 없습니다. `.streamlit/secrets.toml` 에 "
            "`DART_API_KEY` 를 넣거나 환경변수로 설정한 뒤 새로고침하세요."
        )
        st.stop()

    st.divider()
    st.subheader("주의")
    st.caption(core.DISCLAIMER)

index = get_index(keys.dart)

with st.sidebar:
    st.divider()
    st.caption(f"상장사 {len(index.krx_companies):,}개 · 출처 {index.krx_source}")
    st.caption(f"불러온 시각 {index.loaded_at:%Y-%m-%d %H:%M}")
    if st.button("기준 데이터 새로 받기"):
        get_index.clear()
        st.rerun()


st.title("디노테스트")
st.caption("공개 데이터로 기업점수(20점)와 가격매력(10점)을 계산하고, 과거 시점에 같은 규칙을 적용해 결과를 확인합니다.")

tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "종목 하나 보기", "시장 훑어보기", "종목 하나 백테스트",
    "한 시점 백테스트", "여러 시점 백테스트",
])


# ==================================================================
# 1. 개별 종목 분석
# ==================================================================
with tab1:
    col1, col2 = st.columns([3, 1])
    name = col1.text_input("회사명", placeholder="예: 삼성전자")
    year = col2.number_input("재무제표 기준연도", 2015, datetime.now().year,
                             datetime.now().year - 1, key="y1")

    if name:
        hits = index.search_names(name, limit=8)
        if hits:
            st.caption("검색된 회사: " + ", ".join(
                f"{h['company_name']}({h['stock_code']})" for h in hits))

    if st.button("분석하기", type="primary", key="b1") and name:
        company = index.find_company(name)
        if company is None:
            st.error(f"'{name}' 을 상장사 목록에서 찾지 못했습니다. 정확한 회사명을 입력해 보세요.")
        else:
            with st.spinner(f"{company['corp_name']} 분석 중…"):
                r = core.analyze_company_now(keys, company, int(year))
            if not r.get("success"):
                st.error(f"분석하지 못했습니다 — {r.get('reason')}")
            else:
                st.subheader(f"{r['company']['corp_name']} ({r['company']['stock_code']})")

                c = st.columns(4)
                c[0].metric("기업점수", f"{r['company_score']} / 20")
                c[1].metric("가격매력", f"{r['price_score']} / 10")
                c[2].metric("현재가", f"{r['technical']['current_price']:,.0f}원")
                c[3].metric("디노 기준", "충족" if r["dino_passed"] else "미충족")

                f, v, t = r["finance"], r["valuation"], r["technical"]
                c = st.columns(4)
                c[0].metric("ROE", "—" if f["roe"] is None else f"{f['roe']:.2f}%")
                c[1].metric("부채비율", "—" if f["debt_ratio"] is None else f"{f['debt_ratio']:.1f}%")
                c[2].metric("PER", "—" if v["per"] is None else f"{v['per']:.2f}배")
                c[3].metric("PBR", "—" if v["pbr"] is None else f"{v['pbr']:.2f}배")

                c = st.columns(4)
                c[0].metric("RSI", f"{t['rsi']:.1f}")
                c[1].metric("52주 위치", f"{t['price_position']:.0f}%")
                c[2].metric("20일선 이격", f"{t['gap_from_ma20']:+.1f}%")
                c[3].metric("거래량 배수", f"{t['volume_ratio']:.2f}배")

                st.write("**점수 구성**")
                st.dataframe(pd.DataFrame([{
                    "재무(5)": f["finance_score"],
                    "밸류(5)": v["company_valuation_score"],
                    "기술(5)": t["technical_company_score"],
                    "재료(5)": r["news"]["material_score"],
                    "합계": r["company_score"],
                }]), hide_index=True, use_container_width=True)

                st.caption(f"주가 출처 {r['price_source']} · 시총·지표 출처 {r.get('snapshot_source') or '재무 기반 계산'} · 재무 {f['business_year']}년 {f.get('source', '')}")

                if r["news_items"]:
                    with st.expander(f"최근 뉴스 {len(r['news_items'])}건"):
                        for n in r["news_items"]:
                            st.write(f"- {n['title']}")
                elif not keys.has_naver:
                    st.info("네이버 키가 없어 뉴스를 조회하지 못했습니다. 재료점수는 중립인 2점으로 들어갑니다.")


# ==================================================================
# 2. 전체시장 스캔
# ==================================================================
with tab2:
    st.write("상장사 목록에서 고르게 뽑은 종목을 순서대로 분석하고, 점수 합계가 높은 순으로 보여줍니다.")
    c = st.columns(3)
    year2 = c[0].number_input("재무제표 기준연도", 2015, datetime.now().year,
                              datetime.now().year - 1, key="y2")
    limit2 = c[1].number_input("스캔할 종목 수", 5, 500, 30, step=5, key="l2")
    top_n = c[2].number_input("상위 몇 개를 볼지", 3, 50, 5, key="t2")

    st.caption(f"종목당 10~20초쯤 걸립니다. {int(limit2)}종목이면 대략 {int(limit2)*15//60}분 안팎입니다.")

    if st.button("스캔 시작", type="primary", key="b2"):
        fn, bar, cap = make_progress()
        out = core.scan_market(keys, index, int(year2), int(limit2), fn)
        bar.empty()
        cap.empty()

        results, failed = out["results"], out["failed"]
        c = st.columns(3)
        c[0].metric("스캔", f"{out['scanned']}종목")
        c[1].metric("정상 분석", f"{len(results)}종목")
        c[2].metric("디노 기준 충족", f"{sum(1 for r in results if r['dino_passed'])}종목")

        if not results:
            st.warning("정상적으로 분석된 종목이 없습니다. 주가 데이터 출처나 DART 쿼터를 확인해 보세요.")
        else:
            rows = [{
                "순위": i,
                "회사명": r["company"]["corp_name"],
                "종목코드": r["company"]["stock_code"],
                "기업점수": r["company_score"],
                "가격매력": r["price_score"],
                "합계": r["company_score"] + r["price_score"],
                "PER": r["valuation"]["per"],
                "PBR": r["valuation"]["pbr"],
                "ROE(%)": r["finance"]["roe"],
                "디노기준": "충족" if r["dino_passed"] else "미충족",
            } for i, r in enumerate(results, 1)]
            df = pd.DataFrame(rows)
            st.dataframe(df.head(int(top_n)), hide_index=True, use_container_width=True)
            with st.expander(f"전체 {len(df)}종목"):
                st.dataframe(df, hide_index=True, use_container_width=True)
            download(df, "결과 CSV 내려받기", "dino_scan")

        if failed:
            with st.expander(f"분석하지 못한 종목 {len(failed)}개"):
                st.dataframe(pd.DataFrame(failed), hide_index=True,
                             use_container_width=True)


# ==================================================================
# 3. 개별 종목 백테스트
# ==================================================================
with tab3:
    c = st.columns([3, 2])
    name3 = c[0].text_input("회사명", placeholder="예: 한화솔루션", key="n3")
    base3 = c[1].date_input("과거 기준일", value=datetime(2025, 8, 1).date(), key="d3")

    if st.button("백테스트 실행", type="primary", key="b3") and name3:
        company = index.find_company(name3)
        if company is None:
            st.error(f"'{name3}' 을 찾지 못했습니다.")
        elif pd.Timestamp(base3) >= pd.Timestamp(datetime.now().date()):
            st.error("과거 날짜를 골라 주세요.")
        else:
            listing = company.get("listing_date")
            base_dt = datetime.combine(base3, datetime.min.time())
            if listing is not None and listing.date() > base3:
                st.warning(f"기준일 당시 미상장입니다. 상장일 {listing:%Y-%m-%d}")
            else:
                with st.spinner("과거 시점 재현 중…"):
                    r = core.analyze_company_asof(keys, company, base_dt)
                if not r.get("success"):
                    st.error(f"백테스트하지 못했습니다 — {r.get('reason')}")
                else:
                    st.subheader(f"{company['corp_name']} · {base3:%Y-%m-%d} 기준")
                    c = st.columns(5)
                    c[0].metric("기업점수", f"{r['company_score']} / 20")
                    c[1].metric("가격매력", f"{r['price_score']} / 10")
                    c[2].metric("1개월", pct(r["return_1m"]))
                    c[3].metric("3개월", pct(r["return_3m"]))
                    c[4].metric("6개월", pct(r["return_6m"]))

                    st.caption(
                        f"기준가 {r['base_price']:,.0f}원 · "
                        f"실제 거래일 {pd.Timestamp(r['actual_date']):%Y-%m-%d} · "
                        f"주가 출처 {r['price_source']} · 재무 {r['finance']['business_year']}년"
                    )

                    st.write("**전략별 충족 여부**")
                    st.dataframe(pd.DataFrame([{
                        "전략": f"{a}/{b}",
                        "충족": "○" if (r["company_score"] >= a
                                       and r["price_score"] >= b
                                       and r["strong_risk_count"] == 0) else "×",
                    } for a, b in core.STRATEGIES]).T,
                        use_container_width=True)


# ==================================================================
# 4. 한 시점 백테스트
# ==================================================================
with tab4:
    c = st.columns([2, 2])
    base4 = c[0].date_input("기준일", value=datetime(2025, 8, 1).date(), key="d4")
    limit4 = c[1].number_input("분석할 종목 수", 5, 300, 30, step=5, key="l4")
    st.caption(f"종목당 DART를 4~5회 호출합니다. {int(limit4)}종목이면 약 {int(limit4)*5}회입니다.")

    if st.button("백테스트 시작", type="primary", key="b4"):
        if pd.Timestamp(base4) >= pd.Timestamp(datetime.now().date()):
            st.error("과거 날짜를 골라 주세요.")
        else:
            base_dt = datetime.combine(base4, datetime.min.time())
            fn, bar, cap = make_progress()
            snap = core.run_historical_snapshot(keys, index, base_dt, int(limit4), fn)
            bar.empty()
            cap.empty()

            results = snap["analyzed_results"]
            c = st.columns(4)
            c[0].metric("기준일 당시 상장", f"{snap['eligible_count']:,}종목")
            c[1].metric("분석 대상", f"{snap['selected_count']}종목")
            c[2].metric("정상 분석", f"{len(results)}종목")
            c[3].metric("상장일 미확인", f"{snap['unknown_listing_count']:,}종목")

            if not results:
                st.warning("정상 분석된 종목이 없습니다.")
            else:
                st.write("**전략별 결과**")
                strat = core.build_strategy_table(results)
                st.dataframe(strat.style.format({
                    "1개월평균": "{:+.2f}%", "3개월평균": "{:+.2f}%",
                    "6개월평균": "{:+.2f}%", "3개월승률": "{:.1f}%",
                    "6개월승률": "{:.1f}%"}, na_rep="—"),
                    hide_index=True, use_container_width=True)

                st.write("**종목별 점수와 수익률**")
                scores = core.build_score_table(results)
                st.dataframe(scores, hide_index=True, use_container_width=True)
                download(scores, "종목별 CSV 내려받기", f"dino_backtest_{base4:%Y%m%d}")

            if snap["failed_results"]:
                with st.expander(f"분석하지 못한 종목 {len(snap['failed_results'])}개"):
                    st.dataframe(pd.DataFrame(snap["failed_results"]),
                                 hide_index=True, use_container_width=True)


# ==================================================================
# 5. 다중시점 백테스트
# ==================================================================
with tab5:
    st.write("여러 기준일에서 같은 규칙을 반복 적용하고, 결과를 합쳐 전략을 비교합니다.")

    mode = st.radio("기준일", ["추천 날짜 사용", "직접 입력"], horizontal=True)
    if mode == "추천 날짜 사용":
        picked = st.multiselect("사용할 날짜", core.DEFAULT_MULTI_DATES,
                                default=core.DEFAULT_MULTI_DATES)
    else:
        text = st.text_input("쉼표로 구분해 입력",
                             value=",".join(core.DEFAULT_MULTI_DATES[:3]))
        picked = [x.strip() for x in text.split(",") if x.strip()]

    limit5 = st.number_input("날짜당 분석할 종목 수", 5, 300, 30, step=5, key="l5")

    est = len(picked) * int(limit5)
    st.caption(f"총 {est}건을 분석합니다. 대략 {est * 3 // 60}분 안팎이 걸리고, DART는 약 {est * 5}회 호출됩니다.")
    if est > 600:
        st.warning("분석 건수가 많습니다. 브라우저를 닫으면 작업이 중단되니, 처음에는 날짜 2~3개로 시험해 보세요.")

    if st.button("누적 백테스트 시작", type="primary", key="b5"):
        dates, bad = [], []
        for t in picked:
            try:
                d = datetime.strptime(t, "%Y-%m-%d")
                if d >= datetime.now():
                    bad.append(f"{t} (미래)")
                else:
                    dates.append(d)
            except ValueError:
                bad.append(f"{t} (형식 오류)")

        if bad:
            st.error("사용할 수 없는 날짜: " + ", ".join(bad))
        elif not dates:
            st.error("날짜를 하나 이상 골라 주세요.")
        else:
            fn, bar, cap = make_progress()
            out = core.run_multi_date_backtest(keys, index, sorted(dates),
                                               int(limit5), fn)
            bar.empty()
            cap.empty()

            cum = pd.DataFrame(out["cumulative"])
            st.write("**날짜별 분석 현황**")
            st.dataframe(pd.DataFrame(out["dates"]), hide_index=True,
                         use_container_width=True)

            st.write("**전략 종합평가**")
            st.caption("수익성 40% + 안정성 35% + 표본신뢰도 25%. 평균수익률 하나만 높은 전략이 1위가 되지 않도록 만든 내부 기준입니다.")

            ranked = core.rank_strategies(out["cumulative"], min_sample=15, min_dates=2)
            if not ranked:
                st.info("6개월 표본 15건 이상 · 후보 발생 날짜 2개 이상을 만족한 전략이 아직 없습니다. 종목 수나 날짜를 늘려 보세요.")
            else:
                for i, row in enumerate(ranked[:5], 1):
                    with st.container(border=True):
                        c = st.columns([2, 1, 1, 1])
                        c[0].markdown(f"### {i}. {row['전략']}")
                        c[1].metric("종합", f"{row['종합점수']:.2f} / 5")
                        c[2].metric("등급", row["등급"])
                        c[3].metric("6개월 표본", f"{row['6개월표본수']}건")
                        st.write(
                            f"수익성 {core.star_text(row['수익성점수'])} {row['수익성점수']:.2f}　"
                            f"안정성 {core.star_text(row['안정성점수'])} {row['안정성점수']:.2f}　"
                            f"표본신뢰도 {core.star_text(row['표본신뢰도점수'])} {row['표본신뢰도점수']:.2f}"
                        )
                        if row["6개월평균"] is not None:
                            st.write(
                                f"6개월 · 평균 {row['6개월평균']:+.2f}% / "
                                f"중앙값 {row['6개월중앙값']:+.2f}% / "
                                f"승률 {row['6개월승률']:.1f}% / "
                                f"최저 {row['6개월최저']:+.2f}%"
                            )
                        st.caption(row["종합의견"])

            with st.expander("전략별 누적 수치 전체"):
                st.dataframe(cum, hide_index=True, use_container_width=True)

            download(cum, "전략 요약 CSV", "dino_multi_strategy")
            if out["events"]:
                download(pd.DataFrame(out["events"]), "후보 종목 CSV", "dino_multi_events")

st.divider()
st.caption(core.DISCLAIMER)
