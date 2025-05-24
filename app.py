from flask import Flask, render_template, request, jsonify, send_file
from safety_margin_calc_naver import analyze_all_stocks, load_krx_stocks
from datetime import datetime
import os
import threading
import time
import json
import pandas as pd
import math
from io import BytesIO
import random

app = Flask(__name__)

# 격언 데이터 로드
def load_quotes():
    try:
        with open('investment_quotes.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data['quotes']
    except Exception as e:
        print(f"격언 데이터 로드 중 오류 발생: {e}")
        return []

def background_update():
    """백그라운드에서 주기적으로 데이터를 업데이트하는 함수"""
    while True:
        try:
            print(f"[{datetime.now()}] 데이터 업데이트 시작...")
            load_krx_stocks()
            analyze_all_stocks()
            print(f"[{datetime.now()}] 데이터 업데이트 완료")
        except Exception as e:
            print(f"[{datetime.now()}] 데이터 업데이트 중 오류 발생: {str(e)}")
        
        # 4시간 대기
        time.sleep(3600*4)

@app.route('/')
def index():
    quotes = load_quotes()
    quote = random.choice(quotes) if quotes else {
        'quote': '격언을 불러올 수 없습니다.',
        'author': '',
        'source': '',
        'original': ''
    }
    return render_template('index.html', quote=quote)

@app.route('/top-stocks')
def top_stocks():
    try:
        # 마지막 업데이트 시간 가져오기
        last_update = datetime.fromtimestamp(os.path.getmtime('all_safety_margin_results.json')).strftime('%Y-%m-%d %H:%M:%S')
        
        # 격언 데이터 로드
        quotes = load_quotes()
        quote = random.choice(quotes) if quotes else {
            'quote': '격언을 불러올 수 없습니다.',
            'author': '',
            'source': '',
            'original': ''
        }
        
        return render_template('top-stocks.html', last_update=last_update, quote=quote)
    except Exception as e:
        return render_template('top-stocks.html', error=str(e))

@app.route('/search')
def search():
    query = request.args.get('query', '').strip()
    if not query:
        return jsonify([])
    
    try:
        with open('all_safety_margin_results.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # 마지막 업데이트 시간 가져오기
        last_update = datetime.fromtimestamp(os.path.getmtime('all_safety_margin_results.json')).strftime('%Y-%m-%d %H:%M:%S')
        
        # 종목명으로 검색
        results = [stock for stock in data if query.lower() in stock['name'].lower()]
        return jsonify({
            'stocks': results[:20],  # 최대 30개 결과 반환
            'last_update': last_update
        })
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/filter')
def filter_stocks():
    try:
        print("필터링 시작...")
        # JSON 파일에서 데이터 읽기
        with open('all_safety_margin_results.json', 'r', encoding='utf-8') as f:
            stocks = json.load(f)
        print(f"총 {len(stocks)}개의 종목 데이터를 읽었습니다.")
        
        # 안전마진 기준으로 정렬
        stocks.sort(key=lambda x: float('-inf') if math.isnan(x.get('safety_margin', float('-inf'))) else x.get('safety_margin', float('-inf')), reverse=True)
        print("안전마진 기준으로 정렬 완료")
        
        # 배당수익률 필터링
        dividend_filter = request.args.get('dividend', type=float)
        if dividend_filter is not None:
            print(f"배당수익률 {dividend_filter}% 이상 필터링 시작")
            filtered_stocks = []
            for stock in stocks:
                try:
                    dividend_yield = stock.get('dividend_yield')
                    if dividend_yield is not None and not math.isnan(dividend_yield) and dividend_yield >= dividend_filter:
                        filtered_stocks.append(stock)
                except (TypeError, ValueError):
                    continue
            stocks = filtered_stocks
            print(f"배당수익률 필터링 후 {len(stocks)}개 종목 남음")
        
        # 상위 N개 종목 반환
        limit = request.args.get('limit', default=30, type=int)
        print(f"상위 {limit}개 종목 선택")
        # 실제 결과 개수와 요청된 limit 중 작은 값 사용
        actual_limit = min(limit, len(stocks))
        
        # NaN 값을 null로 변환
        for stock in stocks:
            for key, value in stock.items():
                if isinstance(value, float) and math.isnan(value):
                    stock[key] = None
        
        result = {
            'stocks': stocks[:actual_limit],
            'actual_limit': len(stocks[:actual_limit])
        }
        print(f"최종 결과: {len(result['stocks'])}개 종목 반환")
        return jsonify(result)
    except Exception as e:
        print(f"필터링 중 오류 발생: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/watchlist/add', methods=['POST'])
def add_to_watchlist():
    try:
        data = request.get_json()
        print("Received watchlist add request:", data)  # 디버깅 로그 추가
        
        if not data or 'code' not in data or 'purchase_price' not in data or 'purchase_quantity' not in data:
            print("Missing required fields in request")  # 디버깅 로그 추가
            return jsonify({'error': 'Missing required fields'}), 400

        code = data['code']
        purchase_price = float(data['purchase_price'])
        purchase_quantity = int(data['purchase_quantity'])
        
        print(f"Processing stock: {code}, price: {purchase_price}, quantity: {purchase_quantity}")  # 디버깅 로그 추가

        # Read stock information from all_safety_margin_results.json
        with open('all_safety_margin_results.json', 'r', encoding='utf-8') as f:
            stocks = json.load(f)
            stock = next((s for s in stocks if s['code'] == code), None)
            if not stock:
                print(f"Stock not found: {code}")  # 디버깅 로그 추가
                return jsonify({'error': 'Stock not found'}), 404

        # Add purchase price and quantity to stock data
        stock['purchase_price'] = purchase_price
        stock['purchase_quantity'] = purchase_quantity
        
        print("Returning stock data:", stock)  # 디버깅 로그 추가
        return jsonify(stock)

    except Exception as e:
        print(f"Error adding to watchlist: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/watchlist/remove', methods=['POST'])
def remove_from_watchlist():
    try:
        data = request.get_json()
        if not data or 'code' not in data:
            return jsonify({'error': '종목코드가 필요합니다.'}), 400

        return jsonify({'message': '관심종목이 제거되었습니다.'})

    except Exception as e:
        print(f"관심종목 제거 중 오류: {str(e)}")  # 서버 로그에 오류 출력
        return jsonify({'error': str(e)}), 500

@app.route('/watchlist/export', methods=['POST'])
def export_watchlist():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': '데이터가 필요합니다.'}), 400

        # 필터링된 종목 내보내기
        print(data)
        stocks = data['stocks']
        sheet_name = '안전마진 상위종목'
        limit = data.get('limit', 30)
        dividend_filter = data.get('dividend_filter')
        filename = f'안전마진_상위{limit}종목'
        if dividend_filter:
            filename += f'_배당수익률{dividend_filter}%이상'
        filename += '.xlsx'

        if not stocks or len(stocks) == 0:
            return jsonify({'error': '내보낼 데이터가 없습니다.'}), 400
        
        # DataFrame 생성
        df = pd.DataFrame(stocks)
        
        # 필요한 컬럼만 선택
        columns = ['code', 'name', 'current_price', 'intrinsic_value', 'safety_margin', 'treasury_ratio', 'dividend_yield', 'last_updated']
        df = df[columns]
        df.columns = ['종목코드', '종목명', '현재가', '내재가치', '안전마진', '자사주비율', '배당수익률', '마지막 업데이트']
        
        # 마지막 업데이트 시간을 한국 시간으로 변환
        df['마지막 업데이트'] = pd.to_datetime(df['마지막 업데이트']).dt.strftime('%Y-%m-%d %H:%M:%S')
        
        # 숫자 포맷팅 적용
        numeric_columns = ['현재가', '내재가치']
        for col in numeric_columns:
            if col in df.columns:
                df[col] = df[col].apply(lambda x: f"{x:,.0f}")
        
        # 소수점이 필요한 컬럼 포맷팅
        decimal_columns = ['안전마진', '자사주비율', '배당수익률']
        for col in decimal_columns:
            if col in df.columns:
                df[col] = df[col].apply(lambda x: f"{x:.2f}")
        
        # 엑셀 파일 생성
        output = BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df.to_excel(writer, index=False, sheet_name=sheet_name)
            
            # 워크시트 가져오기
            worksheet = writer.sheets[sheet_name]
            
            # 컬럼 너비 자동 조정
            for idx, col in enumerate(df.columns):
                max_length = max(
                    df[col].astype(str).apply(len).max(),
                    len(col)
                )
                worksheet.set_column(idx, idx, max_length + 2)
        
        output.seek(0)
        
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=filename
        )
    except Exception as e:
        print(f"엑셀 내보내기 중 오류: {str(e)}")
        return jsonify({'error': f'엑셀 내보내기 중 오류가 발생했습니다: {str(e)}'}), 500

@app.route('/watchlist/data', methods=['POST'])
def get_watchlist_data():
    try:
        data = request.get_json()
        watchlist = data.get('watchlist', [])
        print(f"Received watchlist request with {len(watchlist)} items")
        
        if not watchlist:
            print("Watchlist is empty")
            return jsonify([])
            
        stocks = []
        for item in watchlist:
            print(f"Processing stock: {item['code']}")
            # all_safety_margin_results.json에서 직접 데이터 읽기
            with open('all_safety_margin_results.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
                stock = next((s for s in data if s['code'] == item['code']), None)
                if stock:
                    stock_data = {
                        'code': stock['code'],
                        'name': stock['name'],
                        'current_price': stock['current_price'],
                        'intrinsic_value': stock['intrinsic_value'],
                        'safety_margin': stock['safety_margin'],
                        'treasury_ratio': stock.get('treasury_ratio', None),
                        'dividend_yield': stock.get('dividend_yield', None),
                        'last_update': stock.get('last_updated', None)
                    }
                    stocks.append(stock_data)
                else:
                    print(f"Stock not found: {item['code']}")
                
        print(f"Returning {len(stocks)} stocks")
        return jsonify(stocks)
    except Exception as e:
        print(f"Error in get_watchlist_data: {str(e)}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    # 백그라운드 업데이트 스레드 시작
    update_thread = threading.Thread(target=background_update, daemon=True)
    update_thread.start()
    
    # Flask 앱 실행
    app.run(host='0.0.0.0', port=7777, debug=False) 