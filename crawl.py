#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""배치 크롤러 진입점.

웹앱(app.py)과 분리되어 스케줄러(GitHub Actions)에서 실행된다.
네이버/DART를 크롤링해 결과를 Supabase Storage에 올리고 종료한다.
웹앱은 그 결과를 읽기만 하므로, 크롤링으로 발생하는 트래픽이
웹 호스트의 대역폭 청구서에 잡히지 않는다.

환경변수:
  SUPABASE_URL, SUPABASE_KEY   필수. 결과 업로드용
  DART_API_KEY                 없으면 NCAV 스크리닝을 건너뛴다
  CRAWL_BUDGET_SECONDS         안전마진 분석 시간 상한 (기본 3600)
  NCAV_BUDGET_SECONDS          NCAV 스크리닝 시간 상한 (기본 1800)
  STOCK_REFRESH_SECONDS        종목 재분석 주기 (기본 3600)

로컬 실행:
  python crawl.py
"""

import os
import sys
import time
from datetime import datetime

import pytz

# encoding: Windows 콘솔(cp949)에서 로그의 이모지가 UnicodeEncodeError를
#   일으켜 크롤링이 중단되는 것을 막는다. CI(Linux)는 이미 UTF-8이라 무해하다.
# line_buffering: 버퍼에 갇힌 로그가 유실되지 않게 한다. 장시간 도는
#   작업이라 진행 상황이 실시간으로 보여야 한다.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)

from safety_margin_calc_naver import (
    load_krx_stocks,
    analyze_all_stocks,
    calculate_ncav_screening,
)

KST = pytz.timezone('Asia/Seoul')


def _log(msg: str):
    print(f"[{datetime.now(KST):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def main() -> int:
    started = time.monotonic()

    missing = [k for k in ('SUPABASE_URL', 'SUPABASE_KEY') if not os.getenv(k)]
    if missing:
        _log(f"❌ 환경변수 누락: {', '.join(missing)}")
        _log("   결과를 올릴 곳이 없으므로 크롤링하지 않고 종료한다.")
        return 1

    crawl_budget = int(os.getenv('CRAWL_BUDGET_SECONDS', '3600'))
    ncav_budget = int(os.getenv('NCAV_BUDGET_SECONDS', '1800'))

    # CI는 체크아웃 직후라 krx_stocks.json의 mtime이 항상 '방금'이다.
    # force를 켜지 않으면 종목 목록이 영원히 갱신되지 않는다.
    _log("KRX 종목 목록 갱신...")
    load_krx_stocks(force=True)

    _log(f"안전마진 분석 시작 (시간 예산 {crawl_budget}초)")
    try:
        analyze_all_stocks(time_budget_seconds=crawl_budget)
    except Exception as e:
        _log(f"❌ 안전마진 분석 실패: {e}")
        return 1

    if os.getenv('DART_API_KEY'):
        _log(f"NCAV 스크리닝 시작 (시간 예산 {ncav_budget}초)")
        try:
            calculate_ncav_screening(time_budget_seconds=ncav_budget)
        except Exception as e:
            # NCAV는 부가 기능이다. 여기서 실패해도 안전마진 결과는 이미
            # 업로드됐으므로 실행 전체를 실패로 만들지 않는다.
            _log(f"⚠️ NCAV 스크리닝 실패(무시하고 진행): {e}")
    else:
        _log("⏩ DART_API_KEY 없음 → NCAV 스크리닝 건너뜀")

    _log(f"✅ 전체 완료 ({time.monotonic() - started:.0f}초)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
