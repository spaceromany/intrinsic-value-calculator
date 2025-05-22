from flask import Flask, render_template, request, jsonify
from safety_margin_calc_naver import get_historical_metrics, calculate_intrinsic_value, search_stock_codes, analyze_stock, get_top_safety_margin_stocks
from datetime import datetime
import os
import threading
import time

app = Flask(__name__)

def background_update():
    """백그라운드에서 주기적으로 데이터를 업데이트하는 함수"""
    while True:
        try:
            print(f"[{datetime.now()}] 데이터 업데이트 시작...")
            get_top_safety_margin_stocks(force_update=True)
            print(f"[{datetime.now()}] 데이터 업데이트 완료")
        except Exception as e:
            print(f"[{datetime.now()}] 데이터 업데이트 중 오류 발생: {str(e)}")
        
        # 1시간 대기
        time.sleep(3600)

@app.route('/')
def index():
    # 상위 30개 종목 가져오기
    top_stocks = get_top_safety_margin_stocks()
    
    # safety_margin_results.json 파일의 수정 시간을 마지막 업데이트 시간으로 사용
    last_update = datetime.fromtimestamp(os.path.getmtime('safety_margin_results.json')).strftime("%Y-%m-%d %H:%M")
    
    return render_template('index.html', top_stocks=top_stocks, last_update=last_update)

@app.route('/search')
def search():
    query = request.args.get('query', '')
    if not query:
        return jsonify([])
    
    results = search_stock_codes(query)
    return jsonify(results)

@app.route('/analyze')
def analyze():
    code = request.args.get('code')
    if not code:
        return jsonify({'error': '종목코드가 필요합니다.'})
    
    try:
        result = analyze_stock(code)
        if result.get('error'):
            return jsonify(result)
            
        # 재무지표 데이터 포맷팅
        historical_data = []
        if result.get('historical_data'):
            for period, data in result['historical_data'].items():
                historical_data.append({
                    'period': period,
                    'PBR': float(data['PBR']),
                    'EPS': float(data['EPS']),
                    'BPS': float(data['BPS'])
                })
        
        return jsonify({
            'stock_name': result['stock_name'],
            'current_price': result['current_price'],
            'intrinsic_value': result['intrinsic_value'],
            'safety_margin': result['safety_margin'],
            'treasury_ratio': result['treasury_ratio'],
            'historical_data': historical_data
        })
        
    except Exception as e:
        return jsonify({'error': f'오류가 발생했습니다: {str(e)}'})

@app.route('/calculate', methods=['POST'])
def calculate():
    ticker = request.form.get('ticker')
    if not ticker:
        return jsonify({'error': '종목코드를 입력해주세요.'})
    
    try:
        # 재무 데이터 가져오기
        df, stock_name, current_price, treasury_stock = get_historical_metrics(ticker)
        
        if df.empty:
            return jsonify({'error': '재무 데이터를 가져올 수 없습니다.'})
        
        # 내재가치 계산
        intrinsic_value = calculate_intrinsic_value(df, treasury_stock)
        if intrinsic_value is None:
            return jsonify({'error': '내재가치를 계산할 수 없습니다.'})
        
        # 안전마진 계산
        safety_margin = None
        if current_price and intrinsic_value:
            safety_margin = ((intrinsic_value - current_price) / current_price) * 100
        
        # 재무지표 데이터 포맷팅
        df = df.reset_index().rename(columns={'index': 'period'})  # 인덱스를 컬럼으로 변환
        financial_data = []
        for _, row in df.iterrows():
            financial_data.append({
                'period': row['period'],
                'PBR': row['PBR'],
                'EPS': row['EPS'],
                'BPS': row['BPS']
            })
        
        return jsonify({
            'stock_name': stock_name,
            'current_price': current_price,
            'intrinsic_value': intrinsic_value,
            'safety_margin': safety_margin,
            'treasury_stock': treasury_stock,
            'financial_data': financial_data
        })
        
    except Exception as e:
        return jsonify({'error': f'오류가 발생했습니다: {str(e)}'})

if __name__ == '__main__':
    # 백그라운드 업데이트 스레드 시작
    update_thread = threading.Thread(target=background_update, daemon=True)
    update_thread.start()
    
    # Flask 앱 실행
    app.run(host='0.0.0.0', port=7777, debug=False) 