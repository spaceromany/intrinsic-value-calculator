#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
from lxml import html
import pandas as pd
from pykrx import stock
from datetime import datetime
import FinanceDataReader as fdr

# 전역 변수로 KRX 종목 목록 저장
try:
    KRX_STOCKS = fdr.StockListing('KRX')
except Exception as e:
    print(f"KRX 종목 목록 로드 중 오류 발생: {str(e)}")
    KRX_STOCKS = None

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
    
    # 현재가 추출
    current_price_node = doc.xpath('//*[@id="content"]/div[6]/table/tbody/tr[1]/td[1]')
    current_price = None
    if current_price_node:
        try:
            current_price = float(current_price_node[0].text_content().strip().replace(',', ''))
        except ValueError:
            current_price = None

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

import requests
import pandas as pd
from io import StringIO

def get_treasury_stock_info(ticker: str) -> dict:
    """
    Wisereport AJAX(c1010001)로부터 '자사주' 보유 주식수와 지분율을 파싱해 반환합니다.
    
    :param ticker: 종목코드 (예: '009970')
    :return: {'shares': int, 'ratio': float}
    """
    # 1) AJAX 엔드포인트 (c1010001) 호출
    url = (
        "https://navercomp.wisereport.co.kr"
        f"/v2/company/c1010001.aspx?cmp_cd={ticker}"
    )
    headers = {
        'Referer': 'https://finance.naver.com',
        'User-Agent': 'Mozilla/5.0'
    }
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()  # :contentReference[oaicite:0]{index=0}

    # 2) HTML → StringIO → pandas.read_html
    html_buf = StringIO(resp.text)
    dfs = pd.read_html(html_buf, encoding='utf-8')  # :contentReference[oaicite:1]{index=1}

    # 3) '자사주'가 포함된 테이블 찾기
    df_target = None
    for df in dfs:
        # 첫 번째 컬럼에 '자사주' 텍스트가 있는지 검사
        first_col = df.columns[0]
        if df[first_col].astype(str).str.contains('자사주', na=False).any():
            df_target = df
            break
    if df_target is None:
        return {'shares': 0, 'ratio': 0}

    # 4) 필요한 3개 컬럼만 슬라이스
    df3 = df_target.iloc[:, :3]
    col0, col1, col2 = df3.columns

    # 5) '자사주' 행 추출
    row = df3[df3[col0].astype(str).str.contains('자사주', na=False)].iloc[0]

    # 6) 문자열 정제 및 숫자 변환
    shares_text = str(row[col1]).replace(',', '').strip()
    ratio_text  = str(row[col2]).replace('%', '').replace(',', '').strip()

    shares = float(shares_text)
    ratio  = float(ratio_text)

    return {'shares': shares, 'ratio': ratio}


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

if __name__ == "__main__":
    ticker = "006125"  # 예: 영원무역홀딩스
    df, stock_name, current_price, treasury_stock = get_historical_metrics(ticker)
    print(f"\n종목명: {stock_name}")
    if current_price:
        print(f"현재가: {current_price:,.0f}원")
    print("\n과거 재무지표:")
    print(df)
    
    if treasury_stock:
        print("\n자사주 정보:")
        if treasury_stock['shares']:
            print(f"보유 주식수: {treasury_stock['shares']:,}주")
        if treasury_stock['ratio']:
            print(f"지분율: {treasury_stock['ratio']:.2f}%")
    
    intrinsic_value = calculate_intrinsic_value(df, treasury_stock)
    if intrinsic_value is not None:
        print(f"\n내재가치: {intrinsic_value:,.0f}원")
        if current_price:
            safety_margin = (intrinsic_value - current_price) / current_price * 100
            print(f"안전마진: {safety_margin:+.1f}%")
    else:
        print("\n내재가치를 계산할 수 없습니다.")
