"""
bot.py
──────────────────────────────────────────────────────────────
텔레그램 승인/패스 버튼 처리 봇 (getUpdates long-polling)

scanner.py가 보낸 시그널 메시지의 버튼을 처리합니다. 여러 종목을 동시에 처리할 수 있습니다.
  ✅ 승인  (buy:종목)        → '매수 금액을 채팅창에 입력해주세요' + ❌ 취소 버튼
                                다음 채팅 메시지를 금액으로 받아 검증(숫자, 0보다 큼)
                                → SSM /scanner/positions 에 {종목: APPROVED, budget} 저장
                                → '✅ 승인 완료! 매수 금액: $XX' + ❌ 승인 취소 버튼
  ❌ (승인) 취소 (cancel_buy:종목)
                              → 금액 입력 대기 중이었으면 대기만 해제
                                이미 저장됐으면 SSM에서 그 종목 삭제
                                → 두 경우 모두 원래 '✅ 승인 / ⏭ 패스' 버튼으로 복원
  ⏭ 패스  (pass:종목)        → SSM 저장 없이 '⏭ 패스했어요'로 변경

금액 입력 방법
  · 대기 종목이 1개        : 90  /  $90.50  /  1,250
  · 여러 종목을 한 번에    : TEAM 100, NVDA 200   (쉼표·세미콜론·줄바꿈으로 구분, 'TEAM: 100'도 가능)
  · 특정 종목 하나만       : 그 종목 메시지에 답장(reply)으로 숫자 입력  또는  'NVDA 200'

환경변수
  TELEGRAM_TOKEN    (필수) 봇 토큰
  TELEGRAM_CHAT_ID  (필수) 이 Chat ID에서 온 버튼/메시지만 처리 (다른 사람 입력 차단)
  AWS_REGION        (선택) 기본 ap-northeast-2
  AWS 자격 증명은 코드에 두지 않습니다. EC2에서는 인스턴스 IAM 역할을 사용합니다.

필요 패키지 : pip install boto3
실행        : python bot.py
"""

from __future__ import annotations

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

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
ALLOWED_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
SSM_KEY = "/scanner/positions"

# 메시지 본문(시그널 정보)과 상태 문구를 구분하는 줄.
# 상태가 바뀌어도 위쪽 시그널 정보는 그대로 두고, 취소 시 원래 메시지로 복원하는 데 사용합니다.
SEP = "\n\n━━━━━━━━━━\n"

TICKER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]*$")
# 항목 구분: 줄바꿈·세미콜론, 또는 뒤에 종목명(영문)이 오는 쉼표. '$2,000' 같은 천 단위 쉼표는 자르지 않음
ITEM_SPLIT_RE = re.compile(r"[;\n]+|,(?=\s*[A-Za-z])")

ssm = boto3.client("ssm", region_name=AWS_REGION)

# 금액 입력 대기 상태: chat_id → { message_id(그 종목 시그널 메시지) → {"symbol", "base"} }
# 종목마다 message_id가 달라서 여러 종목을 동시에 대기시킬 수 있습니다.
# base = 원래 시그널 정보 텍스트 (금액 입력 후 메시지를 다시 그릴 때 사용)
awaiting_amount: dict[int, dict[int, dict]] = {}


# ════════════════════════════════════════════════════════════
# 텔레그램 API (urllib)
# ════════════════════════════════════════════════════════════
def tg_call(method: str, payload: dict, timeout: int = 15) -> dict | None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        if "message is not modified" not in detail:      # 같은 내용으로 수정 시도는 무시
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
    tg_call("editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "reply_markup": {"inline_keyboard": buttons or []},
    })


def base_text(message: dict) -> str:
    """버튼이 달린 메시지에서 원래 시그널 정보 부분만 추출."""
    return (message.get("text") or "").split(SEP, 1)[0]


def decision_buttons(symbol: str) -> list:
    return [[
        {"text": "✅ 승인", "callback_data": f"buy:{symbol}"},
        {"text": "⏭ 패스", "callback_data": f"pass:{symbol}"},
    ]]


