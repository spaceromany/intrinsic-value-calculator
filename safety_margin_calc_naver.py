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
import pytz
from supabase import create_client, Client
from dotenv import load_dotenv

# 환경 변수 로드
load_dotenv()

# Supabase 설정
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')
SUPABASE_BUCKET = 'stock-data'  # Storage 버킷 이름

supabase: Client = None

def get_supabase_client():
    """Supabase 클라이언트 반환 (싱글톤)"""
    global supabase
    if supabase is None and SUPABASE_URL and SUPABASE_KEY:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return supabase

def upload_to_supabase(file_name: str, data: list) -> bool:
    """JSON 데이터를 Supabase Storage에 업로드"""
    try:
        client = get_supabase_client()
        if client is None:
            print("Supabase 클라이언트 없음, 로컬 저장만 수행")
            return False

        json_bytes = json.dumps(data, ensure_ascii=False).encode('utf-8')

        # 기존 파일 삭제 후 업로드 (upsert)
        try:
            client.storage.from_(SUPABASE_BUCKET).remove([file_name])
        except:
            pass  # 파일이 없으면 무시

        result = client.storage.from_(SUPABASE_BUCKET).upload(
            file_name,
            json_bytes,
            file_options={"content-type": "application/json"}
        )
        print(f"✅ Supabase Storage 업로드 완료: {file_name}")
        return True
    except Exception as e:
        print(f"❌ Supabase Storage 업로드 실패: {e}")
        return False

def download_from_supabase(file_name: str) -> list:
    """Supabase Storage에서 JSON 데이터 다운로드"""
    try:
        client = get_supabase_client()
        if client is None:
            return None

        response = client.storage.from_(SUPABASE_BUCKET).download(file_name)
        data = json.loads(response.decode('utf-8'))
        print(f"✅ Supabase Storage에서 다운로드 완료: {file_name} ({len(data)}개 항목)")
        return data
    except Exception as e:
        print(f"⚠️ Supabase Storage 다운로드 실패: {e}")
        return None

# DART OpenAPI 설정
DART_API_KEY = os.getenv('DART_API_KEY')
CORP_CODE_MAP = None  # 종목코드 → corp_code 매핑 딕셔너리
NCAV_RESULTS_FILE = 'ncav_results.json'

# KRX 종목 목록 파일 경로
KRX_STOCKS_FILE = 'krx_stocks.json'
RESULTS_FILE = 'all_safety_margin_results.json'
KRX_STOCKS = None

def load_krx_stocks():
    """KRX 종목 목록을 파일에서 로드하거나 업데이트"""
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
    if os.path.exists(KRX_STOCKS_FILE):
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
            KRX_STOCKS = new_stocks[['Code', 'Name', 'Marcap']].copy()
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


def get_treasury_stock_info(ticker: str) -> dict:
    """자사주 정보 조회"""
    try:
        url = f"https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd={ticker}"
        headers = {
            'Referer': 'https://finance.naver.com',
            'User-Agent': 'Mozilla/5.0'
        }
        resp = requests.get(url, headers=headers, timeout=30)
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
        
        return {'shares': shares, 'ratio': ratio}
        
    except Exception as e:
        print(f"자사주 정보 조회 중 오류 발생: {e}")
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



