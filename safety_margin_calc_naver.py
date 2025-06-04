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
from bs4 import BeautifulSoup
import time
import pytz
# KRX 종목 목록 파일 경로
KRX_STOCKS_FILE = 'krx_stocks.json'
KRX_STOCKS = None

def load_krx_stocks():
    """KRX 종목 목록을 파일에서 로드하거나 업데이트"""
    global KRX_STOCKS
    
    # 파일이 존재하는지 확인
    if os.path.exists(KRX_STOCKS_FILE):
        # 파일의 수정 시간 확인
        file_time = datetime.fromtimestamp(os.path.getmtime(KRX_STOCKS_FILE))
        now = datetime.now()
        
        # 하루가 지났는지 확인
        if now - file_time < timedelta(days=1):
            # 하루가 지나지 않았다면 파일에서 로드
            try:
                with open(KRX_STOCKS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    KRX_STOCKS = pd.DataFrame(data)
                return
            except Exception as e:
                print(f"KRX 종목 목록 파일 로드 중 오류 발생: {e}")
    
    # 파일이 없거나 하루가 지났다면 새로 다운로드
    try:
        KRX_STOCKS = fdr.StockListing('KRX')
        # DataFrame을 JSON으로 저장
        with open(KRX_STOCKS_FILE, 'w', encoding='utf-8') as f:
            json.dump(KRX_STOCKS.to_dict('records'), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"KRX 종목 목록 다운로드 중 오류 발생: {e}")
        KRX_STOCKS = None

def get_stock_data(ticker: str) -> tuple:
    """
    주식 데이터를 한 번의 API 호출로 가져옵니다.
    현재가와 재무제표 데이터를 포함합니다.
    """
    try:
        # 메인 페이지에서 재무제표 데이터 가져오기
        url = f"https://finance.naver.com/item/main.naver?code={ticker}"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=5)
        resp.raise_for_status()
        doc = html.fromstring(resp.text)
        
        # 종목명 추출
        stock_name_node = doc.xpath('//*[@id="middle"]/div[1]/div[1]/h2/a')
        stock_name = stock_name_node[0].text_content().strip() if stock_name_node else "Unknown"
        
        # 현재가 추출 (시세 페이지에서 가져오기)
        current_price = None
        try:
            price_url = f"https://finance.naver.com/item/sise.naver?code={ticker}"
            price_resp = requests.get(price_url, headers=headers)
            price_resp.raise_for_status()
            price_doc = html.fromstring(price_resp.text)
            current_price_node = price_doc.xpath('//*[@id="_nowVal"]')
            if current_price_node:
                current_price = float(current_price_node[0].text_content().strip().replace(',', ''))
        except Exception as e:
            print(f"현재가 조회 중 오류 발생: {e}")

        # 재무지표 XPath
        xpaths = {
            'PBR': [
                '//*[@id="content"]/div[5]/div[1]/table/tbody/tr[13]/td[1]',
                '//*[@id="content"]/div[5]/div[1]/table/tbody/tr[13]/td[2]',
                '//*[@id="content"]/div[5]/div[1]/table/tbody/tr[13]/td[3]',
            ],
            'EPS': [
                '//*[@id="content"]/div[5]/div[1]/table/tbody/tr[10]/td[1]',
                '//*[@id="content"]/div[5]/div[1]/table/tbody/tr[10]/td[2]',
                '//*[@id="content"]/div[5]/div[1]/table/tbody/tr[10]/td[3]',
            ],
            'BPS': [
                '//*[@id="content"]/div[5]/div[1]/table/tbody/tr[12]/td[1]',
                '//*[@id="content"]/div[5]/div[1]/table/tbody/tr[12]/td[2]',
                '//*[@id="content"]/div[5]/div[1]/table/tbody/tr[12]/td[3]',
            ]
        }

        def extract(xpath: str):
            node = doc.xpath(xpath)
            if not node:
                return None
            txt = node[0].text_content().strip().replace(",", "").replace("−", "-")
            try:
                return float(txt) if '.' in txt else int(txt)
            except ValueError:
                return None

        # 데이터 추출
        data = {key: [extract(xp) for xp in xps] for key, xps in xpaths.items()}
        periods = ["3년전", "2년전", "직전년도"]
        df = pd.DataFrame(data, index=periods)

        return df, stock_name, current_price

    except Exception as e:
        print(f"주식 데이터 조회 중 오류 발생: {e}")
        return pd.DataFrame(), "Unknown", None


