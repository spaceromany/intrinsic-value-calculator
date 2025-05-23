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
    try:
        # JSON 파일에서 데이터 읽기
        with open('all_safety_margin_results.json', 'r', encoding='utf-8') as f:
            results = json.load(f)
        top_stocks = results[:30]  # 상위 30개만 선택
        
        # 마지막 업데이트 시간
        last_update = datetime.fromtimestamp(os.path.getmtime('all_safety_margin_results.json')).strftime("%Y-%m-%d %H:%M")
        
        return render_template('index.html', top_stocks=top_stocks, last_update=last_update)
        
    except Exception as e:
        print(f"데이터 로드 중 오류 발생: {str(e)}")
        return render_template('index.html', top_stocks=[], last_update="오류 발생")

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

@app.route('/export_excel')
def export_excel():
    try:
        # 필터 파라미터 가져오기
        limit = int(request.args.get('limit', 30))  # 기본값 30
        
        # JSON 파일에서 데이터 읽기
        with open('all_safety_margin_results.json', 'r', encoding='utf-8') as f:
            results = json.load(f)
        
        # 안전마진 기준으로 정렬
        results.sort(key=lambda x: x['safety_margin'] if not pd.isna(x['safety_margin']) else float('-inf'), reverse=True)
        
        # 상위 N개 선택
        filtered_results = results[:limit]
        
        # DataFrame 생성
        df = pd.DataFrame(filtered_results)
        
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
        df['안전마진'] = df['안전마진'].apply(lambda x: f"{x:+.1f}%")
        df['자사주비율'] = df['자사주비율'].apply(lambda x: f"{x:.1f}%")
        if '시가총액' in df.columns:
            df['시가총액'] = df['시가총액'].apply(lambda x: f"{x:,.0f}")
        
        # 엑셀 파일 생성
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df.to_excel(writer, sheet_name='안전마진 분석', index=False)
            
            # 워크시트 가져오기
            worksheet = writer.sheets['안전마진 분석']
            
            # 컬럼 너비 조정
            for idx, col in enumerate(df.columns):
                max_length = max(df[col].astype(str).apply(len).max(), len(col)) + 2
                worksheet.set_column(idx, idx, max_length)
        
        output.seek(0)
        
        # 파일명 생성
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"안전마진분석_Top{limit}_{timestamp}.xlsx"
        
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=filename
        )
        
    except Exception as e:
        print(f"엑셀 내보내기 중 오류 발생: {str(e)}")
        return jsonify({'error': '엑셀 파일 생성 중 오류가 발생했습니다.'}), 500

@app.route('/filter')
def filter_stocks():
    try:
        # 필터 파라미터 가져오기
        limit = int(request.args.get('limit', 30))  # 기본값 30
        
        # JSON 파일에서 데이터 읽기
        with open('all_safety_margin_results.json', 'r', encoding='utf-8') as f:
            results = json.load(f)
        
        # 안전마진 기준으로 정렬
        results.sort(key=lambda x: x['safety_margin'] if not pd.isna(x['safety_margin']) else float('-inf'), reverse=True)
        
        # 상위 N개 선택
        filtered_results = results[:limit]
        
        return jsonify(filtered_results)
        
    except Exception as e:
        print(f"필터링 중 오류 발생: {str(e)}")
        return jsonify({'error': '데이터 필터링 중 오류가 발생했습니다.'}), 500

if __name__ == '__main__':
    # 백그라운드 업데이트 스레드 시작
    update_thread = threading.Thread(target=background_update, daemon=True)
    update_thread.start()
    
    # Flask 앱 실행
    app.run(host='0.0.0.0', port=7777, debug=True) 