# ════════════════════════════════════════════════════════════
# SSM Parameter Store
# ════════════════════════════════════════════════════════════
def load_positions() -> dict:
    """저장된 포지션. 파라미터가 아직 없으면 {}.
    그 밖의 오류(권한·네트워크)는 그대로 올려서, 빈 값으로 덮어써 기존 데이터를 지우는 일을 막습니다."""
    try:
        value = ssm.get_parameter(Name=SSM_KEY)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return {}
    return json.loads(value) if value.strip() else {}


def save_positions(positions: dict) -> None:
    ssm.put_parameter(
        Name=SSM_KEY,
        Value=json.dumps(positions, ensure_ascii=False),
        Type="String",
        Overwrite=True,
    )


# ════════════════════════════════════════════════════════════
# 버튼 처리
# ════════════════════════════════════════════════════════════
def release_waiting(chat_id: int, message_id: int) -> bool:
    """금액 입력 대기 해제. 대기 중이었으면 True."""
    waiting = awaiting_amount.get(chat_id, {})
    was_waiting = waiting.pop(message_id, None) is not None
    if not waiting:
        awaiting_amount.pop(chat_id, None)
    return was_waiting


def handle_buy(chat_id: int, message: dict, symbol: str) -> str:
    message_id = message["message_id"]
    base = base_text(message)
    awaiting_amount.setdefault(chat_id, {})[message_id] = {"symbol": symbol, "base": base}
    edit_message(
        chat_id, message_id,
        base + SEP + "💰 매수 금액을 채팅창에 입력해주세요\n"
                     "(예: 90  또는  $90.50 / 여러 종목이면 'TEAM 100, NVDA 200')",
        buttons=[[{"text": "❌ 취소", "callback_data": f"cancel_buy:{symbol}"}]],
    )
    return "금액을 입력해주세요"


def handle_pass(chat_id: int, message: dict, symbol: str) -> str:
    release_waiting(chat_id, message["message_id"])
    edit_message(chat_id, message["message_id"], base_text(message) + SEP + "⏭ 패스했어요")
    return "패스했어요"


def handle_cancel_buy(chat_id: int, message: dict, symbol: str) -> str:
    message_id = message["message_id"]
    if release_waiting(chat_id, message_id):
        note = "금액 입력을 취소했어요"            # 아직 저장 전 → SSM은 건드리지 않음
    else:
        positions = load_positions()
        if symbol in positions:
            positions.pop(symbol)
            save_positions(positions)
            note = "승인을 취소했어요"
        else:
            note = "취소했어요"
    edit_message(chat_id, message_id, base_text(message), buttons=decision_buttons(symbol))
    return note


# ════════════════════════════════════════════════════════════
# 금액 입력 처리 (한 종목 / 여러 종목)
# ════════════════════════════════════════════════════════════
def parse_amount(text: str) -> float | None:
    cleaned = text.strip().replace("$", "").replace(",", "").strip()
    if not cleaned:
        return None
    try:
        amount = float(cleaned)
    except ValueError:
        return None
    return amount if math.isfinite(amount) else None


def normalize_symbol(raw: str) -> str:
    return raw.strip().upper().replace(".", "-")


