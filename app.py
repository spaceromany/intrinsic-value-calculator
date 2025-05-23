from flask import Flask, render_template, request, jsonify, send_file
from safety_margin_calc_naver import get_historical_metrics, calculate_intrinsic_value, search_stock_codes, analyze_stock, get_top_safety_margin_stocks
from datetime import datetime
import os
import threading
import time
import json
import pandas as pd
import io
import math
from io import BytesIO
import random

app = Flask(__name__)

# 가치투자 격언 목록
INVESTMENT_QUOTES = [
    {
        "quote": "시장은 단기적으로는 투표 기계이지만, 장기적으로는 저울과 같다.",
        "original": "In the short run, the market is a voting machine but in the long run, it is a weighing machine.",
        "author": "벤저민 그레이엄",
        "source": "The Intelligent Investor"
    },
    {
        "quote": "가격은 당신이 지불하는 것이고, 가치는 당신이 얻는 것이다.",
        "original": "Price is what you pay. Value is what you get.",
        "author": "워렌 버핏",
        "source": "Berkshire Hathaway Annual Meeting"
    },
    {
        "quote": "위험은 당신이 무엇을 하는지 모르는 데서 온다.",
        "original": "Risk comes from not knowing what you're doing.",
        "author": "워렌 버핏",
        "source": "Berkshire Hathaway Annual Meeting"
    },
    {
        "quote": "시장이 비관적일 때 낙관적이 되고, 낙관적일 때 비관적이 되라.",
        "original": "Be fearful when others are greedy and greedy when others are fearful.",
        "author": "존 템플턴",
        "source": "Templeton's Investment Principles"
    },
    {
        "quote": "가장 위험한 것은 위험을 모르는 것이다.",
        "original": "The most dangerous thing is not knowing the risk.",
        "author": "피터 린치",
        "source": "One Up On Wall Street"
    },
    {
        "quote": "주식 시장에서 성공하는 비결은 시장이 두려워할 때 두려워하지 않는 것이다.",
        "original": "The key to success in the stock market is not being afraid when others are afraid.",
        "author": "워렌 버핏",
        "source": "Berkshire Hathaway Annual Meeting"
    },
    {
        "quote": "가격과 가치의 차이를 이해하는 것이 투자의 핵심이다.",
        "original": "Understanding the difference between price and value is the key to investment.",
        "author": "벤저민 그레이엄",
        "source": "The Intelligent Investor"
    },
    {
        "quote": "장기적으로 주식 시장은 기업의 실적을 반영한다.",
        "original": "In the long run, the stock market reflects the performance of businesses.",
        "author": "워렌 버핏",
        "source": "Berkshire Hathaway Annual Meeting"
    },
    {
        "quote": "투자는 단순한 것이지만, 쉽지는 않다.",
        "original": "Investment is simple, but not easy.",
        "author": "워렌 버핏",
        "source": "Berkshire Hathaway Annual Meeting"
    },
    {
        "quote": "시장의 변동성을 두려워하지 마라. 그것은 당신의 친구가 될 수 있다.",
        "original": "Don't fear market volatility. It can be your friend.",
        "author": "벤저민 그레이엄",
        "source": "The Intelligent Investor"
    },
    {
        "quote": "가치투자는 시장의 비효율성을 이용하는 것이다.",
        "original": "Value investing is exploiting market inefficiencies.",
        "author": "세스 클라만",
        "source": "Margin of Safety"
    },
    {
        "quote": "안전마진은 투자의 핵심이다.",
        "original": "Margin of safety is the cornerstone of investment.",
        "author": "벤저민 그레이엄",
        "source": "The Intelligent Investor"
    },
    {
        "quote": "시장이 비합리적일 때 합리적으로 행동하라.",
        "original": "Be rational when the market is irrational.",
        "author": "워렌 버핏",
        "source": "Berkshire Hathaway Annual Meeting"
    },
    {
        "quote": "투자의 목표는 적절한 가격에 좋은 기업을 사는 것이다.",
        "original": "The goal of investment is to buy good companies at fair prices.",
        "author": "워렌 버핏",
        "source": "Berkshire Hathaway Annual Meeting"
    },
    {
        "quote": "시장의 변동성은 기회를 제공한다.",
        "original": "Market volatility provides opportunities.",
        "author": "피터 린치",
        "source": "One Up On Wall Street"
    }
]

def get_random_quote():
    """랜덤으로 격언을 반환하는 함수"""
    return random.choice(INVESTMENT_QUOTES)

def background_update():
    """백그라운드에서 주기적으로 데이터를 업데이트하는 함수"""
    while True:
        try:
            print(f"[{datetime.now()}] 데이터 업데이트 시작...")
            get_top_safety_margin_stocks()
            print(f"[{datetime.now()}] 데이터 업데이트 완료")
        except Exception as e:
            print(f"[{datetime.now()}] 데이터 업데이트 중 오류 발생: {str(e)}")
        
        # 1시간 대기
        time.sleep(3600*4)

@app.route('/')
def index():
    quote = get_random_quote()
    return render_template('index.html', quote=quote)

