"""
bot_kr.py  (국내주식 — 미국용 bot.py와 완전히 별개, 별도 텔레그램 봇)
──────────────────────────────────────────────────────────────
국장 시그널 메시지의 승인/패스 버튼 처리 봇 (getUpdates long-polling)

  ✅ 승인  (buy:종목코드)     → 원화 예수금 표시 + '매수 금액을 채팅창에 입력해주세요' + ❌ 취소
                                 금액 입력 → SSM /scanner-kr/positions 에 {코드: APPROVED, budget(원)} 저장
  ❌ (승인) 취소 (cancel_buy:코드) → 대기 해제 / SSM 삭제 / PENDING이면 토스 주문 취소
  ⏭ 패스  (pass:코드)         → '⏭ 패스했어요'

금액 입력 방법 (원 단위, '만'·'억' 사용 가능)
  · 대기 종목이 1개     : 1000000  /  1,000,000  /  100만  /  100만원  /  1.5억
  · 여러 종목을 한 번에 : 삼성전자 100만, 000660 50만   (쉼표+띄어쓰기·세미콜론·줄바꿈으로 구분)
  · 특정 종목 하나만    : 그 종목 메시지에 답장(reply)으로 금액 입력  또는  '삼성전자 100만'

환경변수
  TELEGRAM_TOKEN_KR (필수) 국장 전용 봇 토큰 — 미국 bot.py의 TELEGRAM_TOKEN과 반드시 달라야 함
                    (같은 토큰으로 두 봇이 getUpdates를 하면 서로 메시지를 빼앗아 미국 봇이 망가짐)
  TELEGRAM_CHAT_ID  (필수) 이 Chat ID에서 온 버튼/메시지만 처리
  TOSS_CLIENT_ID, TOSS_CLIENT_SECRET, AWS_REGION(선택)

실행 : python bot_kr.py
"""

from __future__ import annotations

import gzip
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN_KR", "").strip()
US_TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()   # 충돌 방지 확인용으로만 읽음
ALLOWED_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
SSM_KEY = "/scanner-kr/positions"
SIGNALS_PATH = "/scanner-kr/signals"
HELD_STATUSES = ("OPEN", "BREAKEVEN")

TOSS_CLIENT_ID = os.environ.get("TOSS_CLIENT_ID", "").strip()
TOSS_CLIENT_SECRET = os.environ.get("TOSS_CLIENT_SECRET", "").strip()
TOSS_BASE = "https://openapi.tossinvest.com"

SEP = "\n\n━━━━━━━━━━\n"

CODE_RE = re.compile(r"^[0-9][0-9A-Z]{5}$")
NAME_IN_TEXT_RE = re.compile(r"시그널:\s*(.+?)\s*\(([0-9][0-9A-Z]{5})\)")
# 항목 구분: 줄바꿈·세미콜론, 뒤에 띄어쓰기가 오는 쉼표, 뒤에 글자가 오는 쉼표. '1,000,000' 같은 천 단위 쉼표는 자르지 않음
ITEM_SPLIT_RE = re.compile(r"[;\n]+|,\s+|,(?=[가-힣A-Za-z])")
AMOUNT_PART = r"₩?\s*\d[\d,]*(?:\.\d+)?\s*(?:억|만|천)?\s*원?"
AMOUNT_RE = re.compile(rf"^\s*({AMOUNT_PART})\s*$")
ITEM_RE = re.compile(rf"^(?P<key>.+?)\s*[:=]?\s*(?P<amt>{AMOUNT_PART})\s*$")

ssm = boto3.client("ssm", region_name=AWS_REGION)

# 금액 입력 대기: chat_id → { message_id → {"symbol", "name", "base", "buying_power"} }
awaiting_amount: dict[int, dict[int, dict]] = {}


def won(x: float) -> str:
    return f"{x:,.0f}원"


def label(symbol: str, name: str | None) -> str:
    return f"{name}({symbol})" if name else symbol


# ════════════════════════════════════════════════════════════
# 텔레그램 API
# ════════════════════════════════════════════════════════════
def tg_call(method: str, payload: dict, timeout: int = 15) -> dict | None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        if "message is not modified" not in detail:
            print(f"[텔레그램] {method} HTTP {e.code}: {detail}")
    except Exception as e:
        print(f"[텔레그램] {method} 오류: {type(e).__name__}: {e}")
    return None


