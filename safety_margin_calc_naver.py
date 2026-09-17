#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
from lxml import html
import pandas as pd
import FinanceDataReader as fdr
from datetime import datetime, timedelta
import json
import os
from tqdm import tqdm
import math

import time
import threading
from concurrent.futures import ThreadPoolExecutor
import pytz
from dotenv import load_dotenv

# Supabase 입출력은 storage 모듈로 분리되어 있다. 웹앱이 이 크롤러 모듈을
# 임포트하지 않고도 결과를 내려받을 수 있게 하기 위함이다.
from storage import upload_to_supabase, download_from_supabase

# 환경 변수 로드
load_dotenv()

# DART OpenAPI 설정
DART_API_KEY = os.getenv('DART_API_KEY')
CORP_CODE_MAP = None  # 종목코드 → corp_code 매핑 딕셔너리
NCAV_RESULTS_FILE = 'ncav_results.json'

# KRX 종목 목록 파일 경로
KRX_STOCKS_FILE = 'krx_stocks.json'
RESULTS_FILE = 'all_safety_margin_results.json'
KRX_STOCKS = None

# 크롤링 중 Supabase 체크포인트 업로드 간격 (분석한 종목 수 기준).
# 값을 키울수록 아웃바운드 대역폭을 아끼고, 재시작 시 잃는 작업량이 늘어난다.
SUPABASE_CHECKPOINT_EVERY = int(os.getenv('SUPABASE_CHECKPOINT_EVERY', '500'))

# 재무지표(EPS·BPS·자사주) 재크롤링 주기 (초). 기본 7일.
# 이 값들은 분기마다 바뀌므로 매일 긁을 이유가 없다. 계속 바뀌는 주가는
# refresh_prices()가 네이버 실시간 시세로 묶음 조회해 갱신한다.
FUNDAMENTALS_REFRESH_SECONDS = int(os.getenv('FUNDAMENTALS_REFRESH_SECONDS', str(7 * 86400)))

# 네이버 크롤링 동시 요청 수. 너무 올리면 차단당한다.
CRAWL_WORKERS = int(os.getenv('CRAWL_WORKERS', '6'))
# 한 묶음을 처리한 뒤 시간 예산을 확인하고 중간 저장한다.
CRAWL_CHUNK = int(os.getenv('CRAWL_CHUNK', '50'))

# (연결, 응답) 타임아웃. 연결이 5초 안에 안 되면 30초를 기다려도 안 된다.
# 기존 30초 단일 타임아웃은 실패 1건당 30초를 통째로 버렸다.
CONNECT_TIMEOUT = float(os.getenv('CONNECT_TIMEOUT', '5'))
READ_TIMEOUT = float(os.getenv('READ_TIMEOUT', '15'))
REQUEST_TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)

# wisereport(자사주 조회)가 연속으로 이만큼 실패하면 그 실행에서는 포기한다.
# 자사주 정보가 없으면 treasury_ratio=0 으로 계산이 성립하므로,
# 죽은 호스트에 계속 타임아웃을 쌓는 것보다 낫다.
TREASURY_FAILURE_LIMIT = int(os.getenv('TREASURY_FAILURE_LIMIT', '20'))
_treasury_failures = 0
_treasury_lock = threading.Lock()

# 상장폐지 종목 정리의 안전장치.
# KRX 응답이 일시적으로 불완전할 때 멀쩡한 종목을 지우면 복구에 며칠이
# 걸리므로(재크롤링 필요), 목록이 수상하면 정리를 통째로 건너뛴다.
MIN_KRX_SIZE_FOR_PRUNE = int(os.getenv('MIN_KRX_SIZE_FOR_PRUNE', '2000'))
MAX_PRUNE_RATIO = float(os.getenv('MAX_PRUNE_RATIO', '0.1'))

# NCAV 재조회 주기 (초). 기본 30일.
# NCAV는 DART 사업보고서(reprt_code=11011)의 유동자산·부채총계에서 나오고,
# 사업보고서는 1년에 한 번(3월 말까지) 공시된다. 기존 24시간 주기는 1년에
# 한 번 바뀌는 값을 365번 다시 받는 셈이었다.
NCAV_REFRESH_SECONDS = int(os.getenv('NCAV_REFRESH_SECONDS', str(30 * 86400)))
# NCAV를 구할 수 없는 종목의 재시도 주기 (초). 기본 7일.
# 보험·은행 등은 재무상태표에 유동자산 구분이 없어 영구히 값이 나오지 않는다.
# 실패를 기록하지 않으면 매 실행 재조회하게 되므로 마커를 남기되,
# DART 장애로 인한 일시적 실패일 수도 있으므로 성공보다는 짧게 잡는다.
NCAV_RETRY_SECONDS = int(os.getenv('NCAV_RETRY_SECONDS', str(7 * 86400)))
# DART 동시 요청 수. 일일 호출 한도가 있으므로 과하게 올리지 않는다.
NCAV_WORKERS = int(os.getenv('NCAV_WORKERS', '6'))
NCAV_CHUNK = int(os.getenv('NCAV_CHUNK', '50'))

