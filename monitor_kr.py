"""
monitor_kr.py  (국내주식 — 미국용 monitor.py와 완전히 별개)
──────────────────────────────────────────────────────────────
SSM /scanner-kr/positions 의 종목을 하나씩 확인해서 매수·체결·손절·익절을 처리합니다. (EC2 크론, 1분마다)
동작 흐름은 monitor.py와 같고, 장 시간·통화·호가단위·수수료만 국내주식에 맞췄습니다.

  APPROVED : 국내 매매 시간(한국시간 09:00~15:20, 주말·KRX 휴장일 제외)이 아니면 대기
             장중이면 원화 예수금·현재가 조회 → min(승인 금액, 예수금) ÷ 현재가 = 정수 수량
               · 1주도 못 사면 '매수 불가' 알림 + SSM에서 삭제
               · 시장가 매수 → PENDING 저장 → '주문 접수' 알림
               · 예수금 부족으로 거절되면(시장가 주문은 증거금을 더 잡을 수 있음) 수량을 줄여 1번 재시도
  PENDING  : 보유 내역으로 체결 확인 → OPEN(+매수가, 수량) + 손절·익절 SINGLE 시장가 조건주문 2개 등록
  OPEN     : 매매 시간: 조건주문 2개가 살아있는지 확인 → 없으면 재등록
               · 현재가가 이미 트리거를 넘었으면 조건주문 대신 즉시 시장가 매도
                 → 조건주문 등록이 계속 실패해도 1분마다 현재가로 손절·익절을 직접 확인하는 효과
               · 현재가 ≥ 매수가 +3% → 손절을 본전(매수가 +0.25%)으로 옮기고 BREAKEVEN
             매매 시간 아님(15:20 이후 동시호가·시간외·NXT 포함): 조건주문 2개 모두 취소
  BREAKEVEN: OPEN과 같지만 손절가가 본전으로 고정
  공통     : 보유 수량이 0이 되면 남은 조건주문 정리 + 포지션 삭제 + 알림 (2번 연속 0일 때)

  손절 -3% / 익절 +5% / +3% 본전 이동 (monitor.py와 동일)
  수수료 버퍼 0.25% (토스 국내 매수·매도 수수료 + 매도 시 증권거래세 약 0.2%)
  트리거 가격은 KRX 호가단위에 맞춰 올림

환경변수 (서버 .env)
  TELEGRAM_TOKEN_KR(국장 전용 봇), TELEGRAM_CHAT_ID, TOSS_CLIENT_ID, TOSS_CLIENT_SECRET, AWS_REGION(선택)

실행
  python monitor_kr.py                          # 실제 실행 (크론)
  python monitor_kr.py --dry-run                # 주문·SSM 저장·텔레그램 없이 계산만 출력
  python monitor_kr.py --dry-run --assume-open  # 장 마감 중에도 장중이라 가정 (테스트용)
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import boto3

from krx_calendar import EXTRA_CLOSED_DAYS, HOLIDAY_YEARS, KRX_HOLIDAYS, LATE_OPEN_DAYS

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN_KR", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TOSS_CLIENT_ID = os.environ.get("TOSS_CLIENT_ID", "").strip()
TOSS_CLIENT_SECRET = os.environ.get("TOSS_CLIENT_SECRET", "").strip()
AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-2")

TOSS_BASE = "https://openapi.tossinvest.com"
SSM_KEY = "/scanner-kr/positions"
KST = ZoneInfo("Asia/Seoul")

PENDING_STALE_MIN = 60
UNKNOWN_ORDER_GIVEUP_MIN = 30

STOP_LOSS_PCT   = -0.03
TAKE_PROFIT_PCT =  0.05
BREAKEVEN_PCT   =  0.03
FEE_BUFFER      =  0.0025   # 토스 국내 수수료(매수·매도) + 매도 증권거래세(약 0.2%)
COND_EXPIRE_DAYS = 90
MARKET_BUY_MARGIN = 1.3     # 예수금 부족 거절 시, 현재가 × 1.3 기준으로 수량을 줄여 1번 재시도
HELD = ("OPEN", "BREAKEVEN")

ssm = boto3.client("ssm", region_name=AWS_REGION)
DRY_RUN = False


def log(msg: str) -> None:
    print(f"[{datetime.now(KST):%Y-%m-%d %H:%M:%S} KST] {msg}", flush=True)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def minutes_since(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.0
    return (datetime.now(timezone.utc) - t).total_seconds() / 60


def won(x: float) -> str:
    return f"{x:,.0f}원"


def label(symbol: str, pos: dict | None) -> str:
    name = (pos or {}).get("name")
    return f"{name}({symbol})" if name else symbol


# ════════════════════════════════════════════════════════════
# 장 시간 (한국거래소)
# ════════════════════════════════════════════════════════════
# 휴장일·개장 지연일은 krx_calendar.py에서 관리 (scanner_kr.py와 공용)
TRADE_START_MIN = 9 * 60          # 09:00
TRADE_END_MIN = 15 * 60 + 20      # 15:20 (이후는 종가 단일가 매매 → 주문·조건주문 관리 안 함)


def market_status(now: datetime | None = None) -> str:
    now = (now or datetime.now(timezone.utc)).astimezone(KST)
    d = now.date()
    if d.weekday() >= 5:
        return "주말"
    if d in KRX_HOLIDAYS or d in EXTRA_CLOSED_DAYS:
        return "KRX 휴장일"
    minutes = now.hour * 60 + now.minute
    if minutes < LATE_OPEN_DAYS.get(d, TRADE_START_MIN):
        return "개장 전"
    if minutes >= TRADE_END_MIN:
        return "장 마감(15:20 이후)"
    return "장중"


def is_market_open(now: datetime | None = None) -> bool:
    return market_status(now) == "장중"


# ════════════════════════════════════════════════════════════
# KRX 호가단위 (2023-01-25부터 코스피·코스닥 공통)
# ════════════════════════════════════════════════════════════
def tick_size(price: float) -> int:
    for limit, tick in ((2_000, 1), (5_000, 5), (20_000, 10), (50_000, 50),
                        (200_000, 100), (500_000, 500)):
        if price < limit:
            return tick
    return 1_000


def ceil_tick(price: float) -> int:
    t = tick_size(price)
    return int(math.ceil(price / t - 1e-9) * t)


# ════════════════════════════════════════════════════════════
# 텔레그램 (국장 전용 봇)
# ════════════════════════════════════════════════════════════
def notify(text: str) -> None:
    if DRY_RUN:
        log(f"(dry-run) 텔레그램 생략: {text.splitlines()[0]}")
        return
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("TELEGRAM_TOKEN_KR / TELEGRAM_CHAT_ID 없음 → 알림 생략")
        return
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": "[국장] " + text}).encode()
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data=data),
            timeout=10)
    except Exception as e:
        log(f"텔레그램 전송 실패: {type(e).__name__}: {e}")


# ════════════════════════════════════════════════════════════
# SSM — bot_kr.py와 같은 /scanner-kr/positions 를 함께 씀 (읽고 → 이 종목만 바꿔서 → 저장)
# ════════════════════════════════════════════════════════════
def load_positions() -> dict:
    try:
        value = ssm.get_parameter(Name=SSM_KEY)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return {}
    return json.loads(value) if value.strip() else {}


def save_positions(positions: dict) -> None:
    ssm.put_parameter(Name=SSM_KEY, Value=json.dumps(positions, ensure_ascii=False),
                      Type="String", Overwrite=True)


def update_position(symbol: str, data: dict) -> None:
    if DRY_RUN:
        log(f"(dry-run) SSM 저장 생략: {symbol} → {data.get('status')}")
        return
    positions = load_positions()
    positions[symbol] = data
    save_positions(positions)


def remove_position(symbol: str) -> None:
    if DRY_RUN:
        log(f"(dry-run) SSM 삭제 생략: {symbol}")
        return
    positions = load_positions()
    if positions.pop(symbol, None) is not None:
        save_positions(positions)


# ════════════════════════════════════════════════════════════
# 토스증권 OpenAPI (toss_api_docs.md 규격 + 국내주식: 6자리 코드, currency=KRW)
# ════════════════════════════════════════════════════════════
class TossError(Exception):
    def __init__(self, status: int, code: str = "", message: str = ""):
        self.status, self.code, self.message = status, code, message
        super().__init__(f"HTTP {status} {code} {message}".strip())


def read_body(resp) -> str:
    raw = resp.read()
    if resp.headers.get("Content-Encoding", "").lower() == "gzip" or raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    return raw.decode("utf-8", errors="replace")


def toss_request(method: str, path: str, token: str | None = None, account_seq=None,
                 form: dict | None = None, body: dict | None = None, retry_429: bool = True) -> dict:
    headers, data = {}, None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if account_seq is not None:
        headers["X-Tossinvest-Account"] = str(account_seq)
    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = urllib.parse.urlencode(form).encode()
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    for attempt in range(2):
        req = urllib.request.Request(TOSS_BASE + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = read_body(r)
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            raw = read_body(e)
            try:
                err = json.loads(raw).get("error", {})
            except Exception:
                err = {"message": raw[:200]}
            if e.code == 429 and retry_429 and attempt == 0:
                time.sleep(2)
                continue
            raise TossError(e.code, err.get("code", ""), err.get("message", "")) from None
    raise TossError(429, "rate-limit-exceeded")


class Toss:
    def __init__(self):
        if not TOSS_CLIENT_ID or not TOSS_CLIENT_SECRET:
            raise RuntimeError("TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 환경변수가 없습니다")
        res = toss_request("POST", "/oauth2/token", form={
            "grant_type": "client_credentials", "client_id": TOSS_CLIENT_ID, "client_secret": TOSS_CLIENT_SECRET,
        })
        self.token = res["access_token"]
        self.account_seq = toss_request("GET", "/api/v1/accounts", self.token)["result"][0]["accountSeq"]

    def buying_power_krw(self) -> float:
        res = toss_request("GET", "/api/v1/buying-power?currency=KRW", self.token, self.account_seq)
        return float(res["result"]["cashBuyingPower"])

    def last_price(self, symbol: str) -> float:
        res = toss_request("GET", f"/api/v1/prices?symbols={urllib.parse.quote(symbol)}", self.token)
        return float(res["result"][0]["lastPrice"])

    def holding(self, symbol: str) -> tuple[float, float]:
        res = toss_request("GET", f"/api/v1/holdings?symbol={urllib.parse.quote(symbol)}",
                           self.token, self.account_seq)
        for item in (res.get("result") or {}).get("items") or []:
            if item.get("symbol") == symbol:
                return float(item.get("quantity") or 0), float(item.get("averagePurchasePrice") or 0)
        return 0.0, 0.0

    def market_buy(self, symbol: str, quantity: int) -> str:
        res = toss_request("POST", "/api/v1/orders", self.token, self.account_seq, body={
            "symbol": symbol, "side": "BUY", "orderType": "MARKET", "quantity": str(int(quantity)),
        }, retry_429=False)
        return res["result"]["orderId"]

    def market_sell(self, symbol: str, quantity: int) -> str:
        if DRY_RUN:
            log(f"(dry-run) 시장가 매도 생략: {symbol} {quantity}주")
            return "DRY-RUN"
        res = toss_request("POST", "/api/v1/orders", self.token, self.account_seq, body={
            "symbol": symbol, "side": "SELL", "orderType": "MARKET", "quantity": str(int(quantity)),
        }, retry_429=False)
        return res["result"]["orderId"]

    def place_sell_trigger(self, symbol: str, quantity: int, trigger_price: int) -> str:
        if DRY_RUN:
            log(f"(dry-run) 조건주문 등록 생략: {symbol} {quantity}주 @ {won(trigger_price)}")
            return "DRY-RUN"
        expire = (datetime.now(KST) + timedelta(days=COND_EXPIRE_DAYS)).strftime("%Y-%m-%d")
        res = toss_request("POST", "/api/v1/conditional-orders", self.token, self.account_seq, body={
            "symbol": symbol, "type": "SINGLE", "quantity": str(int(quantity)), "orderType": "MARKET",
            "expireDate": expire, "first": {"orderSide": "SELL", "triggerPrice": str(int(trigger_price))},
        }, retry_429=False)
        return res["result"]["conditionalOrderId"]

    def conditional_active(self, cond_id: str | None) -> bool:
        if not cond_id:
            return False
        try:
            toss_request("GET", f"/api/v1/conditional-orders/{urllib.parse.quote(str(cond_id))}",
                         self.token, self.account_seq)
            return True
        except TossError as e:
            if "not-found" in e.code:
                return False
            raise

    def cancel_conditional(self, cond_id: str | None) -> None:
        if not cond_id:
            return
        if DRY_RUN:
            log(f"(dry-run) 조건주문 취소 생략: {cond_id}")
            return
        try:
            toss_request("DELETE", f"/api/v1/conditional-orders/{urllib.parse.quote(str(cond_id))}",
                         self.token, self.account_seq)
        except TossError as e:
            if "not-found" not in e.code:
                raise
            log(f"조건주문 {cond_id} 이미 종료됨 (무시)")


# ════════════════════════════════════════════════════════════
# APPROVED → 매수 주문
# ════════════════════════════════════════════════════════════
def handle_approved(toss_getter, symbol: str, pos: dict, assume_open: bool) -> None:
    lbl = label(symbol, pos)
    if not (assume_open or is_market_open()):
        log(f"{lbl}: 매수 대기 중 ({market_status()})")
        return

    toss = toss_getter()
    budget = float(pos.get("budget") or 0)
    available = toss.buying_power_krw()
    spend = min(budget, available)
    price = toss.last_price(symbol)
    qty = math.floor(spend / price) if price > 0 else 0
    log(f"{lbl}: 승인 {won(budget)} / 예수금 {won(available)} → 사용 {won(spend)}, 현재가 {won(price)} → {qty}주")

    if qty < 1:
        reason = (f"원화 예수금({won(available)})이 부족해요. 토스 계좌에 원화를 입금해주세요."
                  if available < budget else
                  f"승인 금액 {won(budget)}으로는 1주({won(price)})도 살 수 없어요.")
        notify(f"⚠️ {lbl} 매수 불가\n{reason}\n승인 기록을 지웠어요. 필요하면 다시 승인해주세요.")
        remove_position(symbol)
        return

    latest = load_positions().get(symbol)
    if not latest or latest.get("status") != "APPROVED":
        log(f"{lbl}: 주문 직전 확인 결과 승인이 취소됨 → 주문 안 함")
        return

    qty_before, avg_before = toss.holding(symbol)
    base = {**latest, "account_seq": toss.account_seq, "ref_price": price,
            "holding_before": qty_before, "avg_before": avg_before, "ordered_at": utc_now_iso()}

    if DRY_RUN:
        log(f"(dry-run) 시장가 매수 생략: {lbl} {qty}주 (기존 보유 {qty_before:g}주)")
        return

    attempt_qty = qty
    try:
        try:
            order_id = toss.market_buy(symbol, attempt_qty)
        except TossError as e:
            reduced = math.floor(spend / (price * MARKET_BUY_MARGIN))
            if e.code != "insufficient-buying-power" or not (1 <= reduced < attempt_qty):
                raise
            log(f"{lbl}: {attempt_qty}주 예수금 부족 거절 → {reduced}주로 재시도")
            attempt_qty = reduced
            order_id = toss.market_buy(symbol, attempt_qty)
    except TossError as e:
        if e.code == "insufficient-buying-power":
            notify(f"⚠️ {lbl} 매수 불가\n원화 예수금이 부족해요 (예수금 {won(available)}).\n승인 기록을 지웠어요.")
            remove_position(symbol)
            return
        err_key = f"{e.code or e.status}:{datetime.now(KST):%Y-%m-%d}"
        log(f"{lbl}: 주문 거절 {e} → APPROVED 유지, 다음 실행에 재시도")
        if latest.get("last_order_error") != err_key:
            notify(f"⚠️ {lbl} 매수 주문 실패\n{e.message or e.code or e.status}\n다음 확인 때 다시 시도할게요.")
            update_position(symbol, {**latest, "last_order_error": err_key})
        return
    except Exception as e:
        log(f"{lbl}: 주문 응답 없음 ({type(e).__name__}: {e}) → 체결 여부로 확인")
        update_position(symbol, {**base, "quantity_ordered": attempt_qty, "status": "PENDING",
                                 "order_id": None, "order_unknown": True})
        notify(f"⚠️ {lbl} 매수 주문 결과를 확인하지 못했어요 ({attempt_qty}주).\n"
               f"체결되면 알려드릴게요. 토스 앱에서도 확인해주세요.")
        return

    update_position(symbol, {**base, "quantity_ordered": attempt_qty, "status": "PENDING", "order_id": order_id})
    notify(f"🛒 {lbl} 시장가 매수 주문 접수\n{attempt_qty}주 × 약 {won(price)} = 약 {won(attempt_qty * price)}\n"
           f"체결되면 다시 알려드릴게요.")
    log(f"{lbl}: 주문 접수 orderId={order_id} {attempt_qty}주")


# ════════════════════════════════════════════════════════════
# PENDING → 체결 확인
# ════════════════════════════════════════════════════════════
def handle_pending(toss_getter, symbol: str, pos: dict, assume_open: bool = False) -> None:
    lbl = label(symbol, pos)
    toss = toss_getter()
    qty_now, avg_now = toss.holding(symbol)
    qty_before = float(pos.get("holding_before") or 0)
    avg_before = float(pos.get("avg_before") or 0)
    ordered = float(pos.get("quantity_ordered") or 0)
    filled = qty_now - qty_before

    if filled <= 0 or (ordered and filled < ordered):
        age = minutes_since(pos.get("ordered_at"))
        log(f"{lbl}: 아직 체결 전 (보유 {qty_now:g}주, 주문 전 {qty_before:g}주, 주문 {ordered:g}주, {age:.0f}분 경과)")
        if pos.get("order_unknown") and filled <= 0 and age >= UNKNOWN_ORDER_GIVEUP_MIN:
            notify(f"⚠️ {lbl}: 주문 후 {age:.0f}분 동안 체결이 확인되지 않아 기록을 정리했어요.\n"
                   f"토스 앱에서 주문 내역을 확인해주세요.")
            remove_position(symbol)
        elif age >= PENDING_STALE_MIN and not pos.get("stale_notified"):
            notify(f"⏳ {lbl}: 주문 후 {age:.0f}분이 지났는데 아직 체결이 확인되지 않아요.\n"
                   f"토스 앱에서 주문 상태를 확인해주세요.")
            update_position(symbol, {**pos, "stale_notified": True})
        return

    if qty_before > 0 and avg_now > 0 and avg_before > 0:
        buy_price = (avg_now * qty_now - avg_before * qty_before) / filled
    else:
        buy_price = avg_now or float(pos.get("ref_price") or 0)

    opened = {**pos, "status": "OPEN", "buy_price": round(buy_price, 2), "quantity": int(filled),
              "filled_at": utc_now_iso(), "cond_id_stop": None, "cond_id_profit": None}
    update_position(symbol, opened)
    log(f"{lbl}: 체결 {filled:g}주 @ {won(buy_price)} → OPEN")

    fill_line = f"🔔 {lbl} 체결 완료!\n매수가: {won(buy_price)} × {filled:g}주 = {won(buy_price * filled)}"
    try:
        manage_held(toss_getter, symbol, opened, assume_open, fill_line=fill_line)
    except Exception as e:
        log(f"{lbl}: 체결 직후 조건주문 처리 오류 {type(e).__name__}: {e}")
        notify(f"{fill_line}\n⚠️ 손절·익절 조건주문 등록 중 오류가 나서, 다음 확인(1분 뒤) 때 다시 시도할게요.")


# ════════════════════════════════════════════════════════════
# OPEN / BREAKEVEN → 손절·익절 조건주문 관리
# ════════════════════════════════════════════════════════════
def trigger_prices(buy_price: float, status: str) -> tuple[int, int]:
    """(손절가, 익절가) — 호가단위로 올림. 손절 -2.75% (BREAKEVEN이면 +0.25%), 익절 +5.25%."""
    if status == "BREAKEVEN":
        stop = buy_price * (1 + FEE_BUFFER)
    else:
        stop = buy_price * (1 + STOP_LOSS_PCT + FEE_BUFFER)
    return ceil_tick(stop), ceil_tick(buy_price * (1 + TAKE_PROFIT_PCT + FEE_BUFFER))


def pct(price: float, base: float) -> str:
    return f"{(price / base - 1) * 100:+.1f}%" if base else "?"


def today_key(tag: str) -> str:
    return f"{tag}:{datetime.now(KST):%Y-%m-%d}"


def manage_held(toss_getter, symbol: str, pos: dict, assume_open: bool, fill_line: str | None = None) -> None:
    lbl = label(symbol, pos)
    toss = toss_getter()
    buy = float(pos["buy_price"])

    # ① 보유 수량 확인
    qty_now, _ = toss.holding(symbol)
    mine = qty_now - float(pos.get("holding_before") or 0)
    qty = int(min(float(pos.get("quantity") or 0), mine)) if mine > 0 else 0
    if qty < 1:
        if not pos.get("zero_seen"):
            log(f"{lbl}: 보유 수량 0 감지 → 다음 실행에서 한 번 더 확인 후 종료 처리")
            update_position(symbol, {**pos, "zero_seen": True})
            return
        close_position(toss, symbol, pos)
        return
    if pos.get("zero_seen"):
        pos = {**pos, "zero_seen": False}
        update_position(symbol, pos)

    if pos.get("sell_order_id"):
        age = minutes_since(pos.get("sell_ordered_at"))
        if age < 5:
            log(f"{lbl}: 긴급 매도 체결 대기 중 ({age:.0f}분 경과)")
            return
        notify(f"⚠️ {lbl}: 긴급 매도 주문 후 {age:.0f}분이 지났는데 아직 보유 중이에요 ({qty}주).\n"
               f"토스 앱에서 주문 상태를 확인해주세요. 자동 감시는 계속할게요.")
        pos = {k: v for k, v in pos.items() if k not in ("sell_order_id", "sell_ordered_at")}
        update_position(symbol, pos)

    if qty < int(pos.get("quantity") or 0):
        log(f"{lbl}: 보유 수량 감소 {pos.get('quantity')} → {qty}주, 조건주문을 새 수량으로 다시 등록")
        toss.cancel_conditional(pos.get("cond_id_stop"))
        toss.cancel_conditional(pos.get("cond_id_profit"))
        pos = {**pos, "quantity": qty, "cond_id_stop": None, "cond_id_profit": None}
        update_position(symbol, pos)

    # ② 매매 시간이 아니면 조건주문 전부 취소
    if not (assume_open or is_market_open()):
        if pos.get("cond_id_stop") or pos.get("cond_id_profit"):
            toss.cancel_conditional(pos.get("cond_id_stop"))
            pos = {**pos, "cond_id_stop": None}
            update_position(symbol, pos)
            toss.cancel_conditional(pos.get("cond_id_profit"))
            pos = {**pos, "cond_id_profit": None}
            update_position(symbol, pos)
            log(f"{lbl}: 매매 시간 아님({market_status()}) → 조건주문 2개 취소 (다음 장 시작 후 재등록)")
        else:
            log(f"{lbl}: 매매 시간 아님({market_status()}) → 조건주문 없이 대기 ({qty}주 보유)")
        if fill_line:
            notify(f"{fill_line}\n매매 시간(09:00~15:20)이 아니라서 손절·익절 조건주문은 다음 장에 등록할게요.")
        return

    price = toss.last_price(symbol)
    notes: list[str] = []

    # ③ +3% 달성 → 손절을 본전으로
    moved_to_be = False
    if pos["status"] == "OPEN" and price >= buy * (1 + BREAKEVEN_PCT):
        toss.cancel_conditional(pos.get("cond_id_stop"))
        pos = {**pos, "status": "BREAKEVEN", "cond_id_stop": None, "breakeven_at": utc_now_iso()}
        update_position(symbol, pos)
        moved_to_be = True
        log(f"{lbl}: 현재가 {won(price)} ({pct(price, buy)}) → BREAKEVEN")

    stop_p, profit_p = trigger_prices(buy, pos["status"])
    stop_label = "손절(본전)" if pos["status"] == "BREAKEVEN" else "손절"

    # ④ 조건주문 활성 확인
    stop_ok = toss.conditional_active(pos.get("cond_id_stop"))
    profit_ok = toss.conditional_active(pos.get("cond_id_profit"))

    # ⑤ 이미 트리거를 넘었으면 즉시 시장가 매도 (조건주문 등록이 실패해도 이 확인은 매분 동작)
    if not stop_ok and price <= stop_p:
        emergency_sell(toss, symbol, pos, qty, f"{stop_label}가 {won(stop_p)}인데 현재가가 {won(price)}예요.")
        return
    if not profit_ok and price >= profit_p:
        emergency_sell(toss, symbol, pos, qty, f"익절가 {won(profit_p)}인데 현재가가 {won(price)}예요.")
        return

    # ⑥ 없어진 조건주문 등록
    errors: list[str] = []
    for key, name, trig, ok in (("cond_id_stop", stop_label, stop_p, stop_ok),
                                 ("cond_id_profit", "익절", profit_p, profit_ok)):
        if ok:
            continue
        try:
            pos = {**pos, key: toss.place_sell_trigger(symbol, qty, trig)}
            update_position(symbol, pos)
            notes.append(f"{'🔴' if key == 'cond_id_stop' else '🟢'} {name}: {won(trig)} ({pct(trig, buy)}, 수수료·세금 반영)")
            log(f"{lbl}: {name} 조건주문 등록 {qty}주 @ {won(trig)} id={pos[key]}")
        except TossError as e:
            errors.append(f"{name} 등록 실패: {e.message or e.code or e.status}")
            log(f"{lbl}: {name} 조건주문 등록 실패 {e}")

    # ⑦ 알림
    err_text = ("\n⚠️ " + " / ".join(errors) +
                "\n등록될 때까지 1분마다 현재가를 직접 확인해서, 손절·익절가에 닿으면 시장가로 매도할게요.") if errors else ""
    if fill_line:
        notify(fill_line + ("\n" + "\n".join(notes) if notes else "")
               + ("\n📈 +3% 달성 시 손절 → 본전(매수가 +0.25%)으로 이동" if pos["status"] == "OPEN" else "")
               + err_text)
    elif moved_to_be:
        notify(f"📈 {lbl} +3% 달성! (현재가 {won(price)}, {pct(price, buy)})\n"
               f"손절 기준을 {won(stop_p)}(매수가 +0.25%, 수수료·세금 상쇄)로 옮겼어요."
               + ("\n" + "\n".join(n for n in notes if "익절" in n) if any("익절" in n for n in notes) else "")
               + err_text)
    elif notes or errors:
        if notes:
            notify(f"🔄 {lbl} 조건주문 등록 ({qty}주, 매수가 {won(buy)})\n" + "\n".join(notes) + err_text)
        elif pos.get("last_cond_error") != today_key("cond"):
            notify(f"⚠️ {lbl} 손절·익절 조건주문 등록 실패{err_text}")
            pos = {**pos, "last_cond_error": today_key("cond")}
            update_position(symbol, pos)
    else:
        log(f"{lbl}: {pos['status']} {qty}주, 현재가 {won(price)} ({pct(price, buy)}), "
            f"{stop_label} {won(stop_p)} / 익절 {won(profit_p)} 조건주문 유지 중")


def emergency_sell(toss, symbol: str, pos: dict, qty: int, reason: str) -> None:
    lbl = label(symbol, pos)
    try:
        toss.cancel_conditional(pos.get("cond_id_stop"))
        toss.cancel_conditional(pos.get("cond_id_profit"))
        pos = {**pos, "cond_id_stop": None, "cond_id_profit": None}
        order_id = toss.market_sell(symbol, qty)
    except Exception as e:
        log(f"{lbl}: 긴급 매도 실패 {type(e).__name__}: {e}")
        if pos.get("last_sell_error") != today_key("sell"):
            notify(f"⚠️ {lbl} 긴급 시장가 매도 실패\n{reason}\n오류: {e}\n"
                   f"1분마다 다시 시도할게요. 토스 앱에서도 직접 확인해주세요.")
            pos = {**pos, "last_sell_error": today_key("sell")}
        update_position(symbol, pos)
        return
    update_position(symbol, {**pos, "sell_order_id": order_id, "sell_ordered_at": utc_now_iso(),
                             "sell_reason": reason})
    notify(f"🚨 {lbl} 시장가 매도 ({qty}주)\n{reason}\n"
           f"조건주문이 없는 상태에서 가격이 이미 기준을 넘어서, 즉시 시장가로 매도했어요.\n"
           f"체결되면 다시 알려드릴게요.")
    log(f"{lbl}: 긴급 시장가 매도 {qty}주 orderId={order_id}")


def close_position(toss, symbol: str, pos: dict) -> None:
    lbl = label(symbol, pos)
    leftover = []
    for key in ("cond_id_stop", "cond_id_profit"):
        try:
            toss.cancel_conditional(pos.get(key))
        except Exception as e:
            leftover.append(str(pos.get(key)))
            log(f"{lbl}: 남은 조건주문 {pos.get(key)} 취소 실패 {e}")
    buy = float(pos.get("buy_price") or 0)
    try:
        price = toss.last_price(symbol)
        price_line = f"\n매수가 {won(buy)} → 현재가 약 {won(price)} ({pct(price, buy)})"
    except Exception:
        price_line = f"\n매수가 {won(buy)}"
    remove_position(symbol)
    how = "긴급 시장가 매도가 체결됐어요." if pos.get("sell_order_id") else \
          "보유 수량이 0이 됐어요 (토스 조건주문 체결 또는 직접 매도)."
    notify(f"📊 {lbl} 포지션 종료\n{how}{price_line}\n"
           + (f"⚠️ 조건주문 {', '.join(leftover)} 취소에 실패했어요. 토스 앱에서 직접 취소해주세요.\n" if leftover else "")
           + "토스 앱에서 체결 내역을 확인해주세요.")
    log(f"{lbl}: 포지션 종료 → SSM에서 삭제")


# ════════════════════════════════════════════════════════════
# 메인
# ════════════════════════════════════════════════════════════
def main() -> None:
    global DRY_RUN
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="주문·SSM 저장·텔레그램 없이 계산만")
    ap.add_argument("--assume-open", action="store_true", help="(--dry-run 전용) 장중이라고 가정")
    args = ap.parse_args()
    if args.assume_open and not args.dry_run:
        sys.exit("--assume-open 은 --dry-run 과 함께만 쓸 수 있어요 (장 마감 중 실제 주문 방지)")
    DRY_RUN = args.dry_run

    if datetime.now(KST).year not in HOLIDAY_YEARS:
        log(f"⚠️ KRX_HOLIDAYS에 {datetime.now(KST).year}년 휴장일이 없어요 — monitor_kr.py에 추가해주세요")

    positions = load_positions()
    if not positions:
        log("포지션 없음 → 종료")
        return
    log(f"포지션 {len(positions)}개: " + ", ".join(f"{label(s, p)}[{p.get('status')}]" for s, p in positions.items())
        + (" (dry-run)" if DRY_RUN else ""))

    toss_holder: dict = {}

    def toss_getter() -> Toss:
        if "t" not in toss_holder:
            toss_holder["t"] = Toss()
        return toss_holder["t"]

    for symbol, pos in positions.items():
        status = pos.get("status")
        try:
            if status == "APPROVED":
                handle_approved(toss_getter, symbol, pos, args.assume_open)
            elif status == "PENDING":
                handle_pending(toss_getter, symbol, pos, args.assume_open)
            elif status in HELD:
                manage_held(toss_getter, symbol, pos, args.assume_open)
            else:
                log(f"{symbol}: 알 수 없는 상태 {status!r} → 건너뜀")
        except Exception as e:
            log(f"{symbol} 처리 오류: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