def get_updates(offset: int | None) -> list[dict]:
    payload = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        payload["offset"] = offset
    res = tg_call("getUpdates", payload, timeout=40)
    return res.get("result", []) if res and res.get("ok") else []


def answer_callback(callback_id: str, text: str = "") -> None:
    tg_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


def send_message(chat_id: int, text: str) -> None:
    tg_call("sendMessage", {"chat_id": chat_id, "text": text})


def edit_message(chat_id: int, message_id: int, text: str, buttons: list | None = None) -> None:
    tg_call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text,
                                "reply_markup": {"inline_keyboard": buttons or []}})


def base_text(message: dict) -> str:
    return (message.get("text") or "").split(SEP, 1)[0]


def name_from_text(text: str, symbol: str) -> str:
    m = NAME_IN_TEXT_RE.search(text or "")
    return m.group(1) if m and m.group(2) == symbol else ""


def decision_buttons(symbol: str) -> list:
    return [[
        {"text": "✅ 승인", "callback_data": f"buy:{symbol}"},
        {"text": "⏭ 패스", "callback_data": f"pass:{symbol}"},
    ]]


# ════════════════════════════════════════════════════════════
# SSM Parameter Store (/scanner-kr/positions — 미국용 /scanner/positions와 별개)
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


def committed_budget(positions: dict, exclude: str | None = None) -> float:
    return sum(float(p.get("budget") or 0) for s, p in positions.items()
               if p.get("status") == "APPROVED" and s != exclude)


# ════════════════════════════════════════════════════════════
# 토스증권 OpenAPI (toss_api_docs.md 규격 + 서버에서 확인한 국내주식 형식)
#   종목코드: 6자리('005930'), 원화 예수금: buying-power?currency=KRW
# ════════════════════════════════════════════════════════════
class TossError(Exception):
    def __init__(self, status: int, code: str = "", message: str = ""):
        self.status, self.code = status, code
        super().__init__(f"HTTP {status} {code} {message}".strip())


def read_body(resp) -> str:
    raw = resp.read()
    if resp.headers.get("Content-Encoding", "").lower() == "gzip" or raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    return raw.decode("utf-8", errors="replace")