@app.route('/top-stocks')
def top_stocks():
    try:
        # 마지막 업데이트 시간 가져오기
        last_update = datetime.fromtimestamp(os.path.getmtime('all_safety_margin_results.json')).strftime('%Y-%m-%d %H:%M:%S')
        
        return render_template('calculator.html', last_update=last_update)
    except Exception as e:
        return render_template('calculator.html', error=str(e))

@app.route('/search')
def search():
    query = request.args.get('query', '').strip()
    if not query:
        return jsonify([])
    
    try:
        with open('all_safety_margin_results.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # 종목명으로 검색
        results = [stock for stock in data if query.lower() in stock['name'].lower()]
        return jsonify(results[:10])  # 최대 10개 결과만 반환
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/analyze')
def analyze():
    code = request.args.get('code')
    if not code:
        return jsonify({'error': '종목코드가 필요합니다.'})
    
    try:
        # all_safety_margin_results.json에서 데이터 읽기
        with open('all_safety_margin_results.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # 해당 종목 찾기
        stock = next((item for item in data if item['code'] == code), None)
        if not stock:
            return jsonify({'error': '종목을 찾을 수 없습니다.'})
            
        return jsonify({
            'stock_name': stock['name'],
            'current_price': stock['current_price'],
            'intrinsic_value': stock['intrinsic_value'],
            'safety_margin': stock['safety_margin'],
            'treasury_ratio': stock.get('treasury_ratio', None),
            'dividend_yield': stock.get('dividend_yield', None),
            'last_updated': stock.get('last_updated', None)
        })
        
    except Exception as e:
        return jsonify({'error': f'오류가 발생했습니다: {str(e)}'})

@app.route('/filter')
def filter_stocks():
    try:
        # JSON 파일에서 데이터 읽기
        with open('all_safety_margin_results.json', 'r', encoding='utf-8') as f:
            stocks = json.load(f)
        
        # 안전마진 기준으로 정렬
        stocks.sort(key=lambda x: float('-inf') if math.isnan(x.get('safety_margin', float('-inf'))) else x.get('safety_margin', float('-inf')), reverse=True)
        
        # 배당수익률 필터링
        dividend_filter = request.args.get('dividend', type=float)
        if dividend_filter is not None:
            filtered_stocks = []
            for stock in stocks:
                try:
                    dividend_yield = stock.get('dividend_yield')
                    if dividend_yield is not None and not math.isnan(dividend_yield) and dividend_yield >= dividend_filter:
                        filtered_stocks.append(stock)
                except (TypeError, ValueError):
                    continue
            stocks = filtered_stocks
        
        # 상위 N개 종목 반환
        limit = request.args.get('limit', default=30, type=int)
        return jsonify(stocks[:limit])
    except Exception as e:
        print(f"필터링 중 오류 발생: {str(e)}")  # 서버 로그에 오류 출력
        return jsonify({'error': str(e)}), 500

@app.route('/export')
def export_excel():
    try:
        # JSON 파일에서 데이터 읽기
        with open('all_safety_margin_results.json', 'r', encoding='utf-8') as f:
            stocks = json.load(f)
        
        # 안전마진 기준으로 정렬
        stocks.sort(key=lambda x: float('-inf') if math.isnan(x.get('safety_margin', float('-inf'))) else x.get('safety_margin', float('-inf')), reverse=True)
        
        # 배당수익률 필터링
        dividend_filter = request.args.get('dividend', type=float)
        if dividend_filter is not None:
            filtered_stocks = []
            for stock in stocks:
                try:
                    dividend_yield = stock.get('dividend_yield')
                    if dividend_yield is not None and not math.isnan(dividend_yield) and dividend_yield >= dividend_filter:
                        filtered_stocks.append(stock)
                except (TypeError, ValueError):
                    continue
            stocks = filtered_stocks
        
        # UI에서 선택한 limit 값으로 제한
        limit = request.args.get('limit', default=30, type=int)
        stocks = stocks[:limit]
        
        # DataFrame 생성
        df = pd.DataFrame(stocks)
        
        # 필요한 컬럼만 선택하고 순서 지정
        columns = ['code', 'name', 'current_price', 'intrinsic_value', 'safety_margin', 'treasury_ratio', 'dividend_yield', 'last_updated']
        df = df[columns]
        
        # 컬럼명 한글로 변경
        df.columns = ['종목코드', '종목명', '현재가', '내재가치', '안전마진', '자사주비율', '배당수익률', '마지막 업데이트']
        
        # 마지막 업데이트 시간을 한국 시간으로 변환
        df['마지막 업데이트'] = pd.to_datetime(df['마지막 업데이트']).dt.strftime('%Y-%m-%d %H:%M:%S')
        
        # 엑셀 파일 생성
        output = BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df.to_excel(writer, index=False, sheet_name='안전마진 상위종목')
        
        output.seek(0)
        
        # 파일명에 배당수익률 필터 정보와 limit 정보 추가
        filename = f'안전마진_상위{limit}종목'
        if dividend_filter is not None:
            filename += f'_배당수익률{dividend_filter}%이상'
        filename += '.xlsx'
        
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=filename
        )
    except Exception as e:
        return str(e), 500

if __name__ == '__main__':
    # 백그라운드 업데이트 스레드 시작
    update_thread = threading.Thread(target=background_update, daemon=True)
    update_thread.start()
    
    # Flask 앱 실행
    app.run(host='0.0.0.0', port=7777, debug=True) 