def parse_multi(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """'TEAM 100, NVDA 200' → ([('TEAM', '100'), ('NVDA', '200')], 형식 오류 목록).
    항목은 쉼표·세미콜론·줄바꿈으로 구분하고, 'TEAM: 100' / 'TEAM=$100'도 허용."""
    pairs, errors = [], []
    for item in ITEM_SPLIT_RE.split(text):
        item = item.strip()
        if not item:
            continue
        tokens = re.sub(r"[:=]", " ", item).split()
        ticker, amount_text = tokens[0], "".join(tokens[1:])
        if not TICKER_RE.match(ticker):
            errors.append(f"'{item}' → 'TEAM 100' 형식으로 입력해주세요")
        elif not amount_text:
            errors.append(f"{normalize_symbol(ticker)}: 금액이 없어요")
        else:
            pairs.append((normalize_symbol(ticker), amount_text))
    return pairs, errors


def apply_amounts(chat_id: int, items: list[tuple[int, float]]) -> list[str]:
    """[(message_id, amount)] 를 한 번에 SSM에 저장하고 각 메시지를 '승인 완료'로 변경."""
    waiting = awaiting_amount[chat_id]
    positions = load_positions()          # 실패하면 예외 → 아무것도 저장/해제하지 않음
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for message_id, amount in items:
        symbol = waiting[message_id]["symbol"]
        positions[symbol] = {"symbol": symbol, "status": "APPROVED", "budget": amount, "approved_at": now}
    save_positions(positions)             # 여러 종목도 SSM 쓰기는 1번

    done = []
    for message_id, amount in items:
        state = waiting[message_id]
        symbol = state["symbol"]
        release_waiting(chat_id, message_id)
        edit_message(
            chat_id, message_id,
            state["base"] + SEP + f"✅ 승인 완료! 매수 금액: ${amount:,.2f}",
            buttons=[[{"text": "❌ 승인 취소", "callback_data": f"cancel_buy:{symbol}"}]],
        )
        print(f"[승인] {symbol} budget=${amount:,.2f} → SSM {SSM_KEY} 저장")
        done.append(f"✅ {symbol} ${amount:,.2f}")
    return done


def amount_error(amount: float | None) -> str | None:
    if amount is None:
        return "숫자만 입력해주세요 (예: 90  또는  $90.50)"
    if amount <= 0:
        return "0보다 큰 금액을 입력해주세요"
    return None


def handle_text(chat_id: int, msg: dict) -> None:
    waiting = awaiting_amount.get(chat_id)
    if not waiting:
        return                                      # 대기 중인 종목 없음 → 일반 채팅은 무시
    text = msg.get("text", "")

    # ── ① 종목명 없이 금액만 입력 (기존 방식) ──
    single = parse_amount(text)
    has_letters = re.search(r"[A-Za-z]", text) is not None
    has_digits = re.search(r"\d", text) is not None
    if single is not None or not has_letters or not has_digits:
        reply_to = (msg.get("reply_to_message") or {}).get("message_id")
        if reply_to in waiting:
            target = reply_to
        elif len(waiting) == 1:
            target = next(iter(waiting))
        else:
            pending = ", ".join(s["symbol"] for s in waiting.values())
            send_message(chat_id, f"⚠️ 여러 종목({pending})이 금액을 기다리고 있어요.\n"
                                  f"'TEAM 100, NVDA 200'처럼 종목명과 금액을 함께 입력하거나,\n"
                                  f"그 종목 메시지에 답장(reply)으로 보내주세요.")
            return
        err = amount_error(single)
        if err:
            send_message(chat_id, f"⚠️ {err}")
            return
        apply_amounts(chat_id, [(target, single)])
        return

    # ── ② 종목명 + 금액 (한 종목 또는 여러 종목) ──
    pairs, errors = parse_multi(text)
    by_symbol = {s["symbol"]: mid for mid, s in waiting.items()}
    items, seen = [], set()
    for symbol, amount_text in pairs:
        if symbol not in by_symbol:
            errors.append(f"{symbol}: 금액을 기다리는 종목이 아니에요")
            continue
        if symbol in seen:
            errors.append(f"{symbol}: 두 번 입력됐어요 (처음 값만 사용)")
            continue
        amount = parse_amount(amount_text)
        err = amount_error(amount)
        if err:
            errors.append(f"{symbol}: {err}")
            continue
        seen.add(symbol)
        items.append((by_symbol[symbol], amount))

    done = apply_amounts(chat_id, items) if items else []

    lines = done + [f"⚠️ {e}" for e in errors]
    left = [s["symbol"] for s in awaiting_amount.get(chat_id, {}).values()]
    if left:
        lines.append(f"⏳ 아직 금액 대기: {', '.join(left)}")
    if len(pairs) > 1 or errors:                    # 한 종목 정상 입력이면 메시지 수정만으로 충분
        send_message(chat_id, "\n".join(lines))


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
        if not handler or not symbol:
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
        sys.exit("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID 환경변수를 설정하세요.")

    print(f"봇 시작됨 (허용 Chat ID {ALLOWED_CHAT_ID}, SSM {SSM_KEY} @ {AWS_REGION})")
    offset = None
    while True:
        try:
            for update in get_updates(offset):
                offset = update["update_id"] + 1   # 처리 실패해도 같은 업데이트를 무한 반복하지 않게 먼저 증가
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
