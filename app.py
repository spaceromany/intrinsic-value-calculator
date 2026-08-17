from flask import Flask, render_template, request, jsonify, send_file
from safety_margin_calc_naver import download_from_supabase
from datetime import datetime
import os
import threading
import time
import json
import pandas as pd
import math
from io import BytesIO
import random

# ── 데이터 로딩 (읽기 전용) ──────────────────────────
# 크롤링은 이 프로세스에서 하지 않는다. crawl.py가 스케줄러(GitHub Actions)에서
# 돌면서 Supabase Storage에 결과를 올리고, 웹앱은 그것을 읽기만 한다.
# 덕분에 웹앱의 아웃바운드 트래픽은 캐시 갱신용 다운로드가 전부다.
RESULTS_FILE = 'all_safety_margin_results.json'
NCAV_FILE = 'ncav_results.json'

# 캐시 수명(초). 크롤링이 하루 1회이므로 짧게 잡을 이유가 없다.
CACHE_TTL = int(os.getenv('CACHE_TTL', '3600'))


class RemoteDataCache:
    """Supabase Storage의 JSON을 메모리에 캐싱한다.

    - TTL이 지나면 다시 받아온다.
    - 다운로드에 실패하면 이전 데이터를 계속 제공한다. 빈 목록을 내보내
      화면이 통째로 비는 것보다 오래된 데이터가 낫다.
    - 여러 워커 스레드가 동시에 만료를 감지해도 다운로드는 한 번만 한다.
    """

    def __init__(self, filename):
        self.filename = filename
        self._data = None
        self._fetched_at = 0.0
        self._lock = threading.Lock()

    def _fresh(self):
        return self._data is not None and (time.monotonic() - self._fetched_at) < CACHE_TTL

    def _load_local(self):
        """로컬 개발 환경에 파일이 있으면 그것을 쓴다."""
        if not os.path.exists(self.filename):
            return None
        try:
            with open(self.filename, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ 로컬 {self.filename} 로드 실패: {e}", flush=True)
            return None

    def get(self):
        if self._fresh():
            return self._data

        with self._lock:
            # 락을 기다리는 동안 다른 스레드가 이미 갱신했을 수 있다
            if self._fresh():
                return self._data

            # Supabase가 원본이다. 로컬 파일은 자격증명이 없거나 Supabase가
            # 죽었을 때를 위한 폴백일 뿐이다. 순서가 반대면 한번 생긴
            # 로컬 파일이 원격 갱신을 영구히 가려버린다.
            data = download_from_supabase(self.filename)
            if data is None:
                data = self._load_local()

            if data is None:
                # 갱신 실패. 재시도 폭주를 막기 위해 타임스탬프는 갱신하고
                # 기존 데이터(있으면)를 그대로 제공한다.
                self._fetched_at = time.monotonic()
                if self._data is None:
                    print(f"❗ {self.filename} 를 가져오지 못했고 캐시도 비어 있음", flush=True)
                    return []
                print(f"⚠️ {self.filename} 갱신 실패 → 이전 데이터 유지", flush=True)
                return self._data

            self._data = data
            self._fetched_at = time.monotonic()
            return self._data


_results_cache = RemoteDataCache(RESULTS_FILE)
_ncav_cache = RemoteDataCache(NCAV_FILE)


def _latest_timestamp(data):
    """결과 항목들의 last_updated 중 가장 최근 값을 표시용 문자열로 반환."""
    stamps = [s.get('last_updated') for s in data if s.get('last_updated')]
    if not stamps:
        return ''
    try:
        return datetime.fromisoformat(max(stamps)).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return ''


def get_results_data():
    """안전마진 결과와 마지막 갱신 시각을 반환."""
    data = _results_cache.get()
    return data, _latest_timestamp(data)


def get_ncav_data():
    """NCAV 스크리닝 결과를 반환."""
    return _ncav_cache.get()


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
        _, last_update = get_results_data()
        
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
        data, last_update = get_results_data()

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
        data, _ = get_results_data()
        stocks = list(data)  # 캐시 원본 보호를 위해 복사
        
        # 안전마진 기준으로 정렬 (None이나 NaN은 맨 뒤로)
        stocks.sort(key=lambda x: float('-inf') if x.get('safety_margin') is None or math.isnan(x.get('safety_margin', float('-inf'))) else x.get('safety_margin', float('-inf')), reverse=True)
        # print("안전마진 기준으로 정렬 완료")
        
        # 배당수익률 필터링
        dividend_filter = request.args.get('dividend', type=float)
        if dividend_filter is not None:
            # print(f"배당수익률 {dividend_filter}% 이상 필터링 시작")
            filtered_stocks = []
            for stock in stocks:
                try:
                    dividend_yield = stock.get('dividend_yield')
                    if dividend_yield is not None and not math.isnan(dividend_yield) and dividend_yield >= dividend_filter:
                        filtered_stocks.append(stock)
                except (TypeError, ValueError):
                    continue
            stocks = filtered_stocks
            #print(f"배당수익률 필터링 후 {len(stocks)}개 종목 남음")
        
        # 상위 N개 종목 반환
        limit = request.args.get('limit', default=30, type=int)
        # print(f"상위 {limit}개 종목 선택")
        # 실제 결과 개수와 요청된 limit 중 작은 값 사용
        actual_limit = min(limit, len(stocks))
        
        # NaN 값을 null로 변환
        for stock in stocks:
            for key, value in stock.items():
                if isinstance(value, float) and math.isnan(value):
                    stock[key] = None

        result_stocks = stocks[:actual_limit]

        # NCAV 데이터 합치기 (우선주는 보통주 NCAV 매핑)
        ncav_data = get_ncav_data()
        if ncav_data:
            ncav_dict = {s['code']: s for s in ncav_data}
            for stock in result_stocks:
                code = stock.get('code', '')
                ncav = ncav_dict.get(code)
                if not ncav and code[-1] != '0':
                    # 우선주 → 보통주 코드(끝자리 0)로 매핑
                    ncav = ncav_dict.get(code[:-1] + '0')
                stock['ncav_ratio'] = ncav.get('ncav_ratio') if ncav else None

        result = {
            'stocks': result_stocks,
            'actual_limit': len(result_stocks)
        }
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
        
        # print(f"Processing stock: {code}, price: {purchase_price}, quantity: {purchase_quantity}")  # 디버깅 로그 추가

        # Read stock information from cache
        data, _ = get_results_data()
        stock = next((s for s in data if s['code'] == code), None)
        if not stock:
            return jsonify({'error': 'Stock not found'}), 404

        # Add purchase price and quantity to stock data
        stock['purchase_price'] = purchase_price
        stock['purchase_quantity'] = purchase_quantity
        
        # print("Returning stock data:", stock)  # 디버깅 로그 추가
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
        # print(data)
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
        # print(f"Received watchlist request with {len(watchlist)} items")
        
        if not watchlist:
            # print("Watchlist is empty")
            return jsonify([])
            
        # 캐시에서 읽고 dict로 변환하여 빠른 조회
        all_stocks, _ = get_results_data()
        stock_dict = {s['code']: s for s in all_stocks}

        stocks = []
        for item in watchlist:
            stock = stock_dict.get(item['code'])
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
                
        # print(f"Returning {len(stocks)} stocks")
        return jsonify(stocks)
    except Exception as e:
        print(f"Error in get_watchlist_data: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/ncav')
def ncav_filter():
    """NCAV 스크리닝 결과 반환"""
    try:
        data = get_ncav_data()
        if not data:
            return jsonify({'stocks': [], 'total': 0})

        # 안전마진 결과에서 배당수익률 합치기
        margin_data, _ = get_results_data()
        if margin_data:
            div_dict = {s['code']: s.get('dividend_yield') for s in margin_data}
            for stock in data:
                code = stock.get('code', '')
                stock['dividend_yield'] = div_dict.get(code) or div_dict.get(code[:-1] + '0')

        # 필터 옵션
        only_positive = request.args.get('positive', 'false').lower() == 'true'
        limit = request.args.get('limit', default=50, type=int)
        dividend_filter = request.args.get('dividend', type=float)

        if only_positive:
            data = [s for s in data if s.get('ncav_positive')]

        if dividend_filter is not None:
            data = [s for s in data if s.get('dividend_yield') is not None and s['dividend_yield'] >= dividend_filter]

        return jsonify({
            'stocks': data[:limit],
            'total': len(data),
            'ncav_positive_count': len([s for s in get_ncav_data() if s.get('ncav_positive')])
        })
    except Exception as e:
        print(f"NCAV 필터링 중 오류: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/google6b6e5fdc5623d4eb.html')
def google_verification():
    return send_file('static/google6b6e5fdc5623d4eb.html')

if __name__ == '__main__':

    app.run(host='0.0.0.0', port=7777, debug=False)