def get_treasury_stock_info(ticker: str) -> dict:
    """자사주 정보 조회"""
    try:
        url = f"https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd={ticker}"
        headers = {
            'Referer': 'https://finance.naver.com',
            'User-Agent': 'Mozilla/5.0'
        }
        resp = requests.get(url, headers=headers, timeout=5)
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

def get_historical_metrics(ticker: str) -> pd.DataFrame:
    """
    네이버 금융 메인 페이지에서
    3년 전·2년 전·직전년도 PBR·EPS·BPS를
    고정 XPath로 추출합니다.
    """
    url = f"https://finance.naver.com/item/main.naver?code={ticker}"
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    
    doc = html.fromstring(resp.text)
    
    # 종목명 추출
    stock_name_node = doc.xpath('//*[@id="middle"]/div[1]/div[1]/h2/a')
    stock_name = stock_name_node[0].text_content().strip() if stock_name_node else "Unknown"
    
    # 현재가 추출 (시세 페이지에서 가져오기)
    current_price = None
    try:
        price_url = f"https://finance.naver.com/item/sise.naver?code={ticker}"
        price_resp = requests.get(price_url, headers=headers)
        price_resp.raise_for_status()
        price_doc = html.fromstring(price_resp.text)
        current_price_node = price_doc.xpath('//*[@id="_nowVal"]')
        if current_price_node:
            current_price = float(current_price_node[0].text_content().strip().replace(',', ''))
    except Exception as e:
        print(f"현재가 조회 중 오류 발생: {e}")

    # 자사주 정보 추출
    treasury_stock_info = get_treasury_stock_info(ticker)
    
    # 기간 레이블
    periods = ["3년전", "2년전", "직전년도"]

    # 고정 XPath 목록 (td[1], td[2], td[3] 순서)
    pbr_xps = [
        '//*[@id="content"]/div[5]/div[1]/table/tbody/tr[13]/td[1]',
        '//*[@id="content"]/div[5]/div[1]/table/tbody/tr[13]/td[2]',
        '//*[@id="content"]/div[5]/div[1]/table/tbody/tr[13]/td[3]',
    ]
    eps_xps = [
        '//*[@id="content"]/div[5]/div[1]/table/tbody/tr[10]/td[1]',
        '//*[@id="content"]/div[5]/div[1]/table/tbody/tr[10]/td[2]',
        '//*[@id="content"]/div[5]/div[1]/table/tbody/tr[10]/td[3]',
    ]
    bps_xps = [
        '//*[@id="content"]/div[5]/div[1]/table/tbody/tr[12]/td[1]',
        '//*[@id="content"]/div[5]/div[1]/table/tbody/tr[12]/td[2]',
        '//*[@id="content"]/div[5]/div[1]/table/tbody/tr[12]/td[3]',
    ]

    def extract(xpath: str):
        node = doc.xpath(xpath)
        if not node:
            return None
        txt = node[0].text_content().strip().replace(",", "").replace("−", "-")
        try:
            # 소수점이 있는 경우 float로, 없는 경우 int로 변환
            if '.' in txt:
                return float(txt)
            else:
                return int(txt)
        except ValueError:
            return None

    # 값 추출
    data = {"PBR": [], "EPS": [], "BPS": []}
    for xp_pbr, xp_eps, xp_bps in zip(pbr_xps, eps_xps, bps_xps):
        data["PBR"].append(extract(xp_pbr))
        data["EPS"].append(extract(xp_eps))
        data["BPS"].append(extract(xp_bps))

    # DataFrame 생성
    df = pd.DataFrame(data, index=periods)
    return df, stock_name, current_price, treasury_stock_info

