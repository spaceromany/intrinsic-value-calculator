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
from supabase import create_client, Client
from dotenv import load_dotenv
import hashlib
import uuid
from anonymous_ids import generate_anonymous_id
# from chatbot import StockChatbot
# Load environment variables
load_dotenv()

def background_update():
    """백그라운드에서 데이터를 업데이트하는 함수"""
    try:
        while True:
            print(f"[{datetime.now()}] 백그라운드 데이터 업데이트 시작...")
            load_krx_stocks()
            print(f"KRX 데이터 업데이트 완료...")
            analyze_all_stocks()
            print(f"[{datetime.now()}] 백그라운드 데이터 업데이트 완료")
            time.sleep(60)  # 1분
    except Exception as e:
        print(f"[{datetime.now()}] 백그라운드 데이터 업데이트 중 오류 발생: {str(e)}")

app = Flask(__name__)
update_thread = threading.Thread(target=background_update)
update_thread.daemon = True  # 메인 프로그램이 종료되면 스레드도 함께 종료
update_thread.start()
# Supabase 설정
supabase: Client = create_client(
    os.getenv('SUPABASE_URL'),
    os.getenv('SUPABASE_KEY')
)

supabase.postgrest.headers.update({
    "Prefer": "return=minimal"
})
# StockChatbot 인스턴스 생성
# stock_chatbot = StockChatbot(supabase)

