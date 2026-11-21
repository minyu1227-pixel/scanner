"""
scanner.py
──────────────────────────────────────────────────────────────
MACD(5, 20, 5) 골든크로스 + 이격도(15 SMA) 과매도 이력 스캐너
(TradingView Pine Script "MACD 골든크로스 + 이격도 과매도 이력" 로직 이식)

대상 종목 : NYSE + NASDAQ + AMEX 상장 보통주(Common Stock)
1차 조건  : 최근 종가 >= 30 달러
2차 조건  : 최근 20거래일 평균 거래대금(종가 × 거래량) >= 300억 원
시그널    : ① 최근 봉에서 MACD 골든크로스
            ② 최근 봉을 제외한 직전 10개 봉 중 이격도 <= 85 인 봉 존재

필요 패키지 : pip install yfinance pandas numpy requests
사용 예     : python scanner.py
              python scanner.py --limit 300          (테스트용: 앞 300종목만)
              python scanner.py --include-today      (장중 미완성 봉 포함)
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import time

import numpy as np
import pandas as pd
import requests
import yfinance as yf

# ───── 전제 조건 (Pine Script 입력값과 동일) ─────
MACD_FAST = 5
MACD_SLOW = 20
MACD_SIGNAL = 5
DISP_LEN = 15          # 이격도 SMA 기간
DISP_TH = 85.0         # 이격도 과매도 기준 (이하)
LOOKBACK = 10          # 과매도 확인 구간 (오늘 제외 봉 수)

# ───── 종목 필터 조건 ─────
MIN_PRICE_USD = 30.0                 # 1차: 주가 30달러 이상
MIN_TURNOVER_KRW = 30_000_000_000    # 2차: 평균 거래대금 300억 원 이상
TURNOVER_DAYS = 20                   # 거래대금 평균 기간
FALLBACK_USDKRW = 1400.0             # 환율 조회 실패 시 사용할 값

# ───── 종목 리스트 (NASDAQ Trader 공식 심볼 디렉터리) ─────
NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
OTHER_EXCHANGES = {"N": "NYSE", "A": "AMEX"}   # A = NYSE American(구 AMEX)

# 보통주가 아닌 증권을 걸러내기 위한 키워드
EXCLUDE_RE = re.compile(
    r"\b(preferred|warrants?|rights?|units?|depositary|notes?|debentures?|"
    r"subordinated|trust preferred)\b",
    re.IGNORECASE,
)


# ════════════════════════════════════════════════════════════
# 1. 종목 유니버스
# ════════════════════════════════════════════════════════════
def _read_symdir(url: str) -> pd.DataFrame:
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text), sep="|", dtype=str)
    # 마지막 줄 "File Creation Time: ..." 제거
    return df[~df.iloc[:, 0].str.startswith("File Creation Time", na=False)]


def _is_common_stock(name: str) -> bool:
    return "common stock" in name.lower() and not EXCLUDE_RE.search(name)


def get_universe() -> pd.DataFrame:
    nq = _read_symdir(NASDAQ_URL)
    nq = nq[(nq["Test Issue"] == "N") & (nq["ETF"] == "N")]
    nq = nq.assign(Exchange="NASDAQ")[["Symbol", "Security Name", "Exchange"]]

    ot = _read_symdir(OTHER_URL)
    ot = ot[(ot["Test Issue"] == "N") & (ot["ETF"] == "N")
            & ot["Exchange"].isin(OTHER_EXCHANGES.keys())]
    ot = ot.rename(columns={"ACT Symbol": "Symbol"})
    ot["Exchange"] = ot["Exchange"].map(OTHER_EXCHANGES)
    ot = ot[["Symbol", "Security Name", "Exchange"]]

    df = pd.concat([nq, ot], ignore_index=True)
    df = df[df["Security Name"].fillna("").map(_is_common_stock)]

    # yfinance 표기로 변환 (BRK.B → BRK-B)
    df["Ticker"] = df["Symbol"].str.strip().str.replace(r"[./]", "-", regex=True)
    df = df[~df["Ticker"].str.contains(r"[\$=\^\s]", regex=True)]
    return df.drop_duplicates("Ticker").reset_index(drop=True)


# ════════════════════════════════════════════════════════════
# 2. 지표 계산 (Pine Script와 동일한 방식)
# ════════════════════════════════════════════════════════════
def pine_ema(s: pd.Series, length: int) -> pd.Series:
    """Pine ta.ema: 첫 값은 SMA(length)로 시드, 이후 alpha = 2/(length+1)."""
    v = s.to_numpy(dtype=float)
    out = np.full(len(v), np.nan)
    valid = np.flatnonzero(~np.isnan(v))
    if len(valid) < length:
        return pd.Series(out, index=s.index)
    start = valid[0]
    seed = start + length - 1
    out[seed] = v[start:seed + 1].mean()
    alpha = 2.0 / (length + 1)
    for i in range(seed + 1, len(v)):
        x = v[i]
        out[i] = out[i - 1] if np.isnan(x) else alpha * x + (1 - alpha) * out[i - 1]
    return pd.Series(out, index=s.index)


def evaluate(df: pd.DataFrame, min_turnover_usd: float) -> dict | None:
    df = df.dropna(subset=["Close", "Volume"])
    df = df[df["Volume"] > 0]
    need = max(MACD_SLOW + MACD_SIGNAL + 2, DISP_LEN + LOOKBACK + 1, TURNOVER_DAYS)
    if len(df) < need:
        return None

    close = df["Close"].astype(float)
    volume = df["Volume"].astype(float)

    last_close = close.iloc[-1]
    avg_turnover = (close * volume).iloc[-TURNOVER_DAYS:].mean()

    # MACD(5, 20, 5)
    macd = pine_ema(close, MACD_FAST) - pine_ema(close, MACD_SLOW)
    signal = pine_ema(macd, MACD_SIGNAL)

    # 이격도 = 종가 / SMA(15) × 100
    disparity = close / close.rolling(DISP_LEN).mean() * 100

    # ① ta.crossover(macd, signal): 오늘 macd > signal, 어제 macd <= signal
    golden_cross = bool(macd.iloc[-1] > signal.iloc[-1] and macd.iloc[-2] <= signal.iloc[-2])

    # ② ta.lowest(disparity, 10)[1] <= 85 : 오늘 제외 직전 10개 봉의 최저 이격도
    min_disp = disparity.iloc[-LOOKBACK - 1:-1].min()
    was_oversold = bool(min_disp <= DISP_TH)

    return {
        "Date": df.index[-1].date(),
        "Close": round(last_close, 2),
        "AvgTurnoverUSD": avg_turnover,
        "Disparity": round(disparity.iloc[-1], 2),
        "MinDisp10": round(min_disp, 2),
        "MACD": round(macd.iloc[-1], 4),
        "Signal": round(signal.iloc[-1], 4),
        "Pass1_Price": bool(last_close >= MIN_PRICE_USD),
        "Pass2_Turnover": bool(avg_turnover >= min_turnover_usd),
        "GoldenCross": golden_cross,
        "WasOversold": was_oversold,
    }


# ════════════════════════════════════════════════════════════
# 3. 데이터 다운로드
# ════════════════════════════════════════════════════════════
def get_usdkrw() -> float:
    try:
        h = yf.Ticker("KRW=X").history(period="5d")
        rate = float(h["Close"].dropna().iloc[-1])
        if 500 < rate < 3000:
            return rate
    except Exception as e:
        print(f"[경고] 환율 조회 실패: {e}")
    print(f"[경고] 기본 환율 {FALLBACK_USDKRW} 사용")
    return FALLBACK_USDKRW


def download_batch(tickers: list[str], period: str) -> dict[str, pd.DataFrame]:
    """여러 종목을 한 번에 받아 {ticker: OHLCV DataFrame} 형태로 반환."""
    for attempt in range(3):
        try:
            data = yf.download(
                tickers,
                period=period,
                interval="1d",
                auto_adjust=False,   # TradingView 기본 차트와 동일(분할만 반영, 배당 미반영)
                actions=False,
                group_by="ticker",
                threads=True,
                progress=False,
            )
            break
        except Exception as e:
            print(f"  다운로드 오류(재시도 {attempt + 1}/3): {e}")
            time.sleep(5 * (attempt + 1))
    else:
        return {}

    result = {}
    if data is None or data.empty:
        return result
    if isinstance(data.columns, pd.MultiIndex):
        level0 = set(data.columns.get_level_values(0))
        for t in tickers:
            if t in level0:
                result[t] = data[t]
    elif len(tickers) == 1:
        result[tickers[0]] = data
    return result


def drop_incomplete_bar(df: pd.DataFrame) -> pd.DataFrame:
    """미국 장 마감(16:00 ET) 전이면 오늘 진행 중인 봉을 제외 (Pine barstate.isconfirmed 대응)."""
    if df.empty:
        return df
    now = pd.Timestamp.now(tz="America/New_York")
    if df.index[-1].date() == now.date() and (now.hour, now.minute) < (16, 0):
        return df.iloc[:-1]
    return df


# ════════════════════════════════════════════════════════════
# 4. 메인
# ════════════════════════════════════════════════════════════
def main() -> None:
    ap = argparse.ArgumentParser(description="MACD 골든크로스 + 이격도 과매도 스캐너")
    ap.add_argument("--limit", type=int, default=0, help="앞에서부터 N종목만 스캔 (테스트용)")
    ap.add_argument("--batch-size", type=int, default=100, help="yfinance 한 번에 받을 종목 수")
    ap.add_argument("--period", default="1y", help="다운로드 기간 (EMA 안정화를 위해 1y 권장)")
    ap.add_argument("--include-today", action="store_true", help="장중 미완성 봉도 포함")
    ap.add_argument("--out", default="", help="결과 CSV 경로 (기본: scan_YYYYMMDD.csv)")
    args = ap.parse_args()

    print("▶ 종목 리스트 수집 중...")
    universe = get_universe()
    if args.limit:
        universe = universe.head(args.limit)
    print(f"  보통주 {len(universe):,}개 (NYSE/NASDAQ/AMEX)")

    usdkrw = get_usdkrw()
    min_turnover_usd = MIN_TURNOVER_KRW / usdkrw
    print(f"▶ 환율 USD/KRW = {usdkrw:,.2f} → 300억 원 ≈ ${min_turnover_usd / 1e6:,.1f}M")

    info = universe.set_index("Ticker")
    tickers = universe["Ticker"].tolist()
    rows = []
    n_batches = (len(tickers) + args.batch_size - 1) // args.batch_size

    for b in range(n_batches):
        batch = tickers[b * args.batch_size:(b + 1) * args.batch_size]
        print(f"▶ 다운로드 {b + 1}/{n_batches} ({len(batch)}종목)", end="\r", flush=True)
        frames = download_batch(batch, args.period)
        for t, df in frames.items():
            if not args.include_today:
                df = drop_incomplete_bar(df)
            try:
                res = evaluate(df, min_turnover_usd)
            except Exception:
                res = None
            if res:
                res["Ticker"] = t
                res["Exchange"] = info.at[t, "Exchange"]
                res["Name"] = info.at[t, "Security Name"]
                rows.append(res)
        time.sleep(1)  # 과도한 요청 방지
    print()

    if not rows:
        print("데이터를 받지 못했습니다.")
        sys.exit(1)

    res = pd.DataFrame(rows)
    res["AvgTurnoverKRW_억"] = (res["AvgTurnoverUSD"] * usdkrw / 1e8).round(0)
    res["AvgTurnoverUSD"] = res["AvgTurnoverUSD"].round(0)

    passed = res[res["Pass1_Price"] & res["Pass2_Turnover"]]
    hits = passed[passed["GoldenCross"] & passed["WasOversold"]]
    hits = hits.sort_values("AvgTurnoverUSD", ascending=False)

    print(f"  데이터 수신 {len(res):,}개 → 1·2차 조건 통과 {len(passed):,}개 → 시그널 {len(hits):,}개")

    cols = ["Ticker", "Exchange", "Date", "Close", "Disparity", "MinDisp10",
            "MACD", "Signal", "AvgTurnoverKRW_억", "Name"]
    if hits.empty:
        print("\n오늘 조건을 만족하는 종목이 없습니다.")
    else:
        with pd.option_context("display.max_rows", None, "display.width", 200,
                               "display.max_colwidth", 40):
            print("\n" + hits[cols].to_string(index=False))

    out = args.out or f"scan_{pd.Timestamp.now():%Y%m%d}.csv"
    hits[cols].to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n결과 저장: {out}")


if __name__ == "__main__":
    main()