def analyze_stock(ticker: str) -> dict:
    """
    종목코드를 입력받아 내재가치와 안전마진을 계산하여 반환합니다.
    main.naver 1회 + wisereport 1회 = 총 2회 요청으로 모든 데이터를 수집합니다.
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0"}

        # 1) main.naver 한 번만 요청 (lxml)
        url = f"https://finance.naver.com/item/main.naver?code={ticker}"
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        doc = html.fromstring(resp.text)

        # 종목명
        stock_name_node = doc.xpath('//*[@id="middle"]/div[1]/div[1]/h2/a')
        stock_name = stock_name_node[0].text_content().strip() if stock_name_node else "Unknown"

        # 현재가
        current_price = None
        price_node = doc.xpath('//*[@id="chart_area"]//p[contains(@class,"no_today")]/em/span[contains(@class,"blind")]')
        if price_node:
            current_price = float(price_node[0].text_content().strip().replace(',', ''))

        # 배당수익률
        dividend_yield = None
        try:
            dvr_node = doc.xpath('//*[@id="_dvr"]')
            if dvr_node:
                dvr_text = dvr_node[0].text_content().strip()
                if dvr_text and dvr_text != 'N/A':
                    dividend_yield = float(dvr_text.replace('%', ''))
        except:
            pass

        # 재무지표 (PBR, EPS, BPS) — 같은 doc에서 추출
        def extract(xpath: str):
            node = doc.xpath(xpath)
            if not node:
                return None
            txt = node[0].text_content().strip().replace(",", "").replace("−", "-")
            try:
                return float(txt) if '.' in txt else int(txt)
            except ValueError:
                return None

        periods = ["3년전", "2년전", "직전년도"]
        data = {
            "PBR": [
                extract('//*[@id="content"]/div[5]/div[1]/table/tbody/tr[13]/td[1]'),
                extract('//*[@id="content"]/div[5]/div[1]/table/tbody/tr[13]/td[2]'),
                extract('//*[@id="content"]/div[5]/div[1]/table/tbody/tr[13]/td[3]'),
            ],
            "EPS": [
                extract('//*[@id="content"]/div[5]/div[1]/table/tbody/tr[10]/td[1]'),
                extract('//*[@id="content"]/div[5]/div[1]/table/tbody/tr[10]/td[2]'),
                extract('//*[@id="content"]/div[5]/div[1]/table/tbody/tr[10]/td[3]'),
            ],
            "BPS": [
                extract('//*[@id="content"]/div[5]/div[1]/table/tbody/tr[12]/td[1]'),
                extract('//*[@id="content"]/div[5]/div[1]/table/tbody/tr[12]/td[2]'),
                extract('//*[@id="content"]/div[5]/div[1]/table/tbody/tr[12]/td[3]'),
            ]
        }
        df = pd.DataFrame(data, index=periods)

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

def save_results_data(results: list):
    """결과 데이터를 로컬과 Supabase Storage에 저장"""
    # 로컬 저장
    try:
        with open(RESULTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ 로컬 저장 실패: {e}")

    # Supabase Storage 업로드
    upload_to_supabase(RESULTS_FILE, results)

def analyze_all_stocks(limit: int = 30) -> list:
    """
    전체 종목에 대해 안전마진을 계산합니다.
    각 종목별로 마지막 업데이트 시간을 저장하고,
    1시간이 지나지 않은 종목은 건너뜁니다.
    """

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
    # current_time = datetime.now()
    skipped_count = 0
    code_list = []

    for i, (code, name) in enumerate(stock_list):
        # print(f"🔍 [{i+1}/{total_stocks}] {code} - {name} 처리 시작", flush=True)

        existing_stock = results_dict.get(code)

        if existing_stock and 'last_updated' in existing_stock:
            last_updated = datetime.fromisoformat(existing_stock['last_updated'])
            if (current_time - last_updated).total_seconds() < 3600:
                skipped_count += 1
                continue

        try:
            result = analyze_stock(code)
            # print(f"✅ 종목 {code} ({name}) 분석 완료", flush=True)
            code_list.append(result['stock_name'])
            # time.sleep(10)

            if not result.get('error'):
                stock_data = {
                    'code': code,
                    'name': result['stock_name'],
                    'current_price': result['current_price'],
                    'intrinsic_value': result['intrinsic_value'],
                    'safety_margin': result['safety_margin'],
                    'treasury_ratio': result['treasury_ratio'],
                    'dividend_yield': result['dividend_yield'],
                    'last_updated': current_time.isoformat()
                }

                results_dict[code] = stock_data

                # 10개마다 로컬 저장
                if (i + 1) % 10 == 0:
                    results = sorted(results_dict.values(), key=margin_key, reverse=True)
                    with open(RESULTS_FILE, 'w', encoding='utf-8') as f:
                        json.dump(results, f, ensure_ascii=False)
                    print(f"💾 {i + 1}/{total_stocks} 개 종목 분석 결과 저장, 종목: {code_list}", flush=True)
                    code_list = []

                # 100개마다 Supabase 업로드
                if (i + 1) % 100 == 0:
                    upload_to_supabase(RESULTS_FILE, list(results_dict.values()))

        except Exception as e:
            print(f"❗ 종목 {code} ({name}) 분석 중 오류 발생: {e}", flush=True)
            continue

    # 최종 저장 (로컬 + Supabase)
    results = sorted(results_dict.values(), key=margin_key, reverse=True)
    save_results_data(results)

    print(f"\n✅ 분석 완료: {len(results)}개 종목 분석 성공", flush=True)
    print(f"⏩ 건너뛴 종목 수: {skipped_count}", flush=True)
    print(f"📈 상위 {limit}개 종목 반환", flush=True)

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
            timeout=30
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


def calculate_ncav_screening() -> list:
    """
    전체 KRX 종목에 대해 NCAV 스크리닝을 수행합니다.
    NCAV = 유동자산 - 부채총계
    NCAV > 시가총액 인 종목을 필터링합니다.
    """
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
                last_updated = datetime.fromisoformat(existing['last_updated'])
                if (current_time - last_updated).total_seconds() < 86400:  # 24시간
                    continue
            codes_to_analyze.append(code)

    total = len(codes_to_analyze)
    print(f"\n📊 NCAV 스크리닝 시작: {total}개 종목 분석 예정 (기존 {len(existing_ncav)}개)", flush=True)

    ncav_dict = dict(existing_ncav)
    analyzed = 0

    for i, code in enumerate(codes_to_analyze):
        corp_code = corp_map.get(code)
        if not corp_code:
            continue

        fin = get_latest_financial(corp_code)
        if fin:
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

        # 50개마다 중간 저장
        if (i + 1) % 50 == 0:
            results = sorted(ncav_dict.values(), key=lambda x: x.get('ncav_ratio') or float('-inf'), reverse=True)
            with open(NCAV_RESULTS_FILE, 'w', encoding='utf-8') as f:
                json.dump(results, f, ensure_ascii=False)
            print(f"💾 NCAV [{i+1}/{total}] 중간 저장 ({analyzed}개 분석 완료)", flush=True)

    # 최종 저장
    results = sorted(ncav_dict.values(), key=lambda x: x.get('ncav_ratio') or float('-inf'), reverse=True)
    with open(NCAV_RESULTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False)
    upload_to_supabase(NCAV_RESULTS_FILE, results)

    ncav_positive = [r for r in results if r.get('ncav_positive')]
    print(f"\n✅ NCAV 스크리닝 완료: {len(results)}개 분석, NCAV > 시가총액: {len(ncav_positive)}개", flush=True)

    return results


if __name__ == "__main__":
    top_stocks = analyze_all_stocks()