def get_device_id():
    """디바이스 ID 생성 또는 가져오기"""
    device_id = request.headers.get('X-Device-ID')
    if not device_id:
        return None
    return device_id

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
        # print("필터링 시작...")
        # JSON 파일에서 데이터 읽기
        with open('all_safety_margin_results.json', 'r', encoding='utf-8') as f:
            stocks = json.load(f)
        # print(f"총 {len(stocks)}개의 종목 데이터를 읽었습니다.")
        
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
        
        result = {
            'stocks': stocks[:actual_limit],
            'actual_limit': len(stocks[:actual_limit])
        }
        # print(f"최종 결과: {len(result['stocks'])}개 종목 반환")
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

        # Read stock information from all_safety_margin_results.json
        with open('all_safety_margin_results.json', 'r', encoding='utf-8') as f:
            stocks = json.load(f)
            stock = next((s for s in stocks if s['code'] == code), None)
            if not stock:
                # print(f"Stock not found: {code}")  # 디버깅 로그 추가
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
            
        stocks = []
        for item in watchlist:
            # print(f"Processing stock: {item['code']}")
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
                # else:
                #     print(f"Stock not found: {item['code']}")
                
        # print(f"Returning {len(stocks)} stocks")
        return jsonify(stocks)
    except Exception as e:
        print(f"Error in get_watchlist_data: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/valuetalk')
def valuetalk():
    quotes = load_quotes()
    quote = random.choice(quotes) if quotes else {
        'quote': '격언을 불러올 수 없습니다.',
        'author': '',
        'source': '',
        'original': ''
    }
    return render_template('valuetalk.html', quote=quote)

@app.route('/valuetalk/anonymous-id', methods=['POST'])
def get_anonymous_id():
    try:
        device_id = get_device_id()
        if not device_id:
            return jsonify({'error': '디바이스 ID가 필요합니다.'}), 400
        
        anonymous_id = generate_anonymous_id(device_id)
        return jsonify({'anonymous_id': anonymous_id})
    except Exception as e:
        print(f"익명 ID 발급 중 오류: {str(e)}")
        return jsonify({'error': str(e)}), 500

from postgrest.exceptions import APIError

@app.route('/valuetalk/post', methods=['POST'])
def create_post():
    try:
        # ───────── 0) 입력/검증 ─────────
        data = request.get_json()
        content      = data.get('content')
        stocks       = data.get('stocks', [])
        anonymous_id = data.get('anonymous_id')
        device_id    = get_device_id()

        if not content or not stocks:
            return jsonify({'error': '내용과 종목 정보가 필요합니다.'}), 400
        if not anonymous_id:
            return jsonify({'error': '익명 ID가 필요합니다.'}), 400
        if not device_id:
            return jsonify({'error': '디바이스 ID가 필요합니다.'}), 400

        current_time = datetime.now().astimezone().isoformat()

        # stocks를 JSONB 형식으로 변환
        stocks_json = [{'code': code} for code in stocks]

        post_row = {
            'content':      content,
            'anonymous_id': anonymous_id,
            'device_id':    device_id,
            'created_at':   current_time,
            'stocks':       stocks_json  # JSONB 형식으로 저장
        }

        # ───────── 1) INSERT ─────────
        try:
            insert_resp = supabase.table('posts').insert(post_row).execute()
            if not insert_resp.data:
                raise Exception('게시물 저장에 실패했습니다.')
            
            post_id = insert_resp.data[0]['id']
            return jsonify({'message': '게시물이 작성되었습니다.', 'post_id': post_id}), 201

        except Exception as err:
            app.logger.error(f"posts INSERT error: {err}")
            return jsonify({'error': '게시물 저장에 실패했습니다.'}), 500

    except Exception as e:
        app.logger.exception("게시물 작성 중 예외")
        return jsonify({'error': str(e)}), 500

@app.route('/valuetalk/posts')
def get_posts():
    try:
        filter_type = request.args.get('filter', 'all')
        device_id = get_device_id()
        page = request.args.get('page', 1, type=int)
        per_page = 20
        
        # 기본 쿼리
        query = supabase.table('posts').select('*').order('created_at', desc=True)
        
        # 필터 적용
        if filter_type == 'following':
            try:
                # URL에서 관심종목 데이터 가져오기
                watchlist_json = request.args.get('watchlist', '[]')
                watchlist = json.loads(watchlist_json)
                # print(f"받은 관심종목 데이터: {len(watchlist)}개 항목")
                
                # 관심종목 코드 목록 추출
                watchlist_codes = [item['code'] for item in watchlist]
                # print(f"관심종목 코드: {watchlist_codes}")
                
                # 관심종목이 있는 게시물만 필터링
                if watchlist_codes:
                    # Supabase에서 모든 게시물 가져오기
                    result = query.execute()
                    filtered_posts = []
                    
                    for post in result.data:
                        if isinstance(post['stocks'], list):
                            # 게시물의 종목 코드와 관심종목 코드 비교
                            post_stocks = [stock['code'] if isinstance(stock, dict) else stock for stock in post['stocks']]
                            # print(f"게시물 {post['id']} 종목: {post_stocks}")
                            if any(code in watchlist_codes for code in post_stocks):
                                filtered_posts.append(post)
                    
                    # print(f"필터링된 게시물 수: {len(filtered_posts)}")
                    result.data = filtered_posts
                else:
                    print("관심종목이 없습니다.")
                    result = type('obj', (object,), {'data': []})
            except Exception as e:
                print(f"관심종목 필터링 중 오류 발생: {str(e)}")
                return jsonify([])
        elif filter_type == 'my':
            # 현재 디바이스의 게시물만 필터링
            result = query.eq('device_id', device_id).execute()
        elif filter_type == 'stock':
            # 특정 종목의 게시물만 필터링
            stock_code = request.args.get('code')
            if not stock_code:
                return jsonify([])
            
            # Supabase에서 모든 게시물 가져오기
            result = query.execute()
            filtered_posts = []
            
            for post in result.data:
                if isinstance(post['stocks'], list):
                    # 게시물의 종목 코드와 요청된 종목 코드 비교
                    post_stocks = [stock['code'] if isinstance(stock, dict) else stock for stock in post['stocks']]
                    if stock_code in post_stocks:
                        filtered_posts.append(post)
            
            # 페이지네이션 적용
            total_count = len(filtered_posts)
            start_idx = (page - 1) * per_page
            end_idx = start_idx + per_page
            result.data = filtered_posts[start_idx:end_idx]
            
            # 종목 정보 가져오기
            stock_codes = {stock_code}  # 현재 필터링된 종목 코드만 포함
            post_ids = [post['id'] for post in result.data]
            
            # 종목 정보 로드
            stock_info = {}
            try:
                with open('all_safety_margin_results.json', 'r', encoding='utf-8') as f:
                    stocks_data = json.load(f)
                    for stock in stocks_data:
                        if stock['code'] == stock_code:
                            stock_info[stock['code']] = stock['name']
                            break
            except Exception as e:
                print(f"종목 정보 로드 중 오류 발생: {str(e)}")
            
            # 모든 게시물의 좋아요 정보를 한 번에 가져오기
            likes_result = supabase.table('likes').select('*').in_('post_id', post_ids).execute()
            likes_by_post = {}
            for like in likes_result.data:
                if like['post_id'] not in likes_by_post:
                    likes_by_post[like['post_id']] = []
                likes_by_post[like['post_id']].append(like)
            
            # 사용자의 좋아요 정보를 한 번에 가져오기
            user_likes_result = supabase.table('likes').select('*').in_('post_id', post_ids).eq('device_id', device_id).execute()
            user_liked_posts = {like['post_id'] for like in user_likes_result.data}
            
            # 모든 게시물의 댓글 정보를 한 번에 가져오기
            comments_result = supabase.table('comments').select('*').in_('post_id', post_ids).execute()
            comments_by_post = {}
            for comment in comments_result.data:
                if comment['post_id'] not in comments_by_post:
                    comments_by_post[comment['post_id']] = []
                comments_by_post[comment['post_id']].append(comment)
            
            # 게시물에 종목명, 좋아요, 댓글 정보 추가
            for post in result.data:
                if isinstance(post['stocks'], list):
                    post['stocks'] = [{'code': code if isinstance(code, str) else code['code'], 
                                     'name': stock_info.get(code if isinstance(code, str) else code['code'], 
                                                           code if isinstance(code, str) else code['code'])} 
                                    for code in post['stocks']]
                
                # 현재 디바이스의 게시물인지 표시
                post['is_owner'] = post['device_id'] == device_id
                
                # 좋아요 정보 추가
                post_likes = likes_by_post.get(post['id'], [])
                post['likes'] = {
                    'count': len(post_likes),
                    'is_liked': post['id'] in user_liked_posts
                }
                
                # 댓글 정보 추가
                post_comments = comments_by_post.get(post['id'], [])
                post['comments'] = {
                    'count': len(post_comments),
                    'items': post_comments
                }
            
            return jsonify({
                'posts': result.data,
                'has_more': end_idx < total_count,
                'total_count': total_count
            })
        else:
            result = query.execute()
        
        if not result.data:
            return jsonify({
                'posts': [],
                'has_more': False,
                'total_count': 0
            })
        
        # 종목 정보 가져오기
        stock_codes = set()
        post_ids = [post['id'] for post in result.data]
        for post in result.data:
            if isinstance(post['stocks'], list):
                stock_codes.update([stock['code'] if isinstance(stock, dict) else stock for stock in post['stocks']])
        
        # 종목 정보 로드
        stock_info = {}
        if stock_codes:
            try:
                with open('all_safety_margin_results.json', 'r', encoding='utf-8') as f:
                    stocks_data = json.load(f)
                    for stock in stocks_data:
                        if stock['code'] in stock_codes:
                            stock_info[stock['code']] = stock['name']
            except Exception as e:
                print(f"종목 정보 로드 중 오류 발생: {str(e)}")
        
        # 모든 게시물의 좋아요 정보를 한 번에 가져오기
        likes_result = supabase.table('likes').select('*').in_('post_id', post_ids).execute()
        likes_by_post = {}
        for like in likes_result.data:
            if like['post_id'] not in likes_by_post:
                likes_by_post[like['post_id']] = []
            likes_by_post[like['post_id']].append(like)
        
        # 사용자의 좋아요 정보를 한 번에 가져오기
        user_likes_result = supabase.table('likes').select('*').in_('post_id', post_ids).eq('device_id', device_id).execute()
        user_liked_posts = {like['post_id'] for like in user_likes_result.data}
        
        # 모든 게시물의 댓글 정보를 한 번에 가져오기
        comments_result = supabase.table('comments').select('*').in_('post_id', post_ids).execute()
        comments_by_post = {}
        for comment in comments_result.data:
            if comment['post_id'] not in comments_by_post:
                comments_by_post[comment['post_id']] = []
            comments_by_post[comment['post_id']].append(comment)
        
        # 게시물에 종목명, 좋아요, 댓글 정보 추가
        for post in result.data:
            if isinstance(post['stocks'], list):
                post['stocks'] = [{'code': code if isinstance(code, str) else code['code'], 
                                 'name': stock_info.get(code if isinstance(code, str) else code['code'], 
                                                       code if isinstance(code, str) else code['code'])} 
                                for code in post['stocks']]
            
            # 현재 디바이스의 게시물인지 표시
            post['is_owner'] = post['device_id'] == device_id
            
            # 좋아요 정보 추가
            post_likes = likes_by_post.get(post['id'], [])
            post['likes'] = {
                'count': len(post_likes),
                'is_liked': post['id'] in user_liked_posts
            }
            
            # 댓글 정보 추가
            post_comments = comments_by_post.get(post['id'], [])
            post['comments'] = {
                'count': len(post_comments),
                'items': post_comments
            }
        
        # 페이지네이션 적용
        total_count = len(result.data)
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        paginated_posts = result.data[start_idx:end_idx]
        
        return jsonify({
            'posts': paginated_posts,
            'has_more': end_idx < total_count,
            'total_count': total_count
        })

    except Exception as e:
        print(f"게시물 조회 중 오류: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/valuetalk/post/<post_id>', methods=['DELETE'])
def delete_post(post_id):
    try:
        device_id = get_device_id()
        # print(f"Attempting to delete post {post_id} for device {device_id}")
        
        # 게시물이 해당 디바이스에서 작성된 것인지 확인
        result = supabase.table('posts').select('*').eq('id', post_id).eq('device_id', device_id).execute()
        # print(f"Query result: {result.data}")
        
        if not result.data:
            # print(f"Post not found or not owned by device {device_id}")
            return jsonify({'error': '삭제 권한이 없습니다.'}), 403
        
        # 게시물 삭제 시도
        try:
            delete_result = supabase.table('posts').delete().eq('id', post_id).eq('device_id', device_id).execute()
            # print(f"Delete result: {delete_result}")
            
            # 삭제 후 게시물이 실제로 삭제되었는지 확인
            verify_result = supabase.table('posts').select('*').eq('id', post_id).execute()
            # print(f"Verify result: {verify_result.data}")
            
            if verify_result.data:
                # print("Post still exists after delete operation")
                return jsonify({'error': '게시물 삭제에 실패했습니다.'}), 500
                
            return jsonify({'message': '게시물이 삭제되었습니다.'})
            
        except Exception as delete_error:
            # print(f"Delete operation error: {str(delete_error)}")
            return jsonify({'error': '게시물 삭제 중 오류가 발생했습니다.'}), 500

    except Exception as e:
        print(f"게시물 삭제 중 오류: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/valuetalk/post/<post_id>/comments', methods=['GET'])
def get_comments(post_id):
    try:
        # 댓글 조회
        result = supabase.table('comments').select('*').eq('post_id', post_id).order('created_at', desc=True).execute()
        # print(result.data)
        if not result.data:
            return jsonify([])
        
        # 각 댓글에 익명 ID 추가
        for comment in result.data:
            if not comment.get('anonymous_id'):
                comment['anonymous_id'] = generate_anonymous_id()
        
        return jsonify(result.data)

    except Exception as e:
        print(f"댓글 조회 중 오류: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/valuetalk/post/<post_id>/comment', methods=['POST'])
def create_comment(post_id):
    try:
        data = request.get_json()
        if not data or 'content' not in data or 'anonymous_id' not in data:
            return jsonify({'error': '필수 필드가 누락되었습니다.'}), 400

        device_id = get_device_id()
        
        # 현재 시간을 ISO 형식으로 저장
        current_time = datetime.now().astimezone().isoformat()

        # 댓글 생성
        comment = {
            'content': data['content'],
            'created_at': current_time,
            'post_id': post_id,
            'device_id': device_id,
            'anonymous_id': data['anonymous_id']
        }

        # Supabase에 댓글 저장
        result = supabase.table('comments').insert(comment).execute()
        
        if not result.data:
            raise Exception('댓글 저장에 실패했습니다.')

        response = jsonify(result.data[0])
        response.set_cookie('device_id', device_id, max_age=365*24*60*60)  # 1년간 유효
        return response

    except Exception as e:
        print(f"댓글 작성 중 오류: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/valuetalk/comment/<comment_id>', methods=['DELETE'])
def delete_comment(comment_id):
    try:
        device_id = get_device_id()
        
        # 댓글이 해당 디바이스에서 작성된 것인지 확인
        result = supabase.table('comments').select('*').eq('id', comment_id).eq('device_id', device_id).execute()
        
        if not result.data:
            return jsonify({'error': '삭제 권한이 없습니다.'}), 403
        
        # 댓글 삭제
        delete_result = supabase.table('comments').delete().eq('id', comment_id).execute()
        
        return jsonify({'message': '댓글이 삭제되었습니다.'})

    except Exception as e:
        print(f"댓글 삭제 중 오류: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/valuetalk/post/<post_id>/like', methods=['POST'])
def like_post(post_id):
    try:
        device_id = get_device_id()
        
        # 이미 좋아요를 눌렀는지 확인
        result = supabase.table('likes').select('*').eq('post_id', post_id).eq('device_id', device_id).execute()
        
        if result.data:
            # 이미 좋아요를 눌렀다면 취소
            supabase.table('likes').delete().eq('post_id', post_id).eq('device_id', device_id).execute()
            return jsonify({'liked': False})
        else:
            # 좋아요 추가
            like = {
                'post_id': post_id,
                'device_id': device_id,
                'created_at': datetime.now().astimezone().isoformat()
            }
            supabase.table('likes').insert(like).execute()
            return jsonify({'liked': True})
            
    except Exception as e:
        print(f"좋아요 처리 중 오류: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/valuetalk/post/<post_id>/likes')
def get_post_likes(post_id):
    try:
        device_id = get_device_id()
        
        # 게시물의 좋아요 수 조회
        result = supabase.table('likes').select('*').eq('post_id', post_id).execute()
        like_count = len(result.data)
        
        # 현재 사용자가 좋아요를 눌렀는지 확인
        user_like = supabase.table('likes').select('*').eq('post_id', post_id).eq('device_id', device_id).execute()
        is_liked = len(user_like.data) > 0
        
        return jsonify({
            'count': like_count,
            'is_liked': is_liked
        })
        
    except Exception as e:
        print(f"좋아요 정보 조회 중 오류: {str(e)}")
        return jsonify({'error': str(e)}), 500



if __name__ == '__main__':

    app.run(host='0.0.0.0', port=7777, debug=False) 