#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""감액배당(자본준비금 감액 → 배당) 재원 스크리닝.

사용자가 보고 싶은 것 세 가지:
  1) 감액배당 실시 내역     — 어느 연도에 얼마를 준비금에서 이익잉여금으로 옮겼나
  2) 남은 감액배당 재원     — 옮긴 금액 중 아직 배당으로 나가지 않은 추정치
  3) 남은 감액배당 가능 연수 — 남은 재원 ÷ 직전년도 현금배당총액

데이터는 전부 OpenDART 구조화 API에서 온다 (2026-09-18 실측):

  fnlttSinglAcntAll  전체 재무제표. sj_div=='SCE'(자본변동표) 행의 account_detail에
                     자본 구성요소 열이 붙어 온다. 준비금 전입은 이익잉여금 열 +X /
                     자본잉여금 열 -X 의 쌍이다. 계정명은 회사마다 다르다
                     ("자본준비금 이입액", "주식발행초과금의 이익잉여금 전입",
                      "자본잉여금의 대체", "자본준비금의 전환",
                      "주식발행초과금의결손금보전" 등) → 이름 정규식 + 열 부호로 잡는다.
                     같은 응답의 BS에서 자본금·자본잉여금도 얻어 법적 한도를 계산한다.
  alotMatter         배당에 관한 사항. '현금배당금총액(백만원)' 행의 당기 값.

왜 재무상태표만으로는 안 되나: 감액을 실행하면 준비금이 이익잉여금으로 섞여
들어가 BS에서 구분되지 않는다(메리츠금융지주: 2.15조 감액 후 자본잉여금 1,248억).
그래서 '남은 재원'은 자본변동표의 전입 이력에서 누적해야 한다.

왜 공시 본문을 파싱하지 않나: 정기주총결과 본문에는 감액 문구가 거의 없다
(확정 6사 모두 0건). 자본변동표가 훨씬 정확하고 구조적이다.

