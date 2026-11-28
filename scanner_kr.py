"""
scanner_kr.py  (국내주식 — 미국용 scanner.py와 완전히 별개)
──────────────────────────────────────────────────────────────
MACD(5, 20, 5) 골든크로스 + 이격도(15 SMA) 과매도 이력 스캐너 — 코스피 + 코스닥
(지표·시그널 로직은 scanner.py와 동일)

대상 종목 : KRX KIND 상장법인 목록(코스피 + 코스닥, 스팩 제외)
            다운로드에 실패하면 같은 폴더의 kr_tickers.json(백업 목록) 사용
1차 조건  : 최근 종가 >= 5,000원
2차 조건  : 최근 20거래일 평균 거래대금(종가 × 거래량) >= 300억 원
시그널    : ① 최근 봉에서 MACD 골든크로스
            ② 최근 봉을 제외한 직전 10개 봉 중 이격도 <= 85 인 봉 존재
휴장일    : 최근 봉 날짜가 오늘(한국시간)이 아니면 시그널을 보내지 않음
            · 주말·KRX 휴장일(krx_calendar.py)이면 '휴장일' 안내 (성공)
            · 거래일인데 오늘 봉이 없으면 '시세 지연' 안내 + 실패 종료 → 17:30 백업 실행이 다시 시도

텔레그램  : 환경변수 TELEGRAM_TOKEN_KR(국장 전용 봇), TELEGRAM_CHAT_ID
            (미국용 TELEGRAM_TOKEN은 쓰지 않습니다)
시그널    : SIGNAL_DELIVERY=ssm 이면 SSM /scanner-kr/signals/ 에 저장 → EC2 bot_kr.py가 원화 예수금을 붙여 전송

필요 패키지 : pip install yfinance pandas numpy lxml
사용 예     : python scanner_kr.py                          (전체 스캔 → 오늘 시그널)
              python scanner_kr.py --codes 005930 000660     (지정 종목의 과거 시그널 일자)
              python scanner_kr.py --codes 삼성전자,SK하이닉스
              python scanner_kr.py --limit 200               (테스트용: 앞 200종목만)
              python scanner_kr.py --update-list             (kr_tickers.json 백업 목록 갱신)
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from krx_calendar import is_trading_day

# ───── 전제 조건 (scanner.py와 동일) ─────
MACD_FAST = 5
MACD_SLOW = 20
MACD_SIGNAL = 5
DISP_LEN = 15          # 이격도 SMA 기간
DISP_TH = 85.0         # 이격도 과매도 기준 (이하)
LOOKBACK = 10          # 과매도 확인 구간 (오늘 제외 봉 수)

# ───── 종목 필터 조건 ─────
MIN_PRICE_KRW = 5_000                # 1차: 주가 5,000원 이상
MIN_TURNOVER_KRW = 30_000_000_000    # 2차: 평균 거래대금 300억 원 이상
TURNOVER_DAYS = 20                   # 거래대금 평균 기간

KST_TZ = "Asia/Seoul"
MARKET_CLOSE_HM = (15, 40)           # 이 시각(한국시간) 전이면 오늘 봉은 미완성으로 보고 제외

# ───── 종목 목록 (KRX KIND 상장법인 목록 다운로드) ─────
KIND_URL = "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&marketType={}"
KIND_MARKETS = {"stockMkt": ("KOSPI", ".KS"), "kosdaqMkt": ("KOSDAQ", ".KQ")}
FALLBACK_LIST = Path(__file__).with_name("kr_tickers.json")
EXCLUDE_NAME_RE = re.compile(r"스팩|기업인수목적")
CODE_RE = re.compile(r"^[0-9][0-9A-Z]{5}$")      # 6자리 단축코드 (신규 상장은 영문 포함 가능, 예: 0220W0)

TELEGRAM_MAX_LEN = 4000
SIGNALS_PREFIX = "/scanner-kr/signals/"


# ════════════════════════════════════════════════════════════
# 0. 텔레그램 (국장 전용 봇 토큰)
# ════════════════════════════════════════════════════════════
def _split_message(text: str, limit: int = TELEGRAM_MAX_LEN) -> list[str]:
    chunks, cur = [], ""
    for line in text.splitlines():
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks or [text[:limit]]


def signal_keyboard(code: str) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ 승인", "callback_data": f"buy:{code}"},
        {"text": "⏭ 패스", "callback_data": f"pass:{code}"},
    ]]}


def send_telegram(text: str, reply_markup: dict | None = None) -> bool:
    token = os.environ.get("TELEGRAM_TOKEN_KR", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[텔레그램] TELEGRAM_TOKEN_KR / TELEGRAM_CHAT_ID 환경변수가 없어 전송을 건너뜁니다.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok = True
    chunks = _split_message(text)
    for idx, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": "true"}
        if reply_markup and idx == len(chunks) - 1:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    body = json.loads(r.read().decode("utf-8"))
                if not body.get("ok"):
                    print(f"[텔레그램] 전송 실패: {body.get('description')}")
                    ok = False
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", errors="replace")
                print(f"[텔레그램] HTTP {e.code}: {detail}")
                if e.code == 429 and attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
                ok = False
                break
            except Exception as e:
                print(f"[텔레그램] 전송 오류(재시도 {attempt + 1}/3): {type(e).__name__}")
                if attempt == 2:
                    ok = False
                time.sleep(3 * (attempt + 1))
    if ok:
        print("[텔레그램] 전송 완료")
    return ok


# ════════════════════════════════════════════════════════════
# 1. 종목 목록 (코스피 + 코스닥)
# ════════════════════════════════════════════════════════════
def _download_kind(market_type: str) -> pd.DataFrame:
    req = urllib.request.Request(KIND_URL.format(market_type), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        html = r.read().decode("euc-kr", errors="replace")
    df = pd.read_html(io.StringIO(html))[0]
    # 열 순서: 회사명, 시장구분, 종목코드, ... (열 이름 인코딩에 의존하지 않도록 위치로 읽음)
    return pd.DataFrame({"name": df.iloc[:, 0].astype(str).str.strip(),
                         "code": df.iloc[:, 2].astype(str).str.strip().str.upper().str.zfill(6)})


def fetch_universe_kind() -> list[dict]:
    rows = []
    for market_type, (market, suffix) in KIND_MARKETS.items():
        df = _download_kind(market_type)
        for r in df.itertuples(index=False):
            if CODE_RE.match(r.code) and not EXCLUDE_NAME_RE.search(r.name):
                rows.append({"code": r.code, "name": r.name, "market": market, "yf": r.code + suffix})
        print(f"  {market}: {len(df):,}개 법인")
    if len(rows) < 1000:                       # 비정상 응답 방어
        raise RuntimeError(f"종목 수가 너무 적음 ({len(rows)}개)")
    return rows


def get_universe() -> tuple[list[dict], str]:
    """(종목 목록, 출처). KIND 실패 시 kr_tickers.json."""
    try:
        return fetch_universe_kind(), "KIND"
    except Exception as e:
        print(f"[경고] KIND 종목 목록 다운로드 실패 → 백업 목록 사용 ({type(e).__name__}: {e})")
    data = json.loads(FALLBACK_LIST.read_text(encoding="utf-8"))
    return data["tickers"], f"백업 목록({data.get('updated', '?')})"


def update_fallback_list() -> None:
    rows = fetch_universe_kind()
    FALLBACK_LIST.write_text(json.dumps(
        {"updated": pd.Timestamp.now(tz=KST_TZ).strftime("%Y-%m-%d"), "tickers": rows},
        ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"{FALLBACK_LIST.name} 갱신: {len(rows):,}종목")


# ════════════════════════════════════════════════════════════
# 2. 지표 계산 (scanner.py와 동일)
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


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(subset=["Close", "Volume"])
    return df[df["Volume"] > 0]


def compute_indicators(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    macd = pine_ema(close, MACD_FAST) - pine_ema(close, MACD_SLOW)
    signal = pine_ema(macd, MACD_SIGNAL)
    disparity = close / close.rolling(DISP_LEN).mean() * 100
    return macd, signal, disparity


def evaluate(df: pd.DataFrame) -> dict | None:
    df = _clean(df)
    need = max(MACD_SLOW + MACD_SIGNAL + 2, DISP_LEN + LOOKBACK + 1, TURNOVER_DAYS)
    if len(df) < need:
        return None

    close = df["Close"].astype(float)
    volume = df["Volume"].astype(float)
    last_close = close.iloc[-1]
    avg_turnover = (close * volume).iloc[-TURNOVER_DAYS:].mean()
    macd, signal, disparity = compute_indicators(close)

    golden_cross = bool(macd.iloc[-1] > signal.iloc[-1] and macd.iloc[-2] <= signal.iloc[-2])
    min_disp = disparity.iloc[-LOOKBACK - 1:-1].min()

    return {
        "Date": df.index[-1].date(),
        "Close": round(last_close),
        "AvgTurnoverKRW": avg_turnover,
        "Disparity": round(disparity.iloc[-1], 2),
        "MinDisp10": round(min_disp, 2),
        "MACD": round(macd.iloc[-1], 2),
        "Signal": round(signal.iloc[-1], 2),
        "Pass1_Price": bool(last_close >= MIN_PRICE_KRW),
        "Pass2_Turnover": bool(avg_turnover >= MIN_TURNOVER_KRW),
        "GoldenCross": golden_cross,
        "WasOversold": bool(min_disp <= DISP_TH),
    }


def find_signal_dates(df: pd.DataFrame) -> pd.DataFrame:
    df = _clean(df)
    close = df["Close"].astype(float)
    volume = df["Volume"].astype(float)
    macd, signal, disparity = compute_indicators(close)

    golden_cross = (macd > signal) & (macd.shift(1) <= signal.shift(1))
    min_disp = disparity.rolling(LOOKBACK).min().shift(1)
    hit = golden_cross & (min_disp <= DISP_TH)

    turnover = (close * volume).rolling(TURNOVER_DAYS).mean()
    out = pd.DataFrame({
        "Close": close.round(0),
        "Disparity": disparity.round(2),
        "MinDisp10": min_disp.round(2),
        "AvgTurnoverKRW": turnover,
    })[hit]
    out["PassFilter"] = (out["Close"] >= MIN_PRICE_KRW) & (out["AvgTurnoverKRW"] >= MIN_TURNOVER_KRW)
    return out


# ════════════════════════════════════════════════════════════
# 3. 데이터 다운로드
# ════════════════════════════════════════════════════════════
def download_batch(tickers: list[str], period: str) -> dict[str, pd.DataFrame]:
    for attempt in range(3):
        try:
            data = yf.download(tickers, period=period, interval="1d", auto_adjust=False,
                               actions=False, group_by="ticker", threads=True, progress=False)
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
                sub = data[t].dropna(how="all")
                if not sub.empty:
                    result[t] = sub
    elif len(tickers) == 1:
        result[tickers[0]] = data
    return result


def kst_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz=KST_TZ)


def drop_incomplete_bar(df: pd.DataFrame) -> pd.DataFrame:
    """국내 장 마감(15:30) 후 시세가 정리되기 전이면 오늘 진행 중인 봉 제외."""
    if df.empty:
        return df
    now = kst_now()
    if df.index[-1].date() == now.date() and (now.hour, now.minute) < MARKET_CLOSE_HM:
        return df.iloc[:-1]
    return df


# ════════════════════════════════════════════════════════════
# 4. 실행 모드
# ════════════════════════════════════════════════════════════
def resolve_codes(raw: list[str], universe: list[dict]) -> tuple[list[dict], list[str]]:
    """'005930' / '삼성전자' → 종목 정보. (찾은 종목, 못 찾은 입력)."""
    by_code = {u["code"]: u for u in universe}
    by_name = {u["name"].replace(" ", "").upper(): u for u in universe}
    found, missing = [], []
    for item in raw:
        key = item.strip().upper()
        u = by_code.get(key.zfill(6) if key.isdigit() else key) or by_name.get(key.replace(" ", ""))
        if u:
            if u not in found:
                found.append(u)
        else:
            missing.append(item)
    return found, missing


def run_codes(args, raw_codes: list[str]) -> tuple[str, bool, list]:
    period = args.period or "max"
    universe, _ = get_universe()
    stocks, missing = resolve_codes(raw_codes, universe)
    label = ", ".join(f"{s['name']}({s['code']})" for s in stocks)
    print(f"▶ 종목 조회: {label} (기간 {period})")

    lines = ["🔍 [국장] MACD GC + 이격도 과매도 시그널 조회", f"종목: {label or '-'} | 기간: {period}", ""]
    lines += [f"[{m}] 코스피·코스닥 종목에서 찾지 못했어요" for m in missing]
    frames = download_batch([s["yf"] for s in stocks], period) if stocks else {}
    any_data = False
    for s in stocks:
        title = f"{s['name']}({s['code']})"
        df = frames.get(s["yf"])
        if df is None or df.empty:
            lines += [f"[{title}] 데이터 없음", ""]
            continue
        any_data = True
        if not args.include_today:
            df = drop_incomplete_bar(df)
        sig = find_signal_dates(df)
        start, end = df.index[0].date(), df.index[-1].date()
        if sig.empty:
            lines += [f"[{title}] 시그널 없음 ({start} ~ {end})", ""]
            continue
        shown = sig.tail(args.max_signals).iloc[::-1] if args.max_signals > 0 else sig.iloc[::-1]
        lines.append(f"[{title}] 시그널 {len(sig)}회 ({start} ~ {end})"
                     + (f", 최근 {len(shown)}개" if len(shown) < len(sig) else ""))
        for d, r in shown.iterrows():
            mark = "" if r["PassFilter"] else "  (1·2차 조건 미충족)"
            lines.append(f"• {d.date()}  {r['Close']:,.0f}원{mark}")
        lines.append("")
        print(f"\n[{title}] 시그널 {len(sig)}회")
        print(shown.drop(columns="AvgTurnoverKRW").to_string())

    return "\n".join(lines).strip(), any_data, []


def run_scan(args) -> tuple[str, bool, list]:
    period = args.period or "1y"
    universe, source = get_universe()
    if args.limit:
        universe = universe[:args.limit]
    info = {u["yf"]: u for u in universe}
    tickers = list(info)
    print(f"▶ 코스피+코스닥 {len(tickers):,}종목 스캔 (목록 출처: {source})")

    rows = []
    n_batches = (len(tickers) + args.batch_size - 1) // args.batch_size
    for b in range(n_batches):
        batch = tickers[b * args.batch_size:(b + 1) * args.batch_size]
        print(f"▶ 다운로드 {b + 1}/{n_batches} ({len(batch)}종목)", end="\r", flush=True)
        for t, df in download_batch(batch, period).items():
            if not args.include_today:
                df = drop_incomplete_bar(df)
            try:
                res = evaluate(df)
            except Exception:
                res = None
            if res:
                res.update(Ticker=t, Code=info[t]["code"], Name=info[t]["name"], Market=info[t]["market"])
                rows.append(res)
        time.sleep(1)
    print()

    if not rows:
        return f"⚠️ [국장] 스캔 실패: 시세 데이터를 받지 못했습니다. (대상 {len(tickers):,}종목)", False, []

    res = pd.DataFrame(rows)
    scan_date = res["Date"].max()
    res = res[res["Date"] == scan_date]

    today = kst_now().date()
    if scan_date != today and not args.allow_stale:
        if not is_trading_day(today):
            msg = f"📭 [국장] 오늘({today})은 휴장일이에요 — 스캔 결과를 보내지 않아요."
            print(msg)
            return msg, True, []
        # 거래일인데 오늘 봉이 없음 → 시세 지연. 실패로 끝내서 17:30 백업 실행이 다시 시도하게 함
        msg = (f"⚠️ [국장] 오늘({today}) 시세가 아직 반영되지 않았어요 (최근 데이터 {scan_date}).\n"
               f"17:30 백업 실행 때 다시 시도해요. 그때도 이 메시지가 오면 Actions에서 수동 실행해주세요.")
        print(msg)
        return msg, False, []

    res["AvgTurnover_억"] = (res["AvgTurnoverKRW"] / 1e8).round(0)
    passed = res[res["Pass1_Price"] & res["Pass2_Turnover"]]
    hits = passed[passed["GoldenCross"] & passed["WasOversold"]]
    hits = hits.sort_values("AvgTurnoverKRW", ascending=False).copy()
    print(f"  기준일 {scan_date} | 데이터 {len(res):,}개 → 1·2차 통과 {len(passed):,}개 → 시그널 {len(hits):,}개")

    cols = ["Code", "Name", "Market", "Date", "Close", "Disparity", "MinDisp10",
            "MACD", "Signal", "AvgTurnover_억"]
    if hits.empty:
        print("\n오늘 조건을 만족하는 종목이 없습니다.")
    else:
        with pd.option_context("display.max_rows", None, "display.width", 200):
            print("\n" + hits[cols].to_string(index=False))

    out = args.out or f"scan_kr_{kst_now():%Y%m%d}.csv"
    hits[cols].to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n결과 저장: {out}")

    header = [f"🇰🇷 [국장] MACD GC + 이격도 과매도 스캔 ({scan_date})",
              f"대상 {len(res):,}종목 → 1·2차 통과 {len(passed):,} → 시그널 {len(hits):,}", ""]
    if source != "KIND":
        header.insert(1, f"⚠️ KIND 목록 다운로드 실패 → {source} 사용")
    if hits.empty:
        body = ["📭 오늘은 시그널 없음"]
    else:
        body = [f"{i}. {r.Name}({r.Code}) {r.Market} | {r.Close:,.0f}원"
                for i, r in enumerate(hits.itertuples(index=False), 1)]
        body += ["", "👇 종목별 메시지에서 승인/패스를 선택하세요."]

    signal_msgs = []
    for r in hits.itertuples(index=False):
        text = "\n".join([
            f"📈 [국장] 시그널: {r.Name} ({r.Code})",
            f"시장: {r.Market} | 날짜: {r.Date}",
            f"종가: {r.Close:,.0f}원",
            f"이격도: {r.Disparity:.2f} (직전 10봉 최저 {r.MinDisp10:.2f})",
            f"20일 평균 거래대금: 약 {r.AvgTurnover_억:,.0f}억 원",
        ])
        signal_msgs.append((r.Code, r.Name, text))
    return "\n".join(header + body), True, signal_msgs


# ════════════════════════════════════════════════════════════
# 5. 시그널 전달 / 메인
# ════════════════════════════════════════════════════════════
def deliver_signals(signal_msgs: list[tuple[str, str, str]]) -> None:
    """SIGNAL_DELIVERY=ssm → SSM /scanner-kr/signals/ 저장 (EC2 bot_kr.py가 원화 예수금 붙여 전송).
    그 외 또는 저장 실패 → 텔레그램 직접 전송."""
    if not signal_msgs:
        return
    saved = 0
    if os.environ.get("SIGNAL_DELIVERY", "").lower() == "ssm":
        try:
            import boto3
            ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "ap-northeast-2"))
            batch = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            for i, (code, name, text) in enumerate(signal_msgs, 1):
                ssm.put_parameter(
                    Name=f"{SIGNALS_PREFIX}{batch}-{i:03d}-{code}",
                    Value=json.dumps({"symbol": code, "name": name, "text": text, "created_at": batch},
                                     ensure_ascii=False),
                    Type="String", Overwrite=True)
                saved += 1
            print(f"[SSM] 시그널 {saved}개 저장 → EC2 bot_kr.py가 원화 예수금을 붙여 전송")
            return
        except Exception as e:
            print(f"[SSM] 시그널 저장 실패({saved}개 저장됨) → 나머지는 텔레그램 직접 전송: {type(e).__name__}: {e}")
    for code, _name, text in signal_msgs[saved:]:
        time.sleep(0.5)
        send_telegram(text, reply_markup=signal_keyboard(code))


def parse_codes(raw: list[str] | None) -> list[str]:
    if not raw:
        return []
    codes = [c.strip() for item in raw for c in item.split(",") if c.strip()]
    return list(dict.fromkeys(codes))


def main() -> None:
    ap = argparse.ArgumentParser(description="[국장] MACD 골든크로스 + 이격도 과매도 스캐너")
    ap.add_argument("--codes", nargs="+", help="조회할 종목 코드 또는 이름 (예: --codes 005930 삼성전자)")
    ap.add_argument("--max-signals", type=int, default=10, help="--codes 조회 시 종목별 최근 시그널 수 (0 = 전부)")
    ap.add_argument("--limit", type=int, default=0, help="앞에서부터 N종목만 스캔 (테스트용)")
    ap.add_argument("--batch-size", type=int, default=100, help="yfinance 한 번에 받을 종목 수")
    ap.add_argument("--period", default=None, help="다운로드 기간 (기본: 전체 스캔 1y, --codes 조회 max)")
    ap.add_argument("--include-today", action="store_true", help="장중 미완성 봉도 포함")
    ap.add_argument("--allow-stale", action="store_true", help="최근 봉이 오늘이 아니어도 시그널 판정 (휴장일 검사 끔)")
    ap.add_argument("--out", default="", help="결과 CSV 경로 (기본: scan_kr_YYYYMMDD.csv)")
    ap.add_argument("--update-list", action="store_true", help="kr_tickers.json 백업 목록만 갱신하고 종료")
    args = ap.parse_args()

    if args.update_list:
        update_fallback_list()
        return

    codes = parse_codes(args.codes)
    try:
        message, ok, signal_msgs = run_codes(args, codes) if codes else run_scan(args)
    except Exception as e:
        mode = f"종목 조회({', '.join(codes)})" if codes else "전체 스캔"
        send_telegram(f"❌ [국장] 스캐너 실행 오류 - {mode}\n{type(e).__name__}: {e}")
        raise

    send_telegram(message)
    deliver_signals(signal_msgs)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