def analyze_stock(ticker: str) -> dict:
    """
    종목코드를 입력받아 내재가치와 안전마진을 계산하여 반환합니다.
    
    Args:
        ticker (str): 종목코드 (예: '006125')
        
    Returns:
        dict: {
            'stock_name': str,          # 종목명
            'current_price': float,     # 현재가
            'intrinsic_value': float,   # 내재가치
            'safety_margin': float,     # 안전마진 (%)
            'treasury_shares': float,   # 자사주 보유 주식수
            'treasury_ratio': float,    # 자사주 지분율 (%)
            'historical_data': dict,    # 과거 재무지표 데이터
            'error': str               # 오류 발생 시 오류 메시지
        }
    """
    try:
        # 네이버 금융에서 데이터 가져오기
        url = f'https://finance.naver.com/item/main.naver?code={ticker}'
        response = requests.get(url)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 종목명 가져오기
        stock_name = soup.select_one('#middle > div.h_company > div.wrap_company > h2 > a').text.strip()
        
        # 현재가 가져오기
        current_price = float(soup.select_one('#chart_area > div.rate_info > div > p.no_today > em > span.blind').text.strip().replace(',', ''))
        
        # 배당수익률 가져오기
        dividend_yield = None
        try:
            dividend_yield_text = soup.select_one('#_dvr').text.strip()
            if dividend_yield_text and dividend_yield_text != 'N/A':
                dividend_yield = float(dividend_yield_text.replace('%', ''))
        except:
            pass
        
        # 재무 데이터 가져오기
        df, _, _, treasury_stock = get_historical_metrics(ticker)
        
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

    # 기존 결과 로드
    existing_results = []
    if os.path.exists('all_safety_margin_results.json'):
        try:
            with open('all_safety_margin_results.json', 'r', encoding='utf-8') as f:
                existing_results = json.load(f)
        except Exception as e:
            print(f"❗ 기존 결과 파일 로드 중 오류 발생: {e}", flush=True)

    # dict로 변환하여 빠른 조회 가능하게
    results_dict = {item['code']: item for item in existing_results}
    results = existing_results.copy()

    stock_list = [(row['Code'], row['Name']) for _, row in KRX_STOCKS.iterrows()]

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

                # dict와 list 동시 업데이트
                results_dict[code] = stock_data
                if existing_stock:
                    for j, item in enumerate(results):
                        if item['code'] == code:
                            results[j] = stock_data
                            break
                else:
                    results.append(stock_data)

                # 10개마다 저장
                if (i + 1) % 10 == 0:
                    results.sort(key=margin_key, reverse=True)
                    with open('all_safety_margin_results.json', 'w', encoding='utf-8') as f:
                        json.dump(results, f, ensure_ascii=False, indent=2)
                    print(f"💾 {i + 1}/{total_stocks} 개 종목 분석 결과 저장, 종목: {code_list}", flush=True)
                    code_list = []

        except Exception as e:
            print(f"❗ 종목 {code} ({name}) 분석 중 오류 발생: {e}", flush=True)
            results.sort(key=margin_key, reverse=True)
            with open('all_safety_margin_results.json', 'w', encoding='utf-8') as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            continue

    # 최종 저장
    results.sort(key=margin_key, reverse=True)
    with open('all_safety_margin_results.json', 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 분석 완료: {len(results)}개 종목 분석 성공", flush=True)
    print(f"⏩ 건너뛴 종목 수: {skipped_count}", flush=True)
    print(f"📈 상위 {limit}개 종목 반환", flush=True)

    return results[:limit]



if __name__ == "__main__":
    # 테스트 코드
    top_stocks = analyze_all_stocks()
    
#     print("\n=== 안전마진 상위 종목 ===")
#     for idx, stock in enumerate(top_stocks, 1):
#         print(f"\n{idx}위: {stock['name']} ({stock['code']})")
#         print(f"현재가: {stock['current_price']:,.0f}원")
#         print(f"내재가치: {stock['intrinsic_value']:,.0f}원")
#         print(f"안전마진: {stock['safety_margin']:+.1f}%")
#         if stock['treasury_ratio'] > 0:
#             print(f"자사주비율: {stock['treasury_ratio']:.1f}%")
#         if stock['dividend_yield'] is not None:
#             print(f"배당수익률: {stock['dividend_yield']:.2f}%")