def _toss_request_once(method: str, path: str, token: str | None, account_seq, form: dict | None) -> dict:
    headers, data = {}, None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if account_seq is not None:
        headers["X-Tossinvest-Account"] = str(account_seq)
    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(TOSS_BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = read_body(r)
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as e:
        raw = read_body(e)
        try:
            err = json.loads(raw).get("error", {})
        except Exception:
            err = {"message": raw[:200]}
        raise TossError(e.code, err.get("code", ""), err.get("message", "")) from None


def toss_request(method: str, path: str, token: str | None = None, account_seq=None,
                 form: dict | None = None) -> dict:
    for attempt in range(2):
        try:
            return _toss_request_once(method, path, token, account_seq, form)
        except TossError as e:
            if e.status == 429 and attempt == 0:
                time.sleep(2)
                continue
            raise


TOKEN_TTL_SEC = 600
_toss_cache: dict = {"token": None, "token_at": 0.0, "account_seq": None}


def toss_token(force: bool = False) -> str:
    if not TOSS_CLIENT_ID or not TOSS_CLIENT_SECRET:
        raise RuntimeError("TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 환경변수가 없습니다")
    if not force and _toss_cache["token"] and time.time() - _toss_cache["token_at"] < TOKEN_TTL_SEC:
        return _toss_cache["token"]
    res = toss_request("POST", "/oauth2/token", form={
        "grant_type": "client_credentials", "client_id": TOSS_CLIENT_ID, "client_secret": TOSS_CLIENT_SECRET,
    })
    _toss_cache.update(token=res["access_token"], token_at=time.time())
    return _toss_cache["token"]


def toss_account_seq(token: str):
    if _toss_cache["account_seq"] is None:
        _toss_cache["account_seq"] = toss_request("GET", "/api/v1/accounts", token)["result"][0]["accountSeq"]
    return _toss_cache["account_seq"]


def toss_call(method: str, path: str, account: bool = True, account_seq=None) -> dict:
    for attempt in range(2):
        token = toss_token(force=attempt > 0)
        seq = None
        if account:
            seq = account_seq if account_seq is not None else toss_account_seq(token)
        try:
            return toss_request(method, path, token, seq)
        except TossError as e:
            if e.status == 401 and attempt == 0:
                continue
            raise


def toss_buying_power_krw() -> float:
    res = toss_call("GET", "/api/v1/buying-power?currency=KRW")
    return float(res["result"]["cashBuyingPower"])


def toss_cancel_order(order_id: str, account_seq=None) -> None:
    toss_call("DELETE", f"/api/v1/orders/{order_id}", account_seq=account_seq)


def toss_holding_quantity(symbol: str, account_seq=None) -> float:
    res = toss_call("GET", f"/api/v1/holdings?symbol={urllib.parse.quote(symbol)}", account_seq=account_seq)
    for item in (res.get("result") or {}).get("items") or []:
        if item.get("symbol") == symbol:
            return float(item.get("quantity") or 0)
    return 0.0


# ════════════════════════════════════════════════════════════
# 버튼 처리
# ════════════════════════════════════════════════════════════
def release_waiting(chat_id: int, message_id: int) -> bool:
    waiting = awaiting_amount.get(chat_id, {})
    was_waiting = waiting.pop(message_id, None) is not None
    if not waiting:
        awaiting_amount.pop(chat_id, None)
    return was_waiting


def handle_buy(chat_id: int, message: dict, symbol: str) -> str:
    message_id = message["message_id"]
    base = base_text(message)
    name = name_from_text(base, symbol)

    try:
        buying_power = toss_buying_power_krw()
    except Exception as e:
        print(f"[토스] 원화 예수금 조회 실패 ({symbol}): {type(e).__name__}: {e}")
        release_waiting(chat_id, message_id)
        edit_message(chat_id, message_id, base + SEP + "⚠️ 예수금 조회 실패, 잠시 후 다시 시도해주세요",
                     buttons=decision_buttons(symbol))
        return "예수금 조회 실패"

    lines = [f"💰 원화 예수금: {won(buying_power)}"]
    try:
        committed = committed_budget(load_positions(), exclude=symbol)
        if committed > 0:
            lines.append(f"(이미 승인된 금액 {won(committed)} 제외 → 사용 가능 {won(max(buying_power - committed, 0))})")
    except Exception as e:
        print(f"[SSM] 승인 금액 합계 조회 실패: {e}")
    lines += ["매수 금액을 채팅창에 입력해주세요",
              "(예: 1000000  또는  100만 / 여러 종목이면 '삼성전자 100만, 000660 50만')"]

    awaiting_amount.setdefault(chat_id, {})[message_id] = {
        "symbol": symbol, "name": name, "base": base, "buying_power": buying_power,
    }
    edit_message(chat_id, message_id, base + SEP + "\n".join(lines),
                 buttons=[[{"text": "❌ 취소", "callback_data": f"cancel_buy:{symbol}"}]])
    return "금액을 입력해주세요"


def handle_pass(chat_id: int, message: dict, symbol: str) -> str:
    release_waiting(chat_id, message["message_id"])
    edit_message(chat_id, message["message_id"], base_text(message) + SEP + "⏭ 패스했어요")
    return "패스했어요"


def handle_cancel_buy(chat_id: int, message: dict, symbol: str) -> str:
    message_id = message["message_id"]
    if release_waiting(chat_id, message_id):
        note = "금액 입력을 취소했어요"
    else:
        positions = load_positions()
        pos = positions.get(symbol)
        lbl = label(symbol, (pos or {}).get("name"))
        if pos and pos.get("status") in HELD_STATUSES:
            send_message(chat_id, f"⚠️ {lbl}은(는) 이미 매수가 체결돼 손절·익절 감시 중이라 취소할 수 없어요.\n"
                                  f"팔고 싶으면 토스 앱에서 직접 매도해주세요 (매도되면 자동으로 정리돼요).")
            return "이미 체결된 종목"
        if pos and pos.get("status") == "PENDING":
            ok, note = cancel_pending_order(chat_id, symbol, pos)
            if not ok:
                return note
        else:
            note = "승인을 취소했어요" if pos else "취소했어요"
        if pos:
            positions.pop(symbol)
            save_positions(positions)
    edit_message(chat_id, message_id, base_text(message), buttons=decision_buttons(symbol))
    return note


def cancel_pending_order(chat_id: int, symbol: str, pos: dict) -> tuple[bool, str]:
    order_id = pos.get("order_id")
    account_seq = pos.get("account_seq")
    lbl = label(symbol, pos.get("name"))
    if not order_id:
        send_message(chat_id, f"⚠️ {lbl}: 주문 번호가 저장돼 있지 않아 자동 취소할 수 없어요.\n"
                              f"토스 앱에서 직접 확인·취소해주세요.")
        return False, "주문 번호 없음"
    try:
        toss_cancel_order(order_id, account_seq)
        print(f"[토스] {symbol} 주문 {order_id} 취소")
        send_message(chat_id, f"↩ {lbl} 매수 주문을 취소했어요.")
        return True, "주문을 취소했어요"
    except TossError as e:
        if "not-found" not in e.code:
            print(f"[토스] {symbol} 주문 취소 실패: {e}")
            send_message(chat_id, f"⚠️ {lbl} 주문 취소 실패 ({e}).\n잠시 후 다시 눌러주세요.")
            return False, "주문 취소 실패"
    except Exception as e:
        print(f"[토스] {symbol} 주문 취소 실패: {type(e).__name__}: {e}")
        send_message(chat_id, f"⚠️ {lbl} 주문 취소 실패 ({type(e).__name__}).\n잠시 후 다시 눌러주세요.")
        return False, "주문 취소 실패"

    try:
        qty = toss_holding_quantity(symbol, account_seq)
    except Exception as e:
        send_message(chat_id, f"⚠️ {lbl} 주문을 찾을 수 없는데, 체결 여부 확인도 실패했어요 ({e}).\n"
                              f"토스 앱에서 확인해주세요.")
        return False, "확인 실패"
    if qty > 0:
        send_message(chat_id, f"⚠️ {lbl}은(는) 이미 체결돼서 취소할 수 없어요 (보유 {qty:g}주).")
        return False, "이미 체결됨"
    send_message(chat_id, f"↩ {lbl} 주문은 이미 취소·만료된 상태라 기록만 정리했어요.")
    return True, "승인을 취소했어요"


# ════════════════════════════════════════════════════════════
# 금액 입력 처리 (원 단위)
# ════════════════════════════════════════════════════════════
UNITS = {"억": 100_000_000, "만": 10_000, "천": 1_000}


def parse_amount(text: str) -> float | None:
    """'1,000,000' / '100만' / '100만원' / '1.5억' → 원. 형식이 아니면 None."""
    m = AMOUNT_RE.match(text or "")
    if not m:
        return None
    s = m.group(1).replace(",", "").replace("원", "").replace("₩", "").replace(" ", "")
    mult = 1
    if s and s[-1] in UNITS:
        mult, s = UNITS[s[-1]], s[:-1]
    try:
        amount = float(s) * mult
    except ValueError:
        return None
    return math.floor(amount) if math.isfinite(amount) else None


def normalize_key(raw: str) -> str:
    return raw.strip().replace(" ", "").upper()


def parse_multi(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """'삼성전자 100만, 000660 50만' → ([('삼성전자', '100만'), ('000660', '50만')], 형식 오류)."""
    pairs, errors = [], []
    for item in ITEM_SPLIT_RE.split(text):
        item = item.strip()
        if not item:
            continue
        m = ITEM_RE.match(item)
        if not m or not m.group("key").strip(" :="):
            errors.append(f"'{item}' → '삼성전자 100만' 또는 '005930 1000000' 형식으로 입력해주세요")
            continue
        pairs.append((m.group("key").strip(" :="), m.group("amt")))
    return pairs, errors


def apply_amounts(chat_id: int, items: list[tuple[int, float]]) -> list[str]:
    waiting = awaiting_amount[chat_id]
    positions = load_positions()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    done, skipped = [], set()
    for message_id, amount in items:
        st = waiting[message_id]
        if (positions.get(st["symbol"]) or {}).get("status") in ("PENDING",) + HELD_STATUSES:
            skipped.add(message_id)
            continue
        positions[st["symbol"]] = {"symbol": st["symbol"], "name": st.get("name", ""), "status": "APPROVED",
                                   "budget": amount, "approved_at": now}
    if len(skipped) < len(items):
        save_positions(positions)

    for message_id, amount in items:
        state = waiting[message_id]
        symbol = state["symbol"]
        lbl = label(symbol, state.get("name"))
        release_waiting(chat_id, message_id)
        if message_id in skipped:
            edit_message(chat_id, message_id,
                         state["base"] + SEP + "⚠️ 이미 매수 주문·보유 중인 종목이라 새로 승인하지 않았어요.")
            done.append(f"⚠️ {lbl} 이미 보유/주문 중 → 건너뜀")
            continue
        edit_message(
            chat_id, message_id,
            state["base"] + SEP + f"✅ 승인 완료! 매수 금액: {won(amount)}\n"
                                  f"국내 정규장(09:00~15:20) 시간에 시장가로 매수해요.",
            buttons=[[{"text": "❌ 승인 취소", "callback_data": f"cancel_buy:{symbol}"}]],
        )
        print(f"[승인] {symbol} budget={won(amount)} → SSM {SSM_KEY} 저장")
        done.append(f"✅ {lbl} {won(amount)}")
    return done


def amount_error(amount: float | None) -> str | None:
    if amount is None:
        return "금액만 입력해주세요 (예: 1000000  또는  100만)"
    if amount <= 0:
        return "0보다 큰 금액을 입력해주세요"
    return None


def over_budget_error(state: dict, amount: float, positions: dict, batch_used: float = 0.0) -> str | None:
    buying_power = state.get("buying_power")
    if buying_power is None:
        return None
    available = buying_power - committed_budget(positions, exclude=state["symbol"]) - batch_used
    if amount > available + 1e-9:
        return f"예수금을 초과해요 (사용 가능 {won(max(available, 0))}). 다시 입력해주세요"
    return None


def handle_text(chat_id: int, msg: dict) -> None:
    waiting = awaiting_amount.get(chat_id)
    if not waiting:
        return
    text = msg.get("text", "")

    # ── ① 금액만 입력 ──
    single = parse_amount(text)
    if single is not None or not re.search(r"\d", text):
        reply_to = (msg.get("reply_to_message") or {}).get("message_id")
        if reply_to in waiting:
            target = reply_to
        elif len(waiting) == 1:
            target = next(iter(waiting))
        else:
            pending = ", ".join(label(s["symbol"], s.get("name")) for s in waiting.values())
            send_message(chat_id, f"⚠️ 여러 종목({pending})이 금액을 기다리고 있어요.\n"
                                  f"'삼성전자 100만, 000660 50만'처럼 종목과 금액을 함께 입력하거나,\n"
                                  f"그 종목 메시지에 답장(reply)으로 보내주세요.")
            return
        err = amount_error(single) or over_budget_error(waiting[target], single, load_positions())
        if err:
            send_message(chat_id, f"⚠️ {err}")
            return
        apply_amounts(chat_id, [(target, single)])
        return

    # ── ② 종목(코드 또는 이름) + 금액 ──
    pairs, errors = parse_multi(text)
    lookup = {}
    for mid, s in waiting.items():
        lookup[s["symbol"]] = mid
        if s.get("name"):
            lookup[normalize_key(s["name"])] = mid
    positions = load_positions()
    items, seen, batch_used = [], set(), 0.0
    for key, amount_text in pairs:
        mid = lookup.get(normalize_key(key)) or lookup.get(key.strip().zfill(6) if key.strip().isdigit() else "")
        if mid is None:
            errors.append(f"{key}: 금액을 기다리는 종목이 아니에요")
            continue
        state = waiting[mid]
        lbl = label(state["symbol"], state.get("name"))
        if mid in seen:
            errors.append(f"{lbl}: 두 번 입력됐어요 (처음 값만 사용)")
            continue
        amount = parse_amount(amount_text)
        err = amount_error(amount) or over_budget_error(state, amount, positions, batch_used)
        if err:
            errors.append(f"{lbl}: {err}")
            continue
        seen.add(mid)
        batch_used += amount
        items.append((mid, amount))

    done = apply_amounts(chat_id, items) if items else []
    lines = done + [f"⚠️ {e}" for e in errors]
    left = [label(s["symbol"], s.get("name")) for s in awaiting_amount.get(chat_id, {}).values()]
    if left:
        lines.append(f"⏳ 아직 금액 대기: {', '.join(left)}")
    if len(pairs) > 1 or errors:
        send_message(chat_id, "\n".join(lines))


# ════════════════════════════════════════════════════════════
# 시그널 전달 (GitHub Actions → SSM /scanner-kr/signals/ → 원화 예수금 붙여 전송)
# ════════════════════════════════════════════════════════════
SIGNAL_CHECK_SEC = 20
_last_signal_check = 0.0


def fetch_signal_params() -> list[dict]:
    params, token = [], None
    while True:
        kwargs = {"Path": SIGNALS_PATH, "Recursive": True, "MaxResults": 10}
        if token:
            kwargs["NextToken"] = token
        res = ssm.get_parameters_by_path(**kwargs)
        params += res.get("Parameters", [])
        token = res.get("NextToken")
        if not token:
            return sorted(params, key=lambda p: p["Name"])


def deliver_pending_signals(force: bool = False) -> None:
    global _last_signal_check
    if not force and time.time() - _last_signal_check < SIGNAL_CHECK_SEC:
        return
    _last_signal_check = time.time()
    try:
        params = fetch_signal_params()
    except Exception as e:
        print(f"[시그널] SSM 조회 실패: {type(e).__name__}: {e}")
        return
    if not params:
        return

    try:
        bp_line = f"💰 원화 예수금: {won(toss_buying_power_krw())}"
    except Exception as e:
        print(f"[토스] 시그널용 원화 예수금 조회 실패: {type(e).__name__}: {e}")
        bp_line = "💰 원화 예수금: 조회 실패 (승인 시 다시 조회해요)"

    for p in params:
        try:
            sig = json.loads(p["Value"])
            symbol, text = sig["symbol"], sig["text"]
        except Exception:
            print(f"[시그널] 형식 오류로 삭제: {p['Name']}")
            ssm.delete_parameter(Name=p["Name"])
            continue
        res = tg_call("sendMessage", {
            "chat_id": int(ALLOWED_CHAT_ID),
            "text": text + SEP + bp_line,
            "reply_markup": {"inline_keyboard": decision_buttons(symbol)},
        })
        if not (res and res.get("ok")):
            print(f"[시그널] {symbol} 전송 실패 → 다음 확인 때 다시 시도")
            break
        ssm.delete_parameter(Name=p["Name"])
        print(f"[시그널] {symbol} 전송 (원화 예수금 포함)")
        time.sleep(0.5)


# ════════════════════════════════════════════════════════════
# 메인 루프
# ════════════════════════════════════════════════════════════
HANDLERS = {"buy": handle_buy, "pass": handle_pass, "cancel_buy": handle_cancel_buy}


def is_allowed(chat_id) -> bool:
    return str(chat_id) == ALLOWED_CHAT_ID


def process_update(update: dict) -> None:
    if "callback_query" in update:
        cb = update["callback_query"]
        message = cb.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        if not is_allowed(chat_id):
            answer_callback(cb["id"], "권한이 없습니다")
            return
        action, _, symbol = (cb.get("data") or "").partition(":")
        handler = HANDLERS.get(action)
        if not handler or not CODE_RE.match(symbol):
            answer_callback(cb["id"])
            return
        try:
            note = handler(chat_id, message, symbol)
            answer_callback(cb["id"], note)
        except Exception as e:
            answer_callback(cb["id"], "오류가 발생했어요")
            send_message(chat_id, f"⚠️ {symbol} 처리 중 오류: {type(e).__name__}: {e}")
            raise
        print(f"[버튼] {action}:{symbol}")

    elif "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        if "text" in msg and is_allowed(chat_id):
            try:
                handle_text(chat_id, msg)
            except Exception as e:
                send_message(chat_id, f"⚠️ 금액 저장 중 오류(저장 안 됨): {type(e).__name__}: {e}")
                raise


def main() -> None:
    if not TELEGRAM_TOKEN or not ALLOWED_CHAT_ID:
        sys.exit("TELEGRAM_TOKEN_KR / TELEGRAM_CHAT_ID 환경변수를 설정하세요.")
    if US_TELEGRAM_TOKEN and TELEGRAM_TOKEN == US_TELEGRAM_TOKEN:
        sys.exit("TELEGRAM_TOKEN_KR이 미국 봇 토큰(TELEGRAM_TOKEN)과 같아요. "
                 "같은 토큰이면 미국 bot.py의 메시지 수신이 깨지므로 국장용 봇을 새로 만들어 주세요.")
    if not TOSS_CLIENT_ID or not TOSS_CLIENT_SECRET:
        print("[경고] TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 없음 → 승인 시 예수금 조회가 실패합니다")
    print(f"[국장] 봇 시작됨 (허용 Chat ID {ALLOWED_CHAT_ID}, SSM {SSM_KEY} @ {AWS_REGION})")
    offset = None
    while True:
        try:
            deliver_pending_signals()
            for update in get_updates(offset):
                offset = update["update_id"] + 1
                try:
                    process_update(update)
                except Exception as e:
                    print(f"[오류] update {update.get('update_id')}: {type(e).__name__}: {e}")
        except KeyboardInterrupt:
            print("봇 종료")
            break
        except Exception as e:
            print(f"[메인 루프 오류] {type(e).__name__}: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