한계(추정으로 표기): 배당이 비과세 재원(감액분)부터 소진된다고 가정한다.
결손금 보전(상법 460조)도 같은 이동이라 잡히지만 세법상 성격이 다를 수 있어
계정명을 그대로 노출한다. 법적 한도는 이익준비금을 구할 수 없어 과소 추정이다.
"""

import collections
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytz
import requests

import safety_margin_calc_naver as _core
from safety_margin_calc_naver import DART_API_KEY, REQUEST_TIMEOUT, load_corp_code_map
from storage import upload_to_supabase, download_from_supabase

RESULTS_FILE = 'reduction_dividend_results.json'

# 사업보고서는 연 1회라 30일 주기로 충분하다. 재무제표가 아예 없는 종목은 7일 뒤 재시도.
REFRESH_SECONDS = int(os.getenv('REDUCTION_REFRESH_SECONDS', str(30 * 86400)))
RETRY_SECONDS = int(os.getenv('REDUCTION_RETRY_SECONDS', str(7 * 86400)))
WORKERS = int(os.getenv('REDUCTION_WORKERS', '6'))
CHUNK = int(os.getenv('REDUCTION_CHUNK', '50'))
# 감액배당은 2020년 쌍용C&E 무렵부터 본격화됐다. 이보다 앞선 전입은 드물다.
HISTORY_FROM = int(os.getenv('REDUCTION_HISTORY_FROM', '2020'))

DART = 'https://opendart.fss.or.kr/api/'

# 준비금 → 이익잉여금 이동을 뜻하는 계정명. 앞뒤 순서가 회사마다 뒤바뀌므로 양방향.
_RESERVE = '(자본준비금|주식발행초과금|자본잉여금|준비금)'
_VERB = '(이입|전입|대체|전환|감소|감액|보전)'
TRANSFER_RE = re.compile(_RESERVE + '.*' + _VERB + '|(이입|전입|대체|전환).*' + _RESERVE)
# 자기주식 거래·기타포괄손익 대체는 준비금 감액이 아니다. 계정명에 자본금이 들어가면
# 무상증자(잉여금 → 자본금) 방향이라 제외한다. 남은 오탐은 열 부호 규칙이 걸러낸다.
EXCLUDE_WORDS = ('자기주식', '기타포괄', '자본금')


class DartQuotaExceeded(Exception):
    """DART 일일 요청 한도(status 020) 또는 키 오류. 이 실행에서는 더 시도하지 않는다."""


def _dart(endpoint, **params):
    """OpenDART 호출. status 000이면 list를, 013(데이터 없음)이면 None을 돌려준다.

    한도 초과·키 오류는 예외로 올려 실행을 멈춘다. 계속 두드리면 하루치 한도만 태운다.
    """
    resp = requests.get(DART + endpoint, params=dict(crtfc_key=DART_API_KEY, **params),
                        timeout=REQUEST_TIMEOUT)
    data = resp.json()
    status = data.get('status')
    if status == '000':
        return data.get('list') or []
    if status in ('010', '011', '012', '020', '021'):
        raise DartQuotaExceeded('DART status %s: %s' % (status, data.get('message')))
    return None   # 013 조회 데이터 없음 등


def _amount(text):
    """'460,000,000,000' / '-460000000000' → int. 빈값·'-'는 None."""
    if text is None:
        return None
    t = str(text).replace(',', '').strip()
    if t in ('', '-'):
        return None
    try:
        return int(t)
    except ValueError:
        return None


def latest_fiscal_year(now=None):
    """직전 사업보고서의 사업연도. 사업보고서는 3월 말까지 공시되므로 4월부터 전년도."""
    now = now or datetime.now()
    return now.year - 1 if now.month >= 4 else now.year - 2


def fetch_statements(corp_code, year, reprt_code='11011'):
    """전체 재무제표 한 해치. 연결(CFS)이 없으면 별도(OFS). 둘 다 없으면 None."""
    for fs_div in ('CFS', 'OFS'):
        rows = _dart('fnlttSinglAcntAll.json', corp_code=corp_code, bsns_year=str(year),
                     reprt_code=reprt_code, fs_div=fs_div)
        if rows:
            return rows
    return None


def extract_transfers(rows):
    """자본변동표에서 준비금 → 이익잉여금 전입액을 계정명별로 뽑는다.

    같은 계정명이 자본 구성요소 열마다 한 행씩 반복된다(이익잉여금 +X, 자본잉여금 -X,
    소계 열에 -X 등). 이익잉여금 열이 양수인 행만 전입으로 인정하고 계정명별 max를
    취해 중복을 없앤다. 이익잉여금이 줄어드는 반대 방향(무상증자·결손 등)은 자연히 빠진다.

    :return: {계정명: 금액(원)}
    """
    best = collections.defaultdict(int)
    for it in rows or []:
        if it.get('sj_div') != 'SCE':
            continue
        name = (it.get('account_nm') or '').strip()
        if not TRANSFER_RE.search(name) or any(w in name for w in EXCLUDE_WORDS):
            continue
        detail = it.get('account_detail') or ''
        if '이익잉여금' not in detail:
            continue
        value = _amount(it.get('thstrm_amount'))
        if value and value > 0:
            best[name] = max(best[name], value)
    return dict(best)


def extract_capital(rows):
    """재무상태표에서 자본금·자본잉여금을 뽑아 법적 감액 가능 한도를 계산한다.

    상법 461조의2: (자본준비금 + 이익준비금) − 1.5 × 자본금 을 초과분 한도로 감액 가능.
    이익준비금은 BS·자본변동표 어디에도 오지 않아 빼고 계산한다 → 과소(보수적) 추정.
    자본잉여금은 dart_CapitalSurplus가 표준이지만 삼성전자처럼 주식발행초과금
    (ifrs-full_SharePremium)만 보고하는 회사도 있어 둘을 모두 본다.
    """
    issued = surplus = None
    premium_sum = 0
    for it in rows or []:
        if it.get('sj_div') != 'BS':
            continue
        acct_id = it.get('account_id') or ''
        name = (it.get('account_nm') or '').strip()
        value = _amount(it.get('thstrm_amount'))
        if value is None:
            continue
        if acct_id == 'ifrs-full_IssuedCapital' or name == '자본금':
            if issued is None:
                issued = value
        elif acct_id == 'dart_CapitalSurplus' or name == '자본잉여금':
            if surplus is None:
                surplus = value
        elif acct_id == 'ifrs-full_SharePremium' or '주식발행초과금' in name:
            premium_sum += value
    if surplus is None and premium_sum:
        surplus = premium_sum
    cap = None
    if issued is not None and surplus is not None:
        cap = max(surplus - int(1.5 * issued), 0)
    return {'issued_capital': issued, 'capital_surplus': surplus, 'legal_cap': cap}


def fetch_cash_dividend(corp_code, year):
    """그 사업연도의 현금배당금총액(원). 배당 없음('-')은 0, 보고서 자체가 없으면 None."""
    rows = _dart('alotMatter.json', corp_code=corp_code, bsns_year=str(year), reprt_code='11011')
    if rows is None:
        return None
    for it in rows:
        if (it.get('se') or '').startswith('현금배당금총액'):
            v = _amount(it.get('thstrm'))
            return (v or 0) * 1000000      # 단위: 백만원
    return None


def summarize(record):
    """전입·배당 이력에서 남은 재원과 남은 연수를 계산해 record에 채운다.

    남은 재원 = Σ전입(첫 전입 연도~) − Σ현금배당총액(첫 전입 연도~), 0 하한.
    배당이 비과세 재원부터 소진된다는 통상 가정이므로 '추정'이다.
    직전년도 배당이 0/없음이면 연수는 계산하지 않는다(None).
    이력이 아직 다 모이지 않았으면(history_complete=False) 재원도 계산하지 않는다.
    부분 이력으로 낸 숫자는 틀린 숫자보다 나쁘다.
    """
    transfers = record.get('transfers') or {}
    dividends = record.get('dividends') or {}

    def _year_of(key):
        return int(str(key).rstrip('Q'))

    years_with_transfer = sorted(_year_of(y) for y, t in transfers.items() if t and sum(t.values()) > 0)
    record['has_reduction'] = bool(years_with_transfer)
    record['total_transferred'] = sum(sum(t.values()) for t in transfers.values())

    # 직전년도 배당: 최신 연도부터 내려가며 양수인 첫 값
    last_div = last_div_year = None
    for y in sorted((int(k) for k in dividends), reverse=True):
        v = dividends.get(str(y))
        if v:
            last_div, last_div_year = v, y
            break
    record['last_dividend'] = last_div
    record['last_dividend_year'] = last_div_year

    if not years_with_transfer or not record.get('history_complete'):
        record['first_transfer_year'] = years_with_transfer[0] if years_with_transfer else None
        record['dividends_since_first'] = None
        record['remaining_fund'] = None
        record['remaining_years'] = None
        return record

    first = years_with_transfer[0]
    paid = 0
    if dividends:
        last_year = max(int(k) for k in dividends)
        paid = sum((dividends.get(str(y)) or 0) for y in range(first, last_year + 1))
    remaining = max(record['total_transferred'] - paid, 0)
    record['first_transfer_year'] = first
    record['dividends_since_first'] = paid
    record['remaining_fund'] = remaining
    record['remaining_years'] = round(remaining / last_div, 1) if last_div else None
    return record


def _scan_company(code, corp_code, name, existing, latest_fy, current_time):
    """한 회사의 빠진 연도를 채운다.

    최신 연도부터 내려가며 조회하므로 예산이 잘려도 '감액 여부'는 먼저 확정되고
    옛 이력은 다음 실행이 이어받는다.
    """
    rec = dict(existing or {})
    rec.update({'code': code, 'name': name, 'corp_code': corp_code})
    rec.setdefault('transfers', {})
    rec.setdefault('dividends', {})
    rec.setdefault('transfer_accounts', {})
    scanned = set(rec.get('scanned_years') or [])
    got_any_statement = bool(rec.get('capital_year'))

    for year in range(latest_fy, HISTORY_FROM - 1, -1):
        if year in scanned:
            continue
        rows = fetch_statements(corp_code, year)
        if rows is None:
            # 그 해 보고서가 없다(상장 전 등). 확인했다는 표시만 남긴다.
            scanned.add(year)
            continue
        got_any_statement = True
        transfers = extract_transfers(rows)
        if transfers:
            rec['transfers'][str(year)] = transfers
            rec['transfer_accounts'][str(year)] = sorted(transfers)
        if not rec.get('capital_year') or year > rec['capital_year']:
            cap = extract_capital(rows)
            if cap['legal_cap'] is not None:
                rec.update(cap)
                rec['capital_year'] = year
        div = fetch_cash_dividend(corp_code, year)
        if div is not None:
            rec['dividends'][str(year)] = div
        scanned.add(year)

    # 감액 회사는 올해 분기·반기보고서도 본다. 임시주총 결의(메리츠 2023-11 식)는
    # 다음 사업보고서까지 기다리면 반년 넘게 놓친다.
    if rec['transfers']:
        this_year = latest_fy + 1
        for reprt in ('11014', '11012', '11013'):      # 3분기 → 반기 → 1분기 중 최신
            rows = fetch_statements(corp_code, this_year, reprt)
            if rows:
                q = extract_transfers(rows)
                key = '%dQ' % this_year
                if q:
                    rec['transfers'][key] = q
                    rec['transfer_accounts'][key] = sorted(q)
                else:
                    rec['transfers'].pop(key, None)
                    rec['transfer_accounts'].pop(key, None)
                break

    rec['scanned_years'] = sorted(scanned)
    rec['history_complete'] = all(y in scanned for y in range(HISTORY_FROM, latest_fy + 1))
    rec['no_data'] = not got_any_statement
    rec['latest_fy'] = latest_fy
    rec['last_updated'] = current_time.isoformat()
    return summarize(rec)


def _load_existing():
    if os.path.exists(RESULTS_FILE):
        try:
            with open(RESULTS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if data:
                    return data
        except Exception:
            pass
    data = download_from_supabase(RESULTS_FILE)
    if data:
        try:
            with open(RESULTS_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass
    return data or []


def _sort_key(r):
    # 감액 회사(남은 연수 큰 순) → 감액 회사(연수 미계산) → 나머지
    if r.get('has_reduction'):
        ry = r.get('remaining_years')
        return (0, -(ry if ry is not None else -1))
    return (1, 0)


def _save(results, upload):
    results = sorted(results, key=_sort_key)
    with open(RESULTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False)
    if upload:
        upload_to_supabase(RESULTS_FILE, results)
    return results


def calculate_reduction_dividend_screening(time_budget_seconds=None):
    """전 종목의 감액배당 재원을 갱신한다. NCAV 스크리닝과 같은 틀.

    - 종목마다 FY{HISTORY_FROM}~직전 사업연도 중 아직 안 본 연도만 조회한다(점진 백필).
    - 30일 안에 본 종목은 건너뛴다. 재무제표가 전혀 없던 종목은 7일 뒤 재시도.
    - 묶음마다 시간 예산을 확인하고 로컬 저장한다. DART 한도 초과(020)면 즉시 멈춘다.
    """
    started_at = time.monotonic()
    krx = _core.KRX_STOCKS
    if krx is None:
        print("❗ KRX_STOCKS is None. load_krx_stocks()를 먼저 호출하세요.", flush=True)
        return []
    corp_map = load_corp_code_map()
    if not corp_map:
        return []

    names = {row['Code']: row['Name'] for _, row in krx.iterrows()}
    existing = {r['code']: r for r in _load_existing()}
    kst = pytz.timezone('Asia/Seoul')
    now = datetime.now(kst)
    latest_fy = latest_fiscal_year(now)

    targets = []
    for code in names:
        if code not in corp_map:
            continue
        rec = existing.get(code)
        if rec and rec.get('last_updated'):
            try:
                age = (now - datetime.fromisoformat(rec['last_updated'])).total_seconds()
                limit = RETRY_SECONDS if rec.get('no_data') else REFRESH_SECONDS
                # 이력이 덜 모였거나 새 사업연도가 나왔으면 주기와 무관하게 다시 본다
                stale = (not rec.get('history_complete')) or rec.get('latest_fy') != latest_fy
                if age < limit and not stale:
                    continue
            except ValueError:
                pass
        targets.append(code)

    print("\n📊 감액배당 재원 스크리닝: %d개 조회 예정 (기존 %d개, 직전 사업연도 %d, 이력 %d~)"
          % (len(targets), len(existing), latest_fy, HISTORY_FROM), flush=True)
    if not targets:
        print("⏩ 감액배당: 갱신 대상 없음 → 저장/업로드 생략", flush=True)
        return sorted(existing.values(), key=_sort_key)

    results = dict(existing)
    done = detected = 0
    quota_hit = False

    def _safe(code):
        try:
            rec = _scan_company(code, corp_map[code], names[code], existing.get(code), latest_fy, now)
            return code, rec, None
        except DartQuotaExceeded as e:
            return code, None, e
        except Exception as e:
            print("❗ 감액배당 %s 조회 오류: %s" % (code, type(e).__name__), flush=True)
            return code, None, None

    for start in range(0, len(targets), CHUNK):
        if time_budget_seconds is not None and (time.monotonic() - started_at) > time_budget_seconds:
            print("⏱️ 감액배당 시간 예산 %d초 소진 → %d개 처리 후 중단" % (time_budget_seconds, done), flush=True)
            break
        chunk = targets[start:start + CHUNK]
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            outcomes = list(pool.map(_safe, chunk))
        for code, rec, err in outcomes:
            if isinstance(err, DartQuotaExceeded):
                quota_hit = True
                continue
            if rec is None:
                continue
            results[code] = rec
            done += 1
            if rec.get('has_reduction'):
                detected += 1
        _save(list(results.values()), upload=False)
        elapsed = int(time.monotonic() - started_at)
        print("💾 감액배당 [%d/%d] %d초, 누적 %d개 처리 / 감액 이력 %d개"
              % (min(start + CHUNK, len(targets)), len(targets), elapsed, done, detected), flush=True)
        if quota_hit:
            print("🛑 DART 요청 한도 초과 → 이번 실행 중단 (다음 실행이 이어받음)", flush=True)
            break

    final = _save(list(results.values()), upload=done > 0)
    if done == 0:
        print("⏩ 감액배당: 신규 처리 없음 → Supabase 업로드 생략", flush=True)
    with_hist = [r for r in final if r.get('has_reduction')]
    complete = [r for r in with_hist if r.get('remaining_years') is not None]
    print("\n✅ 감액배당 스크리닝 완료: %d개 중 감액 이력 %d개 (재원·연수 계산 완료 %d개)"
          % (len(final), len(with_hist), len(complete)), flush=True)
    return final
