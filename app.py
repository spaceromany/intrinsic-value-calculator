from flask import Flask, render_template, request, jsonify
from safety_margin_calc_naver import get_historical_metrics, calculate_intrinsic_value, search_stock_codes

app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/search', methods=['POST'])
def search():
    company_name = request.form.get('company_name')
    if not company_name:
        return jsonify({'error': '종목명을 입력해주세요.'})
    
    try:
        results = search_stock_codes(company_name)
        if not results:
            return jsonify({'error': '검색 결과가 없습니다.'})
        return jsonify({'results': results})
    except Exception as e:
        return jsonify({'error': f'검색 중 오류가 발생했습니다: {str(e)}'})

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
    app.run(debug=True) 