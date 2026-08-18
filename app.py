from flask import Flask, render_template, request, jsonify, send_file, Response
# 크롤러 모듈(safety_margin_calc_naver)이 아니라 storage에서 가져온다.
# 크롤러 모듈은 requests·lxml·FinanceDataReader·tqdm을 최상단에서 임포트하는데,
# 웹앱은 그중 무엇도 쓰지 않는다.
from storage import download_from_supabase
from datetime import datetime
import os
import threading
import time
import json
import math
from io import BytesIO
import random
import sys

# 표준 출력 설정 두 가지를 고정한다.
# 1) encoding: Windows 콘솔 기본값(cp949)은 로그의 이모지를 표현하지 못해
#    UnicodeEncodeError를 던진다. 이 예외가 요청 처리 중에 터지면 응답이
#    500이 된다(Supabase 실패 로그를 찍다가 응답 자체가 죽는다).
# 2) line_buffering: 출력이 파이프로 갈 때 파이썬은 블록 버퍼링을 하므로
#    프로세스가 죽으면 로그가 통째로 유실된다. 배포 환경에서 장애를
#    진단하려면 줄 단위로 즉시 나가야 한다.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)

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
    """표시할 데이터 기준 시점을 문자열로 반환.

    price_date(주가가 속한 거래일)를 최우선으로 쓴다. 가져온 시각을 쓰면
    휴장일에 "오늘 15:54 갱신"으로 보이지만 실제 주가는 직전 거래일 종가다.
    사용자가 알아야 하는 건 언제 받아왔는지가 아니라 언제 시세인지다.

    재무지표(last_updated)는 분기 데이터라 최대 7일 주기로만 갱신되므로
    신선도 표시에 쓰면 주가가 최신인데도 오래된 것처럼 보인다.

    장중에 받아온 값은 확정 종가가 아니므로 '종가'라고 쓰지 않는다.
    이때는 거래일보다 몇 시 기준인지가 더 유용해서 시각까지 보여준다.
    """
    dates = [s.get('price_date') for s in data if s.get('price_date')]
    if dates:
        if any(s.get('price_intraday') for s in data):
            stamps = [s.get('price_updated') for s in data if s.get('price_updated')]
            if stamps:
                try:
                    hhmm = datetime.fromisoformat(max(stamps)).strftime('%H:%M')
                    return f"{max(dates)} {hhmm} 장중"
                except ValueError:
                    pass
            return f"{max(dates)} 장중"
        return f"{max(dates)} 종가"

    stamps = [s.get('price_updated') or s.get('last_updated') for s in data]
    stamps = [s for s in stamps if s]
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

# 검색엔진에 노출되는 절대 URL의 기준. 호스팅을 옮기거나 커스텀 도메인을
# 붙일 때 환경변수만 바꾸면 canonical·og:url·sitemap이 모두 따라온다.
SITE_URL = os.getenv('SITE_URL', 'https://intrinsic-value-calculator.onrender.com').rstrip('/')


@app.context_processor
def inject_site_url():
    return {'site_url': SITE_URL}

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
    # pandas는 이 엔드포인트에서만 쓰인다. 최상단에서 임포트하면 모든 요청이
    # 그 비용을 치르는데, 서버리스에서는 콜드 스타트마다 1초 안팎이 붙는다.
    # 엑셀 내보내기는 드물게 쓰이므로 필요할 때만 로드한다.
    import pandas as pd

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
                    'last_update': stock.get('last_updated', None),
                    'price_date': stock.get('price_date', None),
                    'price_intraday': stock.get('price_intraday', False)
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

        # NCAV를 구할 수 없는 종목은 목록에서 제외한다. 보험·은행처럼
        # 재무상태표에 유동자산 구분이 없는 업종이며, 크롤러가 매 실행
        # 재조회하지 않도록 마커만 남겨둔 항목이다.
        data = [s for s in data if not s.get('no_data')]

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


# 크롤러는 사이트 루트의 /robots.txt 와 /sitemap.xml 만 조회한다.
# 정적 파일로 두면 /static/robots.txt 로만 접근되어 아무도 읽지 않는다.
@app.route('/robots.txt')
def robots_txt():
    return Response(render_template('robots.txt'), mimetype='text/plain')


@app.route('/sitemap.xml')
def sitemap_xml():
    # lastmod는 실제 데이터 갱신일을 쓴다. 크롤링이 하루 1회이므로
    # changefreq=daily 와 일관되고, 없으면 오늘 날짜로 대체한다.
    _, last_update = get_results_data()
    lastmod = (last_update or '')[:10] or datetime.now().strftime('%Y-%m-%d')
    return Response(render_template('sitemap.xml', lastmod=lastmod),
                    mimetype='application/xml')

if __name__ == '__main__':

    app.run(host='0.0.0.0', port=7777, debug=False)