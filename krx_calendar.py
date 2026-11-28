"""
krx_calendar.py — 한국거래소(KRX) 휴장일·개장 지연일 (scanner_kr.py, monitor_kr.py 공용)

음력 명절·대체공휴일·선거일이 있어 규칙으로 계산하지 않고 표로 관리합니다.
출처: python 'holidays' 패키지의 XKRX 달력 (2026-11-28 기준, 2026~2028). 매년 말 KRX 공지로 확인·추가하세요.
목록에 없는 휴장일이 있어도 monitor_kr.py는 주문이 거절되면 APPROVED로 남겨두고 다음 장에 다시 시도합니다.
"""

from __future__ import annotations

from datetime import date

KRX_HOLIDAYS: set[date] = {
    # 2026
    date(2026, 1, 1), date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18), date(2026, 3, 2),
    date(2026, 5, 1), date(2026, 5, 5), date(2026, 5, 25), date(2026, 6, 3), date(2026, 7, 17),
    date(2026, 8, 17), date(2026, 9, 24), date(2026, 9, 25), date(2026, 10, 5), date(2026, 10, 9),
    date(2026, 12, 25), date(2026, 12, 31),
    # 2027
    date(2027, 1, 1), date(2027, 2, 8), date(2027, 2, 9), date(2027, 3, 1), date(2027, 5, 3),
    date(2027, 5, 5), date(2027, 5, 13), date(2027, 7, 19), date(2027, 8, 16), date(2027, 9, 14),
    date(2027, 9, 15), date(2027, 9, 16), date(2027, 10, 4), date(2027, 10, 11), date(2027, 12, 27),
    date(2027, 12, 31),
    # 2028 (1/1은 토요일, 12/31은 일요일이라 연말휴장일은 12/29)
    date(2028, 1, 26), date(2028, 1, 27), date(2028, 1, 28), date(2028, 3, 1), date(2028, 4, 12),
    date(2028, 5, 1), date(2028, 5, 2), date(2028, 5, 5), date(2028, 6, 6), date(2028, 7, 17),
    date(2028, 8, 15), date(2028, 10, 2), date(2028, 10, 3), date(2028, 10, 4), date(2028, 10, 5),
    date(2028, 10, 9), date(2028, 12, 25), date(2028, 12, 29),
}
EXTRA_CLOSED_DAYS: set[date] = set()       # 임시 휴장이 발표되면 추가
HOLIDAY_YEARS = {d.year for d in KRX_HOLIDAYS}

# 개장이 늦어지는 날 → 개장 시각(자정부터 분). 새해 첫 거래일 10:00, 수능일 10:00 (수능일은 교육부 발표로 확인)
LATE_OPEN_DAYS: dict[date, int] = {
    date(2027, 1, 4): 10 * 60,
    date(2027, 11, 18): 10 * 60,   # 2027 수능 예정일 (확정 발표 후 확인)
    date(2028, 1, 3): 10 * 60,     # 2028 첫 거래일
    # 2028 수능일은 아직 발표 전 — 발표되면 추가
}


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in KRX_HOLIDAYS and d not in EXTRA_CLOSED_DAYS