def load_krx_stocks(force: bool = False):
    """KRX 종목 목록을 파일에서 로드하거나 업데이트

    force=True 이면 파일 수정 시각과 무관하게 새로 내려받는다.
    CI에서는 체크아웃 직후 파일 mtime이 항상 '방금'이라 mtime 기반 판단이
    무의미하므로, 스케줄러에서 실행할 때는 force를 켜야 목록이 갱신된다.
    """
    global KRX_STOCKS

    # 먼저 기존 파일이 있으면 로드
    if os.path.exists(KRX_STOCKS_FILE):
        try:
            with open(KRX_STOCKS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                KRX_STOCKS = pd.DataFrame(data)
                print(f"KRX 종목 목록 파일 로드 완료: {len(KRX_STOCKS)}개 종목")
        except Exception as e:
            print(f"KRX 종목 목록 파일 로드 중 오류 발생: {e}")

    # 파일의 수정 시간 확인하여 하루가 지났으면 업데이트 시도
    if not force and os.path.exists(KRX_STOCKS_FILE):
        file_time = datetime.fromtimestamp(os.path.getmtime(KRX_STOCKS_FILE))
        now = datetime.now()

        if now - file_time < timedelta(days=1):
            # 하루가 지나지 않았다면 기존 데이터 사용
            return

    # 파일이 없거나 하루가 지났다면 새로 다운로드 시도
    try:
        new_stocks = fdr.StockListing('KRX')
        if new_stocks is not None and len(new_stocks) > 0:
            # 필요한 컬럼만 유지
            # Close·Volume도 보존한다. 평소 주가는 네이버 실시간 시세를
            # 쓰지만(refresh_prices), 네이버가 막히면 이 값으로 폴백한다.
            # 이 목록의 값은 제3자 저장소의 일별 CSV라 실시간이 아니다.
            keep = [c for c in ('Code', 'Name', 'Marcap', 'Close', 'Volume')
                    if c in new_stocks.columns]
            KRX_STOCKS = new_stocks[keep].copy()
            with open(KRX_STOCKS_FILE, 'w', encoding='utf-8') as f:
                json.dump(KRX_STOCKS.to_dict('records'), f, ensure_ascii=False)
            print(f"KRX 종목 목록 다운로드 완료: {len(KRX_STOCKS)}개 종목")
        else:
            if KRX_STOCKS is not None:
                print(f"KRX API 응답 없음, 기존 캐시 데이터 사용 ({len(KRX_STOCKS)}개 종목)")
            else:
                print("KRX 종목 목록 다운로드 실패, 캐시 데이터도 없음")
    except Exception as e:
        if KRX_STOCKS is not None:
            print(f"KRX API 오류 (기존 캐시 데이터 사용 중: {len(KRX_STOCKS)}개 종목)")
        else:
            print(f"KRX 종목 목록 다운로드 중 오류 발생: {e}")


def reset_treasury_circuit():
    """실행 시작 시 서킷 브레이커를 초기화한다."""
    global _treasury_failures
    with _treasury_lock:
        _treasury_failures = 0


def get_treasury_stock_info(ticker: str) -> dict:
    """자사주 정보 조회.

    wisereport가 응답하지 않는 일이 잦다(해외 러너에서 특히). 연속 실패가
    TREASURY_FAILURE_LIMIT를 넘으면 그 실행에서는 더 시도하지 않는다.
    자사주 정보가 없으면 ratio=0으로 내재가치 조정만 생략될 뿐 계산은
    성립하므로, 죽은 호스트에 타임아웃을 계속 쌓는 것보다 낫다.
    """
    global _treasury_failures

    with _treasury_lock:
        if _treasury_failures >= TREASURY_FAILURE_LIMIT:
            return {'shares': 0, 'ratio': 0}

    try:
        url = f"https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd={ticker}"
        headers = {
            'Referer': 'https://finance.naver.com',
            'User-Agent': 'Mozilla/5.0'
        }
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        
        doc = html.fromstring(resp.text)
        
        # 자사주 행 찾기
        treasury_rows = doc.xpath("//tr[contains(., '자사주')]")
        if not treasury_rows:
            return {'shares': 0, 'ratio': 0}
            
        # 자사주 행에서 주식수와 지분율 추출
        shares_node = treasury_rows[0].xpath(".//td[2]")  # 주식수는 두 번째 열
        ratio_node = treasury_rows[0].xpath(".//td[3]")   # 지분율은 세 번째 열
        
        shares = 0
        ratio = 0
        
        if shares_node:
            shares_text = shares_node[0].text_content().strip().replace(',', '')
            try:
                shares = float(shares_text)
            except ValueError:
                pass
                
        if ratio_node:
            ratio_text = ratio_node[0].text_content().strip().replace('%', '').replace(',', '')
            try:
                ratio = float(ratio_text)
            except ValueError:
                pass
        
        with _treasury_lock:
            _treasury_failures = 0  # 성공하면 연속 실패 카운터를 되돌린다
        return {'shares': shares, 'ratio': ratio}

    except Exception as e:
        with _treasury_lock:
            _treasury_failures += 1
            n = _treasury_failures
        if n <= 3:
            print(f"자사주 정보 조회 실패 ({ticker}): {type(e).__name__}", flush=True)
        elif n == TREASURY_FAILURE_LIMIT:
            print(f"⚠️ 자사주 조회 연속 {n}회 실패 → 이번 실행에서는 중단 "
                  f"(treasury_ratio=0으로 계산 계속)", flush=True)
        return {'shares': 0, 'ratio': 0}


def calculate_intrinsic_value(df: pd.DataFrame, treasury_stock_info: dict = None) -> float:
    """
    내재가치를 계산합니다.
    BPS와 EPS의 가중평균의 평균을 사용합니다.
    EPS 가중평균 = (최근년도EPS*3 + 전년도EPS*2 + 전전년도EPS*1) / 6
    
    자사주가 있는 경우, 내재가치는 100/(100-자사주비율)을 곱하여 조정됩니다.
    자사주가 없는 경우(ratio=0)는 조정하지 않습니다.
    """
    if df.empty:
        return None
        
    if df['EPS'].isna().any() and df['BPS'].isna().any() :
        return None
    # EPS 가중평균 계산
    eps_values = df['EPS'].values
    if len(eps_values) != 3:
        return None
        
    weighted_eps = (eps_values[2] * 3 + eps_values[1] * 2 + eps_values[0] * 1) / 6
    
    # BPS는 최근년도 값 사용
    latest_bps = df['BPS'].values[-1]
    
    # 내재가치 = (EPS 가중평균 + BPS) / 2
    intrinsic_value = (weighted_eps*10 + latest_bps) / 2
    
    # 자사주 비율이 있는 경우에만 내재가치 조정
    if treasury_stock_info and treasury_stock_info.get('ratio', 0) > 0:
        treasury_ratio = treasury_stock_info['ratio']
        # 내재가치 = 기존내재가치 * (100 / (100 - 자사주비율))
        intrinsic_value = intrinsic_value * (100 / (100 - treasury_ratio))
    
    return intrinsic_value

def search_stock_codes(company_name: str) -> list:
    """
    종목명으로 종목코드를 검색합니다.
    FinanceDataReader를 사용하여 KRX 상장 종목 정보를 가져옵니다.
    
    :param company_name: 검색할 종목명
    :return: [{'code': str, 'name': str}, ...] 형식의 리스트
    """
    try:
        if KRX_STOCKS is None:
            return []
            
        # 종목명에 검색어가 포함된 종목 찾기
        mask = KRX_STOCKS['Name'].str.contains(company_name, case=False, na=False)
        matches = KRX_STOCKS[mask]
        
        results = []
        for _, row in matches.iterrows():
            results.append({
                'code': row['Code'],
                'name': row['Name']
            })
        
        return results
        
    except Exception as e:
        print(f"검색 중 오류 발생: {str(e)}")
        return []



# 네이버 증권 종목 API (m.stock.naver.com).
# 2026-09 무렵 finance.naver.com/item/main.naver 가 stock.naver.com(Next.js)으로
# 리다이렉트되면서 옛 HTML 구조가 사라졌다. tr[10]/tr[12]/tr[13] 같은 행 번호
# XPath가 전부 빈 결과를 내 종목명이 'Unknown', 내재가치가 None이 됐다.
# 새 페이지는 아래 JSON API로 데이터를 받으며, 행을 'EPS'/'BPS'/'PBR' 제목으로
# 찾고 추정치 컬럼은 isConsensus 플래그로 걸러내므로 위치 의존이 없다.
NAVER_STOCK_API = 'https://m.stock.naver.com/api/stock/'


def _naver_api_json(path: str):
    """m.stock.naver.com/api/stock/{path} 를 받아 JSON으로 돌려준다.

    없는 종목은 409 등 비정상 상태로 오므로 raise_for_status로 예외를 낸다.
    호출부(analyze_stock)의 except가 {'error': ...}로 바꿔 준다.
    """
    resp = requests.get(NAVER_STOCK_API + path,
                        headers={'User-Agent': 'Mozilla/5.0'},
                        timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _naver_num(text):
    """'2,131' → 2131, '-7,512' → -7512, '1.51' → 1.51, '-'/'N/A'/'' → None.

    옛 extract()와 같은 규칙이다: 소수점이 있으면 float, 없으면 int.
    """
    if text is None:
        return None
    t = str(text).strip().replace(',', '').replace('−', '-').replace('%', '')
    if t in ('', '-', 'N/A'):
        return None
    try:
        return float(t) if '.' in t else int(t)
    except ValueError:
        return None


def fetch_naver_fundamentals(ticker: str) -> dict:
    """종목명·배당수익률·3개년 EPS/BPS/PBR 을 네이버 API에서 받는다.

    integration    : stockName, totalInfos[dividendYieldRatio]
    finance/annual : financeInfo.trTitleList(기간, isConsensus) + rowList(행)

    반환하는 DataFrame은 옛 파싱과 같은 모양이다. index가 시간순 오름차순
    ["3년전","2년전","직전년도"] 이어야 한다. calculate_intrinsic_value()가
    .values[0], [1], [2] 위치로 읽기 때문이다.

    재무 행이 없는 종목(인프라펀드 등)은 rowList가 비어 오는데, 그때도 None
    3행짜리 DataFrame을 만들어 준다. 옛 코드도 XPath가 비면 셀마다 None을
    넣었고, calculate_intrinsic_value()는 그 경우 None을 반환한다.
    """
    integ = _naver_api_json(f'{ticker}/integration')
    stock_name = (integ.get('stockName') or '').strip() or 'Unknown'

    totals = {t.get('code'): t.get('value')
              for t in integ.get('totalInfos', []) if isinstance(t, dict)}
    dividend_yield = _naver_num(totals.get('dividendYieldRatio'))
    last_close = _naver_num(totals.get('lastClosePrice'))

    fin = _naver_api_json(f'{ticker}/finance/annual')
    info = fin.get('financeInfo') or {}
    # 확정 실적 컬럼만, 오래된 순으로. 마지막 3개가 3년전·2년전·직전년도다.
    actual_keys = sorted(t['key'] for t in info.get('trTitleList') or []
                         if t.get('isConsensus') == 'N' and t.get('key'))
    actual_keys = actual_keys[-3:]

    rows = {r.get('title'): (r.get('columns') or {})
            for r in info.get('rowList') or [] if isinstance(r, dict)}

    def series(title):
        cols = rows.get(title, {})
        vals = [_naver_num((cols.get(k) or {}).get('value')) for k in actual_keys]
        return ([None] * (3 - len(vals)) + vals)[-3:]   # 항상 3개

    periods = ["3년전", "2년전", "직전년도"]
    df = pd.DataFrame({'PBR': series('PBR'), 'EPS': series('EPS'),
                       'BPS': series('BPS')}, index=periods)

    return {'stock_name': stock_name, 'dividend_yield': dividend_yield,
            'last_close': last_close, 'df': df}


def analyze_stock(ticker: str) -> dict:
    """
    종목코드를 입력받아 내재가치와 안전마진을 계산하여 반환합니다.
    네이버 API 3회(integration, finance/annual, 실시간 시세) + wisereport 1회.
    """
    try:
        # 1) 네이버 API 2회: 종목명·배당률(integration) + 3개년 재무(finance/annual)
        fund = fetch_naver_fundamentals(ticker)
        stock_name = fund['stock_name']
        dividend_yield = fund['dividend_yield']
        df = fund['df']

        # 현재가는 실시간 시세 API가 정답이다. integration의 lastClosePrice는
        # '전일' 종가라 장중에는 한 박자 늦다. 실시간이 비면 그 값으로 폴백한다.
        quote = fetch_naver_prices([ticker]).get(ticker)
        current_price = float(quote['price']) if quote else (
            float(fund['last_close']) if fund['last_close'] else None)

        # 2) 자사주 정보 (wisereport — 별도 요청)
        treasury_stock = get_treasury_stock_info(ticker)

        # 내재가치 계산
        intrinsic_value = calculate_intrinsic_value(df, treasury_stock)

        # 안전마진 계산
        safety_margin = None
        if current_price and intrinsic_value:
            safety_margin = ((intrinsic_value - current_price) / current_price) * 100

        # 재무지표 데이터 포맷팅
        historical_data = {}
        for _, row in df.iterrows():
            historical_data[row.name] = {
                'PBR': float(row['PBR']) if not pd.isna(row['PBR']) else None,
                'EPS': float(row['EPS']) if not pd.isna(row['EPS']) else None,
                'BPS': float(row['BPS']) if not pd.isna(row['BPS']) else None
            }

        return {
            'stock_name': stock_name,
            'current_price': current_price,
            'intrinsic_value': intrinsic_value,
            'safety_margin': safety_margin,
            'treasury_shares': treasury_stock.get('shares', 0),
            'treasury_ratio': treasury_stock.get('ratio', 0),
            'dividend_yield': dividend_yield,
            'historical_data': historical_data
        }

    except Exception as e:
        print(f"종목 {ticker} 분석 중 오류 발생: {e}")
        return {'error': str(e)}

def analyze_stock_wrapper(args):
    """analyze_stock 함수를 병렬 처리하기 위한 래퍼 함수"""
    code, name = args
    try:
        result = analyze_stock(code)
        if not result.get('error') and result.get('safety_margin') is not None:
            return {
                'code': code,
                'name': result['stock_name'],
                'current_price': result['current_price'],
                'intrinsic_value': result['intrinsic_value'],
                'safety_margin': result['safety_margin'],
                'treasury_ratio': result['treasury_ratio'],
                'dividend_yield': result['dividend_yield']
            }
    except Exception as e:
        print(f"종목 {code} 분석 중 오류 발생: {e}")
    return None

def margin_key(x):
    m = x['safety_margin']
    # None이나 nan이면 아주 작은 값으로 치환해서 맨 뒤로 보내기
    if m is None or math.isnan(m):
        return float('-inf')
    return m

def load_results_data() -> list:
    """
    결과 데이터를 로드합니다.
    1. 로컬 파일 확인
    2. 없으면 Supabase Storage에서 다운로드
    """
    # 로컬 파일 확인
    if os.path.exists(RESULTS_FILE):
        try:
            with open(RESULTS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if data:
                    print(f"📁 로컬 파일에서 로드: {len(data)}개 종목")
                    return data
        except Exception as e:
            print(f"⚠️ 로컬 파일 로드 실패: {e}")

    # Supabase Storage에서 다운로드
    data = download_from_supabase(RESULTS_FILE)
    if data:
        # 로컬에도 저장
        try:
            with open(RESULTS_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
            print(f"📁 Supabase에서 다운로드 후 로컬 저장 완료")
        except Exception as e:
            print(f"⚠️ 로컬 저장 실패: {e}")
        return data

    return []

def save_results_data(results: list, upload: bool = True):
    """결과 데이터를 로컬과 Supabase Storage에 저장

    upload=False 이면 로컬 저장만 수행합니다. 변경된 내용이 없을 때
    불필요한 업로드로 아웃바운드 대역폭을 소모하지 않기 위함입니다.
    """
    # 로컬 저장
    try:
        with open(RESULTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ 로컬 저장 실패: {e}")

    # Supabase Storage 업로드
    if upload:
        upload_to_supabase(RESULTS_FILE, results)

def get_latest_trading_date() -> str:
    """KRX 종가 데이터가 실제로 어느 거래일 것인지 반환한다 ('YYYY-MM-DD').

    StockListing은 '최신 종가'를 줄 뿐 그게 어느 날짜인지 알려주지 않는다.
    휴장일이나 장 마감 전에 실행하면 직전 거래일 종가가 오는데, 가져온 시각을
    그대로 기준일로 표시하면 사용자에게 오늘 시세인 것처럼 보인다.
    (실제로 2026-08-17 광복절 대체공휴일에 이 혼동이 발생했다.)

    KOSPI 지수의 마지막 인덱스를 거래일로 삼는다. 실패하면 None을 반환하고
    호출부는 기준일 표시를 생략한다.
    """
    try:
        start = (datetime.now() - timedelta(days=21)).strftime('%Y-%m-%d')
        idx = fdr.DataReader('KS11', start).index
        if len(idx):
            return str(idx[-1].date())
    except Exception as e:
        print(f"⚠️ 거래일 확인 실패: {type(e).__name__}", flush=True)
    return None


def prune_delisted(results_dict: dict, krx_codes: set) -> int:
    """KRX 목록에 없는 종목을 결과에서 제거한다.

    상장폐지·종목코드 변경으로 목록에서 빠진 항목은 어느 경로로도 갱신되지
    않는다. refresh_prices()는 KRX 목록을 훑으므로 건너뛰고, 크롤링 대상
    목록에도 들어가지 않는다. 그대로 두면 마지막으로 성공한 시점의 주가가
    영구히 박제되어, 거래할 수 없는 종목이 검색과 상위종목 목록에 남는다.

    다만 KRX 응답이 일시적으로 불완전할 때 멀쩡한 종목을 지우면 복구에
    재크롤링이 필요하므로, 목록이 수상하면 아무것도 지우지 않는다.

    :return: 제거된 종목 수
    """
    if len(krx_codes) < MIN_KRX_SIZE_FOR_PRUNE:
        print(f"⏩ KRX 목록이 {len(krx_codes)}개뿐이라 정리를 건너뛴다 "
              f"(최소 {MIN_KRX_SIZE_FOR_PRUNE}개 필요)", flush=True)
        return 0

    orphans = [code for code in results_dict if code not in krx_codes]
    if not orphans:
        return 0

    ratio = len(orphans) / len(results_dict)
    if ratio > MAX_PRUNE_RATIO:
        print(f"⚠️ 제거 대상이 {len(orphans)}개({ratio:.1%})로 과도하여 정리를 건너뛴다. "
              f"KRX 목록이 온전한지 확인 필요", flush=True)
        return 0

    sample = ', '.join(f"{results_dict[c].get('name', c)}({c})" for c in orphans[:5])
    for code in orphans:
        del results_dict[code]

    print(f"🧹 상장폐지 등으로 KRX 목록에 없는 {len(orphans)}개 제거: {sample}"
          f"{' 외' if len(orphans) > 5 else ''}", flush=True)
    return len(orphans)


# 네이버 실시간 시세 조회 설정.
# 한 요청에 종목코드를 콤마로 이어 붙인다. 실측으로 800개까지 문제없이
# 돌아왔지만(URL 5,661자), URL 길이 제한에 여유를 두려고 400개로 잡았다.
# 2,900종목 기준 8회 요청, 회당 0.25초 수준이다.
NAVER_PRICE_URL = 'https://polling.finance.naver.com/api/realtime/domestic/stock/'
NAVER_PRICE_CHUNK = int(os.getenv('NAVER_PRICE_CHUNK', '400'))


def fetch_naver_prices(codes: list) -> dict:
    """네이버에서 전 종목의 실시간 체결가를 받아온다.

    KRX 목록(fdr)의 Close를 쓰지 않는 이유는 그것이 실시간이 아니기 때문이다.
    fdr.StockListing('KRX')는 FinanceData/fdr_krx_data_cache 저장소에 올라온
    일별 CSV를 내려받는 구조라, 그 저장소에 커밋이 없으면 값이 갱신되지 않는다.
    실제로 2026-08-31 장중에 확인했을 때 그 CSV는 직전 거래일(08-28) 데이터를
    그대로 담고 있었고, 삼성전자 기준 실제 시세와 1.85% 차이가 났다.

    KRX(data.krx.co.kr)를 직접 호출하는 경로는 세션 쿠키 없이는 HTTP 400
    'LOGOUT'으로 막혀 쓸 수 없다.

    다만 이 엔드포인트는 네이버가 문서로 보장하는 공개 API가 아니다. 형식이
    바뀌거나 막히면 조용히 빈 dict를 반환하고, 호출부가 KRX Close로 폴백한다.

    :return: {종목코드: {'price': int, 'volume': int|None, 'open': bool,
                        'date': 'YYYY-MM-DD'|None}}
             open은 정규장 중 체결가인지 여부(marketStatus == 'OPEN'),
             date는 그 체결이 속한 거래일(localTradedAt에서 뽑는다).
    """
    result = {}
    if not codes:
        return result

    headers = {'User-Agent': 'Mozilla/5.0'}
    chunks = [codes[i:i + NAVER_PRICE_CHUNK]
              for i in range(0, len(codes), NAVER_PRICE_CHUNK)]

    for chunk in chunks:
        try:
            resp = requests.get(
                NAVER_PRICE_URL + ','.join(chunk),
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            datas = resp.json().get('datas', [])
        except Exception as e:
            # 한 묶음이 실패해도 나머지는 계속 시도한다. 전부 실패하면
            # 빈 dict가 되어 호출부가 통째로 KRX 폴백을 타게 된다.
            print(f"⚠️ 네이버 시세 조회 실패({len(chunk)}개): {type(e).__name__}", flush=True)
            continue

        for item in datas:
            code = item.get('itemCode')
            if not code:
                continue
            # ...Raw 필드는 콤마 없는 숫자다. 콤마가 섞인 표시용 필드를
            # 파싱하다 실패하는 경로를 아예 만들지 않는다.
            try:
                price = int(item['closePriceRaw'])
            except (KeyError, TypeError, ValueError):
                continue
            if price <= 0:
                continue

            try:
                volume = int(item['accumulatedTradingVolumeRaw'])
            except (KeyError, TypeError, ValueError):
                volume = None

            # localTradedAt: '2026-08-31T12:53:24.703315+09:00'
            traded_at = item.get('localTradedAt') or ''
            result[code] = {
                'price': price,
                'volume': volume,
                'open': item.get('marketStatus') == 'OPEN',
                'date': traded_at[:10] if len(traded_at) >= 10 else None,
            }

    return result


def refresh_prices(results_dict: dict, current_time) -> int:
    """전 종목의 주가와 안전마진을 갱신한다.

    가격은 네이버 실시간 시세(fetch_naver_prices)에서 받는다. KRX 목록의
    Close는 실시간이 아니라 제3자 저장소의 일별 CSV라서, 그 저장소에 커밋이
    없는 날에는 직전 거래일 값이 그대로 남는다. 네이버가 응답하지 않을 때만
    그 Close로 폴백한다.

    내재가치는 저장된 값을 그대로 쓰고 안전마진만 다시 계산한다. 내재가치는
    EPS·BPS에서 나오고 그것들은 분기마다 바뀌지만, 주가는 계속 바뀌기 때문이다.

    :return: 주가가 갱신된 종목 수
    """
    if KRX_STOCKS is None:
        print("⚠️ KRX 목록이 없어 주가 갱신을 건너뛴다", flush=True)
        return 0

    has_close = 'Close' in KRX_STOCKS.columns
    has_volume = 'Volume' in KRX_STOCKS.columns
    updated = 0
    from_naver = 0
    stamp = current_time.isoformat()

    # 결과에 들어 있는 종목만 조회하면 되지만, 신규 상장분도 KRX 목록에
    # 들어오므로 목록 전체를 대상으로 한다.
    codes = [str(c) for c in KRX_STOCKS['Code'].tolist()]
    quotes = fetch_naver_prices(codes)

    if quotes:
        # 정규장 중이면 확정 종가가 아니라 그 시점의 체결가다. 시간대를
        # 추정하지 않고 네이버가 알려주는 marketStatus를 그대로 믿는다.
        intraday = any(q['open'] for q in quotes.values())
        print(f"💹 네이버 실시간 시세 {len(quotes)}개 수신 "
              f"({'장중' if intraday else '장 마감'})", flush=True)
    else:
        intraday = False
        print("⚠️ 네이버 시세를 받지 못했다 → KRX 목록의 Close로 폴백", flush=True)

    # 주가가 어느 거래일 것인지.
    # 네이버 응답의 체결 시각을 우선 쓴다. get_latest_trading_date()는 KOSPI
    # 지수의 마지막 인덱스를 보는데, 장중에는 당일 봉이 아직 안 잡혀 직전
    # 거래일이 나온다. 그러면 오늘 실시간 시세에 어제 날짜가 붙는다.
    trading_date = next((q['date'] for q in quotes.values() if q.get('date')), None)
    if not trading_date:
        trading_date = get_latest_trading_date()
    print(f"📅 기준일: {trading_date or '확인 실패'}", flush=True)

    for row in KRX_STOCKS.itertuples(index=False):
        stock = results_dict.get(row.Code)
        if stock is None:
            continue

        quote = quotes.get(str(row.Code))
        if quote:
            price = float(quote['price'])
            volume = quote['volume']
            from_naver += 1
        elif has_close:
            # 네이버에 없는 종목(신규 상장 직후 등)은 KRX 값으로 채운다.
            try:
                price = float(row.Close)
            except (TypeError, ValueError):
                continue
            volume = None
            if has_volume:
                try:
                    volume = int(row.Volume)
                except (TypeError, ValueError):
                    volume = None
        else:
            continue

        if price <= 0:
            continue

        stock['current_price'] = price
        stock['price_updated'] = stamp      # 가져온 시각
        if volume is not None:
            stock['volume'] = volume
        if trading_date:
            stock['price_date'] = trading_date   # 그 주가가 속한 거래일

        iv = stock.get('intrinsic_value')
        if iv is not None and not (isinstance(iv, float) and math.isnan(iv)):
            stock['safety_margin'] = ((iv - price) / price) * 100

        updated += 1

    print(f"💰 주가 갱신: {updated}개 종목 "
          f"(네이버 {from_naver}개 / KRX 폴백 {updated - from_naver}개)", flush=True)
    return updated


def analyze_all_stocks(limit: int = 30, time_budget_seconds: int = None,
                       price_only: bool = False) -> list:
    """
    전체 종목에 대해 안전마진을 계산합니다.
    각 종목별로 마지막 업데이트 시간을 저장하고,
    FUNDAMENTALS_REFRESH_SECONDS가 지나지 않은 종목은 재무지표 크롤링을
    건너뜁니다. 주가는 건너뛴 종목도 포함해 매번 갱신됩니다.

    time_budget_seconds를 주면 그 시간이 지난 시점에 루프를 중단하고
    거기까지의 결과를 저장/업로드합니다. 종목은 오래된 순으로 정렬되어
    처리되므로, 중단되더라도 다음 실행이 남은 종목부터 이어서 갱신합니다.

    price_only=True면 재무지표 크롤링을 통째로 건너뛰고 주가만 갱신합니다.
    주가는 네이버 실시간 시세를 400종목씩 묶어 받으므로 전 종목이 8회
    요청·2초 안에 끝납니다. 장중에 자주 돌려도 부담이 없습니다.
    """
    started_at = time.monotonic()

    if KRX_STOCKS is None:
        print("❗ KRX_STOCKS is None. 데이터 없음", flush=True)
        return []

    total_stocks = len(KRX_STOCKS)
    print(f"\n📊 전체 {total_stocks}개 종목 분석 시작...", flush=True)

    # 기존 결과 로드 (로컬 또는 Supabase)
    existing_results = load_results_data()

    # dict로 변환하여 빠른 조회 및 업데이트
    results_dict = {item['code']: item for item in existing_results}

    stock_list = [(row['Code'], row['Name']) for _, row in KRX_STOCKS.iterrows()]
    
    # 오래된 종목부터 업데이트하기 위해 last_updated 기준으로 정렬
    stock_update_times = []
    for code, name in stock_list:
        existing_stock = results_dict.get(code)
        if existing_stock and 'last_updated' in existing_stock:
            last_updated = datetime.fromisoformat(existing_stock['last_updated'])
        else:
            # 업데이트된 적 없는 종목은 매우 오래된 시간으로 설정
            last_updated = datetime(2000, 1, 1, tzinfo=pytz.timezone("Asia/Seoul"))
        stock_update_times.append((code, name, last_updated))
    
    # 오래된 순서로 정렬
    stock_update_times.sort(key=lambda x: x[2])
    stock_list = [(code, name) for code, name, _ in stock_update_times]

    kst = pytz.timezone("Asia/Seoul")
    current_time = datetime.now(kst)

    # ── 0단계: 상장폐지 종목 정리 ─────────────────────────
    pruned = prune_delisted(results_dict, set(KRX_STOCKS['Code']))

    # ── 1단계: 주가 갱신 (묶음 요청 8회 내외) ──────────────
    # 네이버 실시간 시세를 400종목씩 묶어 받는다. 계속 바뀌는 건 주가뿐이므로
    # 전 종목을 여기서 한 번에 최신화한다.
    price_updated = refresh_prices(results_dict, current_time)

    # ── 2단계: 재무지표 크롤링 (느림, 나눠서 진행) ────────
    # EPS·BPS는 분기마다 바뀌므로 FUNDAMENTALS_REFRESH_SECONDS 주기로만
    # 다시 긁는다. 오래된 종목부터 처리하므로 중단돼도 다음 실행이 이어받는다.
    to_crawl = []
    skipped_count = 0
    if price_only:
        skipped_count = len(stock_list)
        print(f"⏩ 주가 전용 모드 → 재무지표 크롤링 건너뜀 ({skipped_count}개)", flush=True)
    else:
        for code, name in stock_list:
            existing_stock = results_dict.get(code)
            # 종목명 'Unknown'은 파싱이 통째로 실패했다는 표식이다(정상 종목명일
            # 수 없다). 2026-09 네이버 페이지 개편 때 그런 결과가 예외 없이
            # last_updated=지금 으로 저장됐고, 갱신 주기(7일) 때문에 파서를
            # 고친 뒤에도 일주일간 건너뛰어졌다. 그래서 주기와 무관하게 다시
            # 긁는다. intrinsic_value None을 기준으로 삼지 않는 이유는, 재무표가
            # 원래 없는 종목(인프라펀드 등)을 매번 재조회하게 되기 때문이다.
            broken = existing_stock is not None and existing_stock.get('name') == 'Unknown'
            if existing_stock and 'last_updated' in existing_stock and not broken:
                try:
                    last_updated = datetime.fromisoformat(existing_stock['last_updated'])
                    if (current_time - last_updated).total_seconds() < FUNDAMENTALS_REFRESH_SECONDS:
                        skipped_count += 1
                        continue
                except ValueError:
                    pass  # 파싱 불가하면 다시 크롤링
            to_crawl.append((code, name))

        print(f"🔎 재무지표 크롤링 대상 {len(to_crawl)}개 "
              f"(최신이라 건너뜀 {skipped_count}개), 동시 {CRAWL_WORKERS}개", flush=True)

    reset_treasury_circuit()

    def _safe_analyze(item):
        code, name = item
        try:
            return code, name, analyze_stock(code)
        except Exception as e:
            print(f"❗ 종목 {code} ({name}) 분석 중 오류: {type(e).__name__}", flush=True)
            return code, name, None

    analyzed_count = 0
    budget_exhausted = False

    # 묶음 단위로 처리한다. 묶음이 끝날 때마다 시간 예산을 확인하고 저장하므로
    # 스레드 간 락 없이도 결과 병합이 안전하다.
    for start in range(0, len(to_crawl), CRAWL_CHUNK):
        if time_budget_seconds is not None and (time.monotonic() - started_at) > time_budget_seconds:
            budget_exhausted = True
            print(f"⏱️ 시간 예산 {time_budget_seconds}초 소진 → 재무지표 {analyzed_count}개 갱신 후 중단", flush=True)
            break

        chunk = to_crawl[start:start + CRAWL_CHUNK]
        with ThreadPoolExecutor(max_workers=CRAWL_WORKERS) as pool:
            outcomes = list(pool.map(_safe_analyze, chunk))

        done_names = []
        for code, name, result in outcomes:
            if not result or result.get('error'):
                continue
            # 주가 관련 필드는 1단계에서 refresh_prices()가 KRX 기준으로 이미
            # 채워 놓았다. 여기서 통째로 새 dict를 만들면 price_date와
            # price_updated가 사라져, 재크롤링된 종목만 갱신 시각 표시를 잃는다.
            # 그래서 주가는 KRX 값(전 종목이 같은 기준일)을 그대로 두고,
            # 이 단계는 EPS·BPS에서 나오는 값만 갱신한다.
            entry = results_dict.get(code) or {}
            entry.update({
                'code': code,
                'name': result['stock_name'],
                'intrinsic_value': result['intrinsic_value'],
                'treasury_ratio': result['treasury_ratio'],
                'dividend_yield': result['dividend_yield'],
                'last_updated': current_time.isoformat(),
            })

            # KRX 주가가 없던 신규 종목만 크롤링으로 얻은 주가를 쓴다.
            if not entry.get('current_price'):
                entry['current_price'] = result['current_price']
                entry['price_updated'] = current_time.isoformat()

            # 내재가치가 바뀌었으므로 안전마진을 현재 주가 기준으로 다시 계산한다.
            iv, price = entry.get('intrinsic_value'), entry.get('current_price')
            if iv is not None and not (isinstance(iv, float) and math.isnan(iv)) and price:
                entry['safety_margin'] = ((iv - price) / price) * 100
            else:
                entry['safety_margin'] = result['safety_margin']

            results_dict[code] = entry
            analyzed_count += 1
            done_names.append(result['stock_name'])

        # 묶음마다 로컬 저장 (디스크 I/O, 대역폭 없음)
        results = sorted(results_dict.values(), key=margin_key, reverse=True)
        with open(RESULTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False)
        elapsed = int(time.monotonic() - started_at)
        print(f"💾 [{min(start + CRAWL_CHUNK, len(to_crawl))}/{len(to_crawl)}] "
              f"{elapsed}초 경과, 이번 묶음 {len(done_names)}개 성공", flush=True)

        # 일정 개수마다 Supabase 체크포인트 (재시작 대비)
        if analyzed_count and analyzed_count % SUPABASE_CHECKPOINT_EVERY < CRAWL_CHUNK:
            upload_to_supabase(RESULTS_FILE, list(results_dict.values()))

    # 최종 저장. 주가든 재무지표든 바뀐 게 있으면 업로드한다.
    results = sorted(results_dict.values(), key=margin_key, reverse=True)
    changed = analyzed_count > 0 or price_updated > 0 or pruned > 0
    if not changed:
        print("⏩ 변경된 내용 없음 → Supabase 업로드 생략", flush=True)
        save_results_data(results, upload=False)
    else:
        save_results_data(results)

    status = "중단(시간 예산)" if budget_exhausted else "완료"
    print(f"\n✅ 분석 {status}: 주가 {price_updated}개 / 재무지표 신규 {analyzed_count}개 "
          f"/ 정리 {pruned}개 / 누적 {len(results)}개", flush=True)
    print(f"⏩ 재무지표가 최신이라 건너뛴 종목: {skipped_count}", flush=True)

    return results[:limit]



## ── DART API: NCAV 스크리닝 ──────────────────────────────

def load_corp_code_map() -> dict:
    """DART에서 종목코드 → corp_code 매핑을 다운로드하여 반환"""
    global CORP_CODE_MAP
    if CORP_CODE_MAP is not None:
        return CORP_CODE_MAP

    if not DART_API_KEY:
        print("❗ DART_API_KEY가 설정되지 않음", flush=True)
        return {}

    try:
        import zipfile, io
        from lxml import etree

        resp = requests.get(
            'https://opendart.fss.or.kr/api/corpCode.xml',
            params={'crtfc_key': DART_API_KEY},
            timeout=60
        )
        z = zipfile.ZipFile(io.BytesIO(resp.content))
        xml_data = z.read(z.namelist()[0])
        root = etree.fromstring(xml_data)

        CORP_CODE_MAP = {}
        for corp in root.findall('.//list'):
            stock_code = corp.findtext('stock_code', '').strip()
            corp_code = corp.findtext('corp_code', '').strip()
            if stock_code:
                CORP_CODE_MAP[stock_code] = corp_code

        print(f"✅ DART corp_code 매핑 로드 완료: {len(CORP_CODE_MAP)}개 상장사", flush=True)
        return CORP_CODE_MAP
    except Exception as e:
        print(f"❌ DART corp_code 매핑 실패: {e}", flush=True)
        return {}


def get_dart_financial(corp_code: str, bsns_year: int, reprt_code: str = '11011') -> dict:
    """DART API로 단일회사 주요계정 조회"""
    try:
        resp = requests.get(
            'https://opendart.fss.or.kr/api/fnlttSinglAcnt.json',
            params={
                'crtfc_key': DART_API_KEY,
                'corp_code': corp_code,
                'bsns_year': str(bsns_year),
                'reprt_code': reprt_code
            },
            timeout=REQUEST_TIMEOUT
        )
        data = resp.json()
        if data.get('status') == '000':
            return data
        return None
    except Exception as e:
        print(f"DART API 오류 (corp_code={corp_code}): {e}")
        return None


def get_latest_financial(corp_code: str) -> dict:
    """가장 최근 사업보고서의 재무상태표 데이터를 반환"""
    now = datetime.now()
    # 사업보고서는 보통 3월 말까지 공시 → 4월부터 전년도 사용 가능
    if now.month >= 4:
        start_year = now.year - 1
    else:
        start_year = now.year - 2

    # 최대 3년 전까지 시도
    for year in range(start_year, start_year - 3, -1):
        data = get_dart_financial(corp_code, year)
        if data:
            # 연결재무제표 우선, 없으면 별도 재무제표
            bs = {}
            for item in data.get('list', []):
                if item.get('sj_nm') == '재무상태표':
                    fs = item.get('fs_nm', '')
                    acnt = item.get('account_nm', '')
                    val_str = item.get('thstrm_amount', '').replace(',', '')
                    if not val_str or val_str == '-':
                        continue
                    key = f"{fs}_{acnt}"
                    try:
                        bs[key] = int(val_str)
                    except ValueError:
                        continue

            # 연결재무제표 우선
            유동자산 = bs.get('연결재무제표_유동자산') or bs.get('재무제표_유동자산')
            부채총계 = bs.get('연결재무제표_부채총계') or bs.get('재무제표_부채총계')
            자산총계 = bs.get('연결재무제표_자산총계') or bs.get('재무제표_자산총계')
            자본총계 = bs.get('연결재무제표_자본총계') or bs.get('재무제표_자본총계')

            if 유동자산 is not None and 부채총계 is not None:
                return {
                    'bsns_year': year,
                    '유동자산': 유동자산,
                    '부채총계': 부채총계,
                    '자산총계': 자산총계,
                    '자본총계': 자본총계,
                }
    return None


def calculate_ncav_screening(time_budget_seconds: int = None) -> list:
    """
    전체 KRX 종목에 대해 NCAV 스크리닝을 수행합니다.
    NCAV = 유동자산 - 부채총계
    NCAV > 시가총액 인 종목을 필터링합니다.

    time_budget_seconds를 주면 그 시간이 지난 시점에 중단하고 거기까지의
    결과를 저장/업로드합니다. 남은 종목은 다음 실행에서 처리됩니다.
    """
    started_at = time.monotonic()
    if KRX_STOCKS is None:
        print("❗ KRX_STOCKS is None. load_krx_stocks()를 먼저 호출하세요.", flush=True)
        return []

    corp_map = load_corp_code_map()
    if not corp_map:
        return []

    # KRX에서 시가총액 매핑
    marcap_dict = {}
    for _, row in KRX_STOCKS.iterrows():
        marcap_dict[row['Code']] = {
            'name': row['Name'],
            'marcap': row.get('Marcap', 0)
        }

    # 기존 NCAV 결과 로드 (로컬 → Supabase 폴백)
    existing_ncav = {}
    existing_list = None
    if os.path.exists(NCAV_RESULTS_FILE):
        try:
            with open(NCAV_RESULTS_FILE, 'r', encoding='utf-8') as f:
                existing_list = json.load(f)
        except Exception:
            pass

    if not existing_list:
        existing_list = download_from_supabase(NCAV_RESULTS_FILE)
        if existing_list:
            try:
                with open(NCAV_RESULTS_FILE, 'w', encoding='utf-8') as f:
                    json.dump(existing_list, f, ensure_ascii=False)
                print(f"📁 NCAV: Supabase에서 다운로드 후 로컬 저장 완료", flush=True)
            except Exception:
                pass

    if existing_list:
        existing_ncav = {item['code']: item for item in existing_list}

    kst = pytz.timezone("Asia/Seoul")
    current_time = datetime.now(kst)

    # 이미 분석된 종목은 24시간 내 스킵
    codes_to_analyze = []
    for code in marcap_dict:
        if code in corp_map:
            existing = existing_ncav.get(code)
            if existing and 'last_updated' in existing:
                try:
                    last_updated = datetime.fromisoformat(existing['last_updated'])
                    # 값을 못 구한 종목은 더 짧은 주기로만 재시도한다
                    limit = NCAV_RETRY_SECONDS if existing.get('no_data') else NCAV_REFRESH_SECONDS
                    if (current_time - last_updated).total_seconds() < limit:
                        continue
                except ValueError:
                    pass  # 파싱 불가하면 다시 조회
            codes_to_analyze.append(code)

    total = len(codes_to_analyze)
    print(f"\n📊 NCAV 스크리닝 시작: {total}개 종목 분석 예정 (기존 {len(existing_ncav)}개)", flush=True)

    # 분석할 종목이 없으면 즉시 종료. 기존 결과를 다시 저장/업로드하지 않는다
    # (사업보고서는 연 1회 공시라 대부분의 실행이 여기에 해당한다)
    if total == 0:
        print("⏩ NCAV: 갱신 대상 없음 → 저장/업로드 생략", flush=True)
        return existing_list or []

    ncav_dict = dict(existing_ncav)
    analyzed = 0
    no_data = 0
    print(f"   동시 {NCAV_WORKERS}개로 조회", flush=True)

    def _safe_financial(code):
        try:
            return code, get_latest_financial(corp_map[code])
        except Exception as e:
            print(f"❗ NCAV {code} 조회 오류: {type(e).__name__}", flush=True)
            return code, None

    targets = [c for c in codes_to_analyze if corp_map.get(c)]

    # 묶음 단위로 처리해 묶음마다 예산을 확인하고 중간 저장한다.
    for start in range(0, len(targets), NCAV_CHUNK):
        if time_budget_seconds is not None and (time.monotonic() - started_at) > time_budget_seconds:
            print(f"⏱️ NCAV 시간 예산 {time_budget_seconds}초 소진 → {analyzed}개 분석 후 중단", flush=True)
            break

        chunk = targets[start:start + NCAV_CHUNK]
        with ThreadPoolExecutor(max_workers=NCAV_WORKERS) as pool:
            outcomes = list(pool.map(_safe_financial, chunk))

        for code, fin in outcomes:
            if not fin:
                # 값을 못 구한 종목도 기록해 둔다. 남기지 않으면 다음 실행에도
                # '결과 없음' 상태라 영원히 재조회된다. 보험·은행처럼 재무상태표에
                # 유동자산 구분이 없는 업종은 앞으로도 값이 나오지 않는다.
                ncav_dict[code] = {
                    'code': code,
                    'name': marcap_dict[code]['name'],
                    'ncav': None,
                    'marcap': marcap_dict[code]['marcap'],
                    'ncav_ratio': None,
                    'ncav_positive': False,
                    'no_data': True,
                    'last_updated': current_time.isoformat(),
                }
                no_data += 1
                continue
            marcap = marcap_dict[code]['marcap']
            ncav = fin['유동자산'] - fin['부채총계']

            ncav_dict[code] = {
                'code': code,
                'name': marcap_dict[code]['name'],
                'ncav': ncav,
                'marcap': marcap,
                'ncav_ratio': round(ncav / marcap * 100, 2) if marcap > 0 else None,
                '유동자산': fin['유동자산'],
                '부채총계': fin['부채총계'],
                '자산총계': fin['자산총계'],
                '자본총계': fin['자본총계'],
                'bsns_year': fin['bsns_year'],
                'ncav_positive': ncav > marcap,
                'last_updated': current_time.isoformat()
            }
            analyzed += 1

        # 묶음마다 중간 저장 (로컬 디스크, 대역폭 없음)
        results = sorted(ncav_dict.values(), key=lambda x: x.get('ncav_ratio') or float('-inf'), reverse=True)
        with open(NCAV_RESULTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False)
        elapsed = int(time.monotonic() - started_at)
        print(f"💾 NCAV [{min(start + NCAV_CHUNK, len(targets))}/{len(targets)}] "
              f"{elapsed}초 경과, 누적 분석 {analyzed}개 / 값없음 {no_data}개", flush=True)

    # 최종 저장. 실제로 분석된 종목이 있을 때만 Supabase에 업로드
    results = sorted(ncav_dict.values(), key=lambda x: x.get('ncav_ratio') or float('-inf'), reverse=True)
    with open(NCAV_RESULTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False)
    if analyzed > 0 or no_data > 0:
        upload_to_supabase(NCAV_RESULTS_FILE, results)
    else:
        print("⏩ NCAV: 신규 분석 결과 없음 → Supabase 업로드 생략", flush=True)

    ncav_positive = [r for r in results if r.get('ncav_positive')]
    print(f"\n✅ NCAV 스크리닝 완료: {len(results)}개 분석, NCAV > 시가총액: {len(ncav_positive)}개", flush=True)

    return results


if __name__ == "__main__":
    top_stocks = analyze_all_stocks()
