"""
monitor.py
──────────────────────────────────────────────────────────────
SSM /scanner/positions 의 종목을 하나씩 확인해서 매수·체결을 처리합니다. (EC2 크론, 1분마다)

  APPROVED : 미국 정규장(뉴욕 09:30~16:00, 서머타임 자동)이 아니면 '매수 대기 중' 출력 후 넘어감
             장중이면 달러 예수금·현재가 조회 → min(승인 금액, 예수금) ÷ 현재가 = 정수 수량
               · 1주도 못 사면 '매수 불가' 알림 + SSM에서 삭제
               · 살 수 있으면 시장가 매수 → PENDING(+order_id, account_seq) 저장 → '주문 접수' 알림
  PENDING  : 토스 보유 내역으로 체결 확인 → 체결되면 OPEN(+매수가, 수량) 저장 → '체결 완료' 알림
             + 손절(매수가 -2.8%)·익절(+5.2%) SINGLE 시장가 조건주문 2개 등록 (수수료 0.2% 반영)
             아직이면 다음 실행 때 다시 확인
  OPEN     : 정규장: 조건주문 2개가 살아있는지 확인 → 없으면 재등록
               · 단, 현재가가 이미 그 트리거 가격을 넘어섰으면 조건주문 대신 즉시 시장가 매도
                 (매도 트리거가 현재가보다 높게 걸리면 '오르면 매도'로 반대 해석되기 때문)
               · 현재가 ≥ 매수가 +3% → 손절 조건주문을 매수가 +0.2%(본전)로 옮기고 BREAKEVEN
             정규장 아님: 조건주문 2개 모두 취소 (장이 열리면 위 로직으로 자동 재등록)
  BREAKEVEN: OPEN과 같지만 손절가가 매수가 +0.2%(본전)로 고정
  공통     : 보유 수량이 0이 되면(조건주문 체결) 남은 조건주문 정리 + 포지션 삭제 + 알림
             (일시적인 조회 오류로 오판하지 않도록 2번 연속 0일 때 종료로 판단)

  토스 API는 toss_api_docs.md 규격만 사용합니다.

환경변수 (서버 .env)
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, TOSS_CLIENT_ID, TOSS_CLIENT_SECRET, AWS_REGION(선택)

실행
  python monitor.py                       # 실제 실행 (크론)
  python monitor.py --dry-run             # 주문·SSM 저장·텔레그램 없이 계산만 출력
  python monitor.py --dry-run --assume-open   # 장 마감 중에도 장중이라 가정하고 계산 (테스트용)
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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import boto3

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TOSS_CLIENT_ID = os.environ.get("TOSS_CLIENT_ID", "").strip()
TOSS_CLIENT_SECRET = os.environ.get("TOSS_CLIENT_SECRET", "").strip()
AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-2")

TOSS_BASE = "https://openapi.tossinvest.com"
SSM_KEY = "/scanner/positions"
NY = ZoneInfo("America/New_York")

PENDING_STALE_MIN = 60        # 이 시간(분) 넘게 체결이 안 보이면 1회 알림
UNKNOWN_ORDER_GIVEUP_MIN = 30 # 주문 응답을 못 받은 경우, 이 시간 동안 체결이 안 보이면 기록 정리

# 손절 / 익절 / 브레이크이븐
STOP_LOSS_PCT   = -0.03
TAKE_PROFIT_PCT =  0.05
BREAKEVEN_PCT   =  0.03
FEE_BUFFER      =  0.002   # 토스 왕복 수수료(매수 0.1% + 매도 0.1% + SEC Fee) 상쇄용
COND_EXPIRE_DAYS = 90      # 조건주문 만료일 = 등록일 + 90일 (매일 장 마감 때 취소하므로 사실상 안 씀)
HELD = ("OPEN", "BREAKEVEN")

ssm = boto3.client("ssm", region_name=AWS_REGION)
DRY_RUN = False


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


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


# ════════════════════════════════════════════════════════════
# 장 시간
# ════════════════════════════════════════════════════════════
def is_market_open(now: datetime | None = None) -> bool:
    """미국 정규장: 뉴욕 시간 평일 09:30~16:00 (서머타임은 America/New_York 시간대가 자동 적용).
    미국 공휴일·조기 폐장은 반영하지 않음 → 그날 주문이 거절되면 APPROVED로 남겨두고 다음 장에 재시도."""
    now = (now or datetime.now(timezone.utc)).astimezone(NY)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


# ════════════════════════════════════════════════════════════
# 텔레그램 (urllib)
# ════════════════════════════════════════════════════════════
def notify(text: str) -> None:
    if DRY_RUN:
        log(f"(dry-run) 텔레그램 생략: {text.splitlines()[0]}")
        return
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID 없음 → 알림 생략")
        return
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data=data),
            timeout=10)
    except Exception as e:
        log(f"텔레그램 전송 실패: {type(e).__name__}: {e}")


# ════════════════════════════════════════════════════════════
# SSM — bot.py와 같은 /scanner/positions 를 함께 씀
#   항상 '최신 값을 다시 읽고 → 이 종목만 바꿔서 → 저장' 해서,
#   그 사이 bot.py가 바꾼 다른 종목(새 승인·취소)을 덮어쓰지 않음
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
# 토스증권 OpenAPI (toss_api_docs.md 규격)
# ════════════════════════════════════════════════════════════
class TossError(Exception):
    def __init__(self, status: int, code: str = "", message: str = ""):
        self.status, self.code, self.message = status, code, message
        super().__init__(f"HTTP {status} {code} {message}".strip())


def read_body(resp) -> str:
    """응답 본문 읽기. 토스는 오류 응답을 gzip으로 압축해서 보내므로 풀어서 읽음."""
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
    """실행 1회 동안 토큰·계좌번호를 재사용 (요청 한도 방지)."""

    def __init__(self):
        if not TOSS_CLIENT_ID or not TOSS_CLIENT_SECRET:
            raise RuntimeError("TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 환경변수가 없습니다")
        res = toss_request("POST", "/oauth2/token", form={
            "grant_type": "client_credentials",
            "client_id": TOSS_CLIENT_ID,
            "client_secret": TOSS_CLIENT_SECRET,
        })
        self.token = res["access_token"]
        self.account_seq = toss_request("GET", "/api/v1/accounts", self.token)["result"][0]["accountSeq"]

    def buying_power_usd(self) -> float:
        res = toss_request("GET", "/api/v1/buying-power?currency=USD", self.token, self.account_seq)
        return float(res["result"]["cashBuyingPower"])

    def last_price(self, symbol: str) -> float:
        res = toss_request("GET", f"/api/v1/prices?symbols={urllib.parse.quote(symbol)}", self.token)
        return float(res["result"][0]["lastPrice"])

    def holding(self, symbol: str) -> tuple[float, float]:
        """(보유 수량, 평균 매수가). 보유 목록에 없으면 (0, 0)."""
        res = toss_request("GET", f"/api/v1/holdings?symbol={urllib.parse.quote(symbol)}",
                           self.token, self.account_seq)
        for item in (res.get("result") or {}).get("items") or []:
            if item.get("symbol") == symbol:
                return float(item.get("quantity") or 0), float(item.get("averagePurchasePrice") or 0)
        return 0.0, 0.0

    def market_buy(self, symbol: str, quantity: int) -> str:
        # 주문은 429여도 자동 재시도하지 않음 (중복 주문 방지)
        res = toss_request("POST", "/api/v1/orders", self.token, self.account_seq, body={
            "symbol": symbol, "side": "BUY", "orderType": "MARKET", "quantity": str(int(quantity)),
        }, retry_429=False)
        return res["result"]["orderId"]

    # 아래 쓰기 함수들은 --dry-run 이면 호출하지 않고 로그만 남김
    def market_sell(self, symbol: str, quantity: int) -> str:
        if DRY_RUN:
            log(f"(dry-run) 시장가 매도 생략: {symbol} {quantity}주")
            return "DRY-RUN"
        res = toss_request("POST", "/api/v1/orders", self.token, self.account_seq, body={
            "symbol": symbol, "side": "SELL", "orderType": "MARKET", "quantity": str(int(quantity)),
        }, retry_429=False)
        return res["result"]["orderId"]

    # ── 조건주문 (toss_api_docs.md 11번) ──
    def place_sell_trigger(self, symbol: str, quantity: int, trigger_price: float) -> str:
        """SINGLE 시장가 조건주문: 트리거 가격 도달 시 전량 시장가 매도."""
        if DRY_RUN:
            log(f"(dry-run) 조건주문 등록 생략: {symbol} {quantity}주 @ ${trigger_price:.2f}")
            return "DRY-RUN"
        expire = (datetime.now(NY) + timedelta(days=COND_EXPIRE_DAYS)).strftime("%Y-%m-%d")
        res = toss_request("POST", "/api/v1/conditional-orders", self.token, self.account_seq, body={
            "symbol": symbol, "type": "SINGLE", "quantity": str(int(quantity)), "orderType": "MARKET",
            "expireDate": expire, "first": {"orderSide": "SELL", "triggerPrice": f"{trigger_price:.2f}"},
        }, retry_429=False)
        return res["result"]["conditionalOrderId"]

    def conditional_active(self, cond_id: str | None) -> bool:
        """200 → 활성 / not-found → 체결·취소·만료됨. 그 밖의 오류는 예외 (판단 보류)."""
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
        """조건주문 취소. 이미 끝난 주문(not-found)은 조용히 넘어감."""
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
    if not (assume_open or is_market_open()):
        log(f"{symbol}: 매수 대기 중 (미국 정규장 아님, 뉴욕 {datetime.now(NY):%m-%d %H:%M})")
        return

    toss = toss_getter()
    budget = float(pos.get("budget") or 0)
    available = toss.buying_power_usd()
    spend = min(budget, available)
    price = toss.last_price(symbol)
    qty = math.floor(spend / price) if price > 0 else 0
    log(f"{symbol}: 승인 ${budget:,.2f} / 예수금 ${available:,.2f} → 사용 ${spend:,.2f}, "
        f"현재가 ${price:,.2f} → {qty}주")

    if qty < 1:
        reason = (f"달러 예수금(${available:,.2f})이 부족해요. 토스 앱에서 달러로 환전해주세요."
                  if available < budget else
                  f"승인 금액 ${budget:,.2f}로는 1주(${price:,.2f})도 살 수 없어요.")
        notify(f"⚠️ {symbol} 매수 불가\n{reason}\n승인 기록을 지웠어요. 필요하면 다시 승인해주세요.")
        remove_position(symbol)
        return

    # 주문 직전에 최신 상태 재확인 — 그 사이 텔레그램에서 승인 취소했으면 주문하지 않음
    latest = load_positions().get(symbol)
    if not latest or latest.get("status") != "APPROVED":
        log(f"{symbol}: 주문 직전 확인 결과 승인이 취소됨 → 주문 안 함")
        return

    qty_before, avg_before = toss.holding(symbol)   # 체결 판정 기준 (기존 보유분과 구분)
    base = {**latest, "account_seq": toss.account_seq, "quantity_ordered": qty, "ref_price": price,
            "holding_before": qty_before, "avg_before": avg_before, "ordered_at": utc_now_iso()}

    if DRY_RUN:
        log(f"(dry-run) 시장가 매수 생략: {symbol} {qty}주 (기존 보유 {qty_before:g}주)")
        return

    try:
        order_id = toss.market_buy(symbol, qty)
    except TossError as e:
        if e.code == "insufficient-buying-power":
            notify(f"⚠️ {symbol} 매수 불가\n달러 예수금이 부족해요 (예수금 ${available:,.2f}). "
                   f"토스 앱에서 달러로 환전해주세요.\n승인 기록을 지웠어요.")
            remove_position(symbol)
            return
        # 그 밖의 거절(장 휴일·일시 오류 등) → 주문은 안 들어감. APPROVED 유지, 같은 오류는 하루 1번만 알림
        err_key = f"{e.code or e.status}:{datetime.now(NY):%Y-%m-%d}"
        log(f"{symbol}: 주문 거절 {e} → APPROVED 유지, 다음 실행에 재시도")
        if latest.get("last_order_error") != err_key:
            notify(f"⚠️ {symbol} 매수 주문 실패\n{e.message or e.code or e.status}\n"
                   f"다음 확인 때 다시 시도할게요.")
            update_position(symbol, {**latest, "last_order_error": err_key})
        return
    except Exception as e:
        # 응답을 못 받음 → 주문이 들어갔는지 알 수 없음. 다시 주문하면 중복 매수 위험 → PENDING(주문번호 없음)으로 두고 체결 여부만 확인
        log(f"{symbol}: 주문 응답 없음 ({type(e).__name__}: {e}) → 체결 여부로 확인")
        update_position(symbol, {**base, "status": "PENDING", "order_id": None, "order_unknown": True})
        notify(f"⚠️ {symbol} 매수 주문 결과를 확인하지 못했어요 ({qty}주).\n"
               f"체결되면 알려드릴게요. 토스 앱에서도 확인해주세요.")
        return

    update_position(symbol, {**base, "status": "PENDING", "order_id": order_id})
    notify(f"🛒 {symbol} 시장가 매수 주문 접수\n{qty}주 × 약 ${price:,.2f} = 약 ${qty * price:,.2f}\n"
           f"체결되면 다시 알려드릴게요.")
    log(f"{symbol}: 주문 접수 orderId={order_id} {qty}주")


# ════════════════════════════════════════════════════════════
# PENDING → 체결 확인
# ════════════════════════════════════════════════════════════
def handle_pending(toss_getter, symbol: str, pos: dict, assume_open: bool = False) -> None:
    toss = toss_getter()
    qty_now, avg_now = toss.holding(symbol)
    qty_before = float(pos.get("holding_before") or 0)
    avg_before = float(pos.get("avg_before") or 0)
    ordered = float(pos.get("quantity_ordered") or 0)
    filled = qty_now - qty_before

    if filled <= 0 or (ordered and filled < ordered):
        age = minutes_since(pos.get("ordered_at"))
        log(f"{symbol}: 아직 체결 전 (보유 {qty_now:g}주, 주문 전 {qty_before:g}주, 주문 {ordered:g}주, {age:.0f}분 경과)")
        if pos.get("order_unknown") and filled <= 0 and age >= UNKNOWN_ORDER_GIVEUP_MIN:
            notify(f"⚠️ {symbol}: 주문 후 {age:.0f}분 동안 체결이 확인되지 않아 기록을 정리했어요.\n"
                   f"토스 앱에서 주문 내역을 확인해주세요.")
            remove_position(symbol)
        elif age >= PENDING_STALE_MIN and not pos.get("stale_notified"):
            notify(f"⏳ {symbol}: 주문 후 {age:.0f}분이 지났는데 아직 체결이 확인되지 않아요.\n"
                   f"토스 앱에서 주문 상태를 확인해주세요.")
            update_position(symbol, {**pos, "stale_notified": True})
        return

    # 이번 주문으로 늘어난 수량의 평균 단가 (기존 보유분이 있었으면 그만큼 제외)
    if qty_before > 0 and avg_now > 0 and avg_before > 0:
        buy_price = (avg_now * qty_now - avg_before * qty_before) / filled
    else:
        buy_price = avg_now or float(pos.get("ref_price") or 0)

    opened = {**pos, "status": "OPEN", "buy_price": round(buy_price, 4), "quantity": int(filled),
              "filled_at": utc_now_iso(), "cond_id_stop": None, "cond_id_profit": None}
    update_position(symbol, opened)
    log(f"{symbol}: 체결 {filled:g}주 @ ${buy_price:,.4f} → OPEN")

    fill_line = f"🔔 {symbol} 체결 완료!\n매수가: ${buy_price:,.2f} × {filled:g}주 = ${buy_price * filled:,.2f}"
    try:
        # 손절·익절 조건주문 2개 등록 (장외라면 다음 정규장에 자동 등록)
        manage_held(toss_getter, symbol, opened, assume_open, fill_line=fill_line)
    except Exception as e:
        log(f"{symbol}: 체결 직후 조건주문 처리 오류 {type(e).__name__}: {e}")
        notify(f"{fill_line}\n⚠️ 손절·익절 조건주문 등록 중 오류가 나서, 다음 확인(1분 뒤) 때 다시 시도할게요.")


# ════════════════════════════════════════════════════════════
# OPEN / BREAKEVEN → 손절·익절 조건주문 관리
# ════════════════════════════════════════════════════════════
def trigger_prices(buy_price: float, status: str) -> tuple[float, float]:
    """(손절가, 익절가). 손절은 -3%에서 수수료만큼 당김(-2.8%), BREAKEVEN이면 본전(+0.2%). 익절은 +5.2%."""
    if status == "BREAKEVEN":
        stop = buy_price * (1 + FEE_BUFFER)
    else:
        stop = buy_price * (1 + STOP_LOSS_PCT + FEE_BUFFER)
    return round(stop, 2), round(buy_price * (1 + TAKE_PROFIT_PCT + FEE_BUFFER), 2)


def pct(price: float, base: float) -> str:
    return f"{(price / base - 1) * 100:+.1f}%" if base else "?"


def today_key(tag: str) -> str:
    return f"{tag}:{datetime.now(NY):%Y-%m-%d}"


def manage_held(toss_getter, symbol: str, pos: dict, assume_open: bool, fill_line: str | None = None) -> None:
    toss = toss_getter()
    buy = float(pos["buy_price"])

    # ① 보유 수량 확인 — 이번 매수분(주문 전부터 갖고 있던 수량은 제외)이 0이면 포지션 종료
    qty_now, _ = toss.holding(symbol)
    mine = qty_now - float(pos.get("holding_before") or 0)
    qty = int(min(float(pos.get("quantity") or 0), mine)) if mine > 0 else 0
    if qty < 1:
        if not pos.get("zero_seen"):   # 일시적인 조회 이상일 수 있어 1번 더 확인
            log(f"{symbol}: 보유 수량 0 감지 → 다음 실행에서 한 번 더 확인 후 종료 처리")
            update_position(symbol, {**pos, "zero_seen": True})
            return
        close_position(toss, symbol, pos)
        return
    if pos.get("zero_seen"):
        pos = {**pos, "zero_seen": False}
        update_position(symbol, pos)

    # 긴급 매도 주문을 넣은 직후면 체결(보유 0)될 때까지 대기
    if pos.get("sell_order_id"):
        age = minutes_since(pos.get("sell_ordered_at"))
        if age < 5:
            log(f"{symbol}: 긴급 매도 체결 대기 중 ({age:.0f}분 경과)")
            return
        notify(f"⚠️ {symbol}: 긴급 매도 주문 후 {age:.0f}분이 지났는데 아직 보유 중이에요 ({qty}주).\n"
               f"토스 앱에서 주문 상태를 확인해주세요. 자동 감시는 계속할게요.")
        pos = {k: v for k, v in pos.items() if k not in ("sell_order_id", "sell_ordered_at")}
        update_position(symbol, pos)

    # 일부만 매도된 경우 → 수량이 맞지 않는 조건주문은 취소하고 새 수량으로 다시 등록
    if qty < int(pos.get("quantity") or 0):
        log(f"{symbol}: 보유 수량 감소 {pos.get('quantity')} → {qty}주, 조건주문을 새 수량으로 다시 등록")
        toss.cancel_conditional(pos.get("cond_id_stop"))
        toss.cancel_conditional(pos.get("cond_id_profit"))
        pos = {**pos, "quantity": qty, "cond_id_stop": None, "cond_id_profit": None}
        update_position(symbol, pos)

    # ② 정규장이 아니면 조건주문 전부 취소 (급변 오체결 방지, 장이 열리면 ③에서 재등록)
    if not (assume_open or is_market_open()):
        if pos.get("cond_id_stop") or pos.get("cond_id_profit"):
            toss.cancel_conditional(pos.get("cond_id_stop"))
            pos = {**pos, "cond_id_stop": None}
            update_position(symbol, pos)
            toss.cancel_conditional(pos.get("cond_id_profit"))
            pos = {**pos, "cond_id_profit": None}
            update_position(symbol, pos)
            log(f"{symbol}: 정규장 아님 → 조건주문 2개 취소 (장 시작 후 재등록)")
        else:
            log(f"{symbol}: 정규장 아님 → 조건주문 없이 대기 ({qty}주 보유)")
        if fill_line:
            notify(f"{fill_line}\n정규장이 아니라서 손절·익절 조건주문은 장이 열리면 등록할게요.")
        return

    price = toss.last_price(symbol)
    notes: list[str] = []

    # ③ +3% 달성 → 손절을 본전(매수가 +0.2%)으로 이동, BREAKEVEN
    moved_to_be = False
    if pos["status"] == "OPEN" and price >= buy * (1 + BREAKEVEN_PCT):
        toss.cancel_conditional(pos.get("cond_id_stop"))
        pos = {**pos, "status": "BREAKEVEN", "cond_id_stop": None, "breakeven_at": utc_now_iso()}
        update_position(symbol, pos)
        moved_to_be = True
        log(f"{symbol}: 현재가 ${price:,.2f} ({pct(price, buy)}) → BREAKEVEN")

    stop_p, profit_p = trigger_prices(buy, pos["status"])
    stop_label = "손절(본전)" if pos["status"] == "BREAKEVEN" else "손절"

    # ④ 조건주문이 살아있는지 확인
    stop_ok = toss.conditional_active(pos.get("cond_id_stop"))
    profit_ok = toss.conditional_active(pos.get("cond_id_profit"))

    # ⑤ 재등록 전에 현재가가 이미 트리거를 넘었는지 확인 → 넘었으면 즉시 시장가 매도
    if not stop_ok and price <= stop_p:
        emergency_sell(toss, symbol, pos, qty,
                       f"{stop_label}가 ${stop_p:,.2f}를 등록하려는데 현재가가 이미 ${price:,.2f}였어요.")
        return
    if not profit_ok and price >= profit_p:
        emergency_sell(toss, symbol, pos, qty,
                       f"익절가 ${profit_p:,.2f}를 등록하려는데 현재가가 이미 ${price:,.2f}였어요.")
        return

    # ⑥ 없어진 조건주문 등록
    errors: list[str] = []
    for key, label, trig, ok in (("cond_id_stop", stop_label, stop_p, stop_ok),
                                  ("cond_id_profit", "익절", profit_p, profit_ok)):
        if ok:
            continue
        try:
            pos = {**pos, key: toss.place_sell_trigger(symbol, qty, trig)}
            update_position(symbol, pos)
            notes.append(f"{'🔴' if key == 'cond_id_stop' else '🟢'} {label}: ${trig:,.2f} ({pct(trig, buy)}, 수수료 반영)")
            log(f"{symbol}: {label} 조건주문 등록 {qty}주 @ ${trig:,.2f} id={pos[key]}")
        except TossError as e:
            errors.append(f"{label} 등록 실패: {e.message or e.code or e.status}")
            log(f"{symbol}: {label} 조건주문 등록 실패 {e}")

    # ⑦ 알림
    err_text = ("\n⚠️ " + " / ".join(errors) + "\n다음 확인(1분 뒤) 때 다시 시도할게요.") if errors else ""
    if fill_line:
        notify(fill_line + ("\n" + "\n".join(notes) if notes else "")
               + ("\n📈 +3% 달성 시 손절 → 본전(매수가 +0.2%)으로 이동" if pos["status"] == "OPEN" else "")
               + err_text)
    elif moved_to_be:
        notify(f"📈 {symbol} +3% 달성! (현재가 ${price:,.2f}, {pct(price, buy)})\n"
               f"손절 기준을 ${stop_p:,.2f}(매수가 +0.2%, 수수료 상쇄)로 옮겼어요."
               + ("\n" + "\n".join(n for n in notes if "익절" in n) if any("익절" in n for n in notes) else "")
               + err_text)
    elif notes or errors:
        # 매일 장 시작 때 재등록 알림. 같은 등록 실패는 하루 1번만 알림
        if notes:
            notify(f"🔄 {symbol} 조건주문 등록 ({qty}주, 매수가 ${buy:,.2f})\n" + "\n".join(notes) + err_text)
        elif pos.get("last_cond_error") != today_key("cond"):
            notify(f"⚠️ {symbol} 손절·익절 조건주문 등록 실패{err_text}")
            pos = {**pos, "last_cond_error": today_key("cond")}
            update_position(symbol, pos)
    else:
        log(f"{symbol}: {pos['status']} {qty}주, 현재가 ${price:,.2f} ({pct(price, buy)}), "
            f"{stop_label} ${stop_p:,.2f} / 익절 ${profit_p:,.2f} 조건주문 유지 중")


def emergency_sell(toss, symbol: str, pos: dict, qty: int, reason: str) -> None:
    """조건주문을 (재)등록하려는 시점에 이미 트리거를 넘어선 경우 → 조건주문 대신 즉시 시장가 매도."""
    try:
        toss.cancel_conditional(pos.get("cond_id_stop"))
        toss.cancel_conditional(pos.get("cond_id_profit"))
        pos = {**pos, "cond_id_stop": None, "cond_id_profit": None}
        order_id = toss.market_sell(symbol, qty)
    except Exception as e:
        log(f"{symbol}: 긴급 매도 실패 {type(e).__name__}: {e}")
        if pos.get("last_sell_error") != today_key("sell"):
            notify(f"⚠️ {symbol} 긴급 시장가 매도 실패\n{reason}\n오류: {e}\n"
                   f"1분마다 다시 시도할게요. 토스 앱에서도 직접 확인해주세요.")
            pos = {**pos, "last_sell_error": today_key("sell")}
        update_position(symbol, pos)
        return
    # 체결(보유 0)이 확인되면 close_position에서 포지션 삭제
    update_position(symbol, {**pos, "sell_order_id": order_id, "sell_ordered_at": utc_now_iso(),
                             "sell_reason": reason})
    notify(f"🚨 {symbol} 긴급 시장가 매도 ({qty}주)\n{reason}\n"
           f"조건주문을 다시 걸면 방향이 반대로 등록될 위험이 있어, 조건주문 대신 즉시 시장가로 매도했어요.\n"
           f"체결되면 다시 알려드릴게요.")
    log(f"{symbol}: 긴급 시장가 매도 {qty}주 orderId={order_id}")


def close_position(toss, symbol: str, pos: dict) -> None:
    """보유 수량 0 (조건주문 체결 등) → 남은 조건주문 정리 + 포지션 삭제 + 알림."""
    leftover = []
    for key in ("cond_id_stop", "cond_id_profit"):
        try:
            toss.cancel_conditional(pos.get(key))
        except Exception as e:
            leftover.append(str(pos.get(key)))
            log(f"{symbol}: 남은 조건주문 {pos.get(key)} 취소 실패 {e}")
    buy = float(pos.get("buy_price") or 0)
    try:
        price = toss.last_price(symbol)
        price_line = f"\n매수가 ${buy:,.2f} → 현재가 약 ${price:,.2f} ({pct(price, buy)})"
    except Exception:
        price_line = f"\n매수가 ${buy:,.2f}"
    remove_position(symbol)
    how = "긴급 시장가 매도가 체결됐어요." if pos.get("sell_order_id") else \
          "보유 수량이 0이 됐어요 (토스 조건주문 체결 또는 직접 매도)."
    notify(f"📊 {symbol} 포지션 종료\n{how}{price_line}\n"
           + (f"⚠️ 조건주문 {', '.join(leftover)} 취소에 실패했어요. 토스 앱에서 직접 취소해주세요.\n" if leftover else "")
           + "토스 앱에서 체결 내역을 확인해주세요.")
    log(f"{symbol}: 포지션 종료 → SSM에서 삭제")


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

    positions = load_positions()
    if not positions:
        log("포지션 없음 → 종료")
        return
    log(f"포지션 {len(positions)}개: " + ", ".join(f"{s}[{p.get('status')}]" for s, p in positions.items())
        + (" (dry-run)" if DRY_RUN else ""))

    toss_holder: dict = {}

    def toss_getter() -> Toss:                    # 토스가 필요할 때만 로그인 (장 마감 중엔 호출 안 함)
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
