from flask import Flask, render_template, request, jsonify, send_file
from safety_margin_calc_naver import get_historical_metrics, calculate_intrinsic_value, search_stock_codes, analyze_stock, get_top_safety_margin_stocks
from datetime import datetime
import os
import threading
import time
import json
import pandas as pd
import io

app = Flask(__name__)

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
    return render_template('index.html')

@app.route('/top-stocks')
def top_stocks():
    try:
        with open('all_safety_margin_results.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # 안전마진 기준으로 정렬
        sorted_data = sorted(data, key=lambda x: float(x['safety_margin']) if x['safety_margin'] is not None else float('-inf'), reverse=True)
        
        # 상위 30개만 선택
        top_stocks = sorted_data[:30]
        
        # 마지막 업데이트 시간 가져오기
        last_update = datetime.fromtimestamp(os.path.getmtime('all_safety_margin_results.json')).strftime('%Y-%m-%d %H:%M:%S')
        
        return render_template('calculator.html', top_stocks=top_stocks, last_update=last_update)
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

@app.route('/filter')
def filter_stocks():
    limit = request.args.get('limit', default=30, type=int)
    try:
        with open('all_safety_margin_results.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # 안전마진 기준으로 정렬
        sorted_data = sorted(data, key=lambda x: float(x['safety_margin']) if x['safety_margin'] is not None else float('-inf'), reverse=True)
        
        # 상위 N개 선택
        filtered_data = sorted_data[:limit]
        
        return jsonify(filtered_data)
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/export_excel')
def export_excel():
    try:
        limit = request.args.get('limit', default=30, type=int)
        
        # JSON 파일 읽기
        with open('all_safety_margin_results.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # 안전마진 기준으로 정렬
        sorted_data = sorted(data, key=lambda x: float(x['safety_margin']) if x['safety_margin'] is not None else float('-inf'), reverse=True)
        
        # 상위 N개 선택
        filtered_data = sorted_data[:limit]
        
        # DataFrame 생성
        df = pd.DataFrame(filtered_data)
        
        # 컬럼명 한글로 변경
        df = df.rename(columns={
            'code': '종목코드',
            'name': '종목명',
            'current_price': '현재가',
            'intrinsic_value': '내재가치',
            'safety_margin': '안전마진',
            'treasury_ratio': '자사주비율',
            'market_cap': '시가총액'
        })
        
        # 숫자 포맷팅
        df['현재가'] = df['현재가'].apply(lambda x: f"{x:,.0f}")
        df['내재가치'] = df['내재가치'].apply(lambda x: f"{x:,.0f}")
        df['안전마진'] = df['안전마진'].apply(lambda x: f"{x:.1f}%")
        df['자사주비율'] = df['자사주비율'].apply(lambda x: f"{x:.1f}%" if x is not None else 'N/A')
        df['시가총액'] = df['시가총액'].apply(lambda x: f"{x:,.0f}" if x is not None else 'N/A')
        
        # Excel 파일 생성
        filename = f'safety_margin_top_{limit}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
        with pd.ExcelWriter(filename, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='안전마진 상위 종목')
            
            # 열 너비 자동 조정
            worksheet = writer.sheets['안전마진 상위 종목']
            for idx, col in enumerate(df.columns):
                max_length = max(
                    df[col].astype(str).apply(len).max(),
                    len(col)
                )
                worksheet.column_dimensions[chr(65 + idx)].width = max_length + 2
        
        return send_file(filename, as_attachment=True)
    except Exception as e:
        return jsonify({'error': str(e)})

if __name__ == '__main__':
    # 백그라운드 업데이트 스레드 시작
    update_thread = threading.Thread(target=background_update, daemon=True)
    update_thread.start()
    
    # Flask 앱 실행
    app.run(host='0.0.0.0', port=7777, debug=True) 