# 내재가치 계산기

주식의 내재가치와 안전마진을 계산하는 웹 애플리케이션입니다.

## 기능

- 종목명으로 주식 검색
- 내재가치 계산 (EPS 가중평균, BPS 기반)
- 자기주식 조정
- 안전마진 계산
- 과거 재무지표 표시

## 설치 및 실행

1. 필요한 패키지 설치:
```bash
pip install -r requirements.txt          # 웹앱만 실행할 때
pip install -r requirements-crawl.txt    # 크롤러도 돌릴 때 추가
```

2. 애플리케이션 실행:
```bash
python app.py
```

3. 웹 브라우저에서 접속:
```
http://localhost:7777
```

## 구조

크롤링과 웹 서빙은 분리되어 있습니다.

```
[GitHub Actions cron]  평일 16:00 KST (장 마감 후)
  crawl.py → 네이버/DART 크롤링
           → Supabase Storage 업로드
                    │
                    ▼
[Supabase Storage]  all_safety_margin_results.json
                    ncav_results.json
                    │  (웹앱은 읽기만)
                    ▼
[웹앱] app.py — 결과를 메모리에 캐싱해 서빙
```

파일 구성:

| 파일 | 역할 | 의존성 |
|---|---|---|
| `app.py` | 웹 서빙 전용. 크롤링하지 않음 | `requirements.txt` |
| `crawl.py` | 배치 크롤러 진입점 | `requirements-crawl.txt` |
| `safety_margin_calc_naver.py` | 크롤링·계산 로직 | 〃 |
| `storage.py` | Supabase 입출력. 양쪽이 공유 | supabase, python-dotenv |

`storage.py`가 따로 있는 이유는 웹앱 때문입니다. 이 함수들이 크롤러 모듈
안에 있으면 웹앱이 `download_from_supabase` 하나를 쓰려고
requests·lxml·FinanceDataReader·tqdm까지 전부 임포트하게 됩니다.
분리 후 웹앱의 모듈 임포트 시간이 2.42초에서 1.30초로 줄었습니다.

웹앱은 크롤링하지 않습니다. 한 프로세스에 묶여 있을 때는 백그라운드
크롤링이 웹 호스트의 아웃바운드 대역폭을 월 수십 GB씩 소모했습니다.
분리 후 웹앱의 트래픽은 실제 사용자 요청과 시간당 1회의 캐시 갱신뿐입니다.

### 웹앱 배포 (Vercel)

서울 리전(`icn1`)으로 고정되어 있습니다. 한국 사용자 기준 지연이 가장 낮습니다.

1. vercel.com에서 GitHub 저장소를 import
2. Settings → Environment Variables에 등록:
   - `SUPABASE_URL`, `SUPABASE_KEY` — 결과를 읽기 위해 필요
   - `SITE_URL` — 배포된 도메인 (예: `https://<프로젝트명>.vercel.app`).
     설정하지 않으면 canonical과 sitemap이 옛 Render 주소를 가리킨다
3. 이후 `main`에 push하면 자동 배포

구조상 주의할 점:

- `api/index.py`가 진입점이다. 실제 앱은 루트의 `app.py`에 그대로 두고
  경로만 잡아준다. `app.py`를 `api/` 안으로 옮기면 Flask가 `templates/`를
  찾지 못하고 `python app.py` 로컬 실행도 깨진다.
- 서버리스라 유휴 상태 후 첫 요청에 콜드 스타트가 붙는다. `pandas`는
  엑셀 내보내기에서만 쓰이므로 지연 임포트로 돌려 콜드 스타트 경로에서
  제외했다 (약 1.5초 절감).
- 함수 번들은 압축 해제 250MB 제한이 있다. `.vercelignore`로 크롤러
  파일과 `krx_stocks.json`을 제외한다.

### 크롤러 수동 실행

```bash
python crawl.py
```

### 필요한 환경변수

| 변수 | 대상 | 설명 |
|---|---|---|
| `SUPABASE_URL` | 크롤러 + 웹앱 | Supabase 프로젝트 URL |
| `SUPABASE_KEY` | 크롤러 + 웹앱 | Supabase anon 키. 웹앱은 읽기만 하므로 이걸로 충분 |
| `SUPABASE_SERVICE_KEY` | 크롤러 | 있으면 `SUPABASE_KEY`보다 우선 사용. 쓰기 권한이 필요한 크롤러용이며, 브라우저에 절대 노출하지 말 것 |
| `SUPABASE_BUCKET` | 크롤러 + 웹앱 | 버킷 이름. 기본 `stock-data` |
| `DART_API_KEY` | 크롤러 | 없으면 NCAV 스크리닝을 건너뜀 |
| `CACHE_TTL` | 웹앱 | 캐시 수명(초). 기본 3600 |
| `SITE_URL` | 웹앱 | canonical·og:url·sitemap에 쓰이는 절대 URL 기준. 기본 `https://intrinsic-value-calculator.onrender.com`. 호스팅을 옮기거나 커스텀 도메인을 붙이면 이 값만 바꾸면 된다 |
| `CRAWL_BUDGET_SECONDS` | 크롤러 | 안전마진 분석 시간 상한. 기본 3600 |
| `NCAV_BUDGET_SECONDS` | 크롤러 | NCAV 스크리닝 시간 상한. 기본 1800 |
| `STOCK_REFRESH_SECONDS` | 크롤러 | 종목 재분석 주기. 기본 3600 |

크롤러용 값은 GitHub 저장소의 Settings → Secrets and variables → Actions에
등록합니다. 시간 상한을 두는 이유는 Actions 실행이 무료 분을 초과하거나
6시간 작업 제한에 걸려 결과를 통째로 잃는 것을 막기 위함입니다. 종목은
오래된 순으로 처리되므로 중단돼도 다음 실행이 남은 종목부터 이어받습니다.

## 내재가치 계산 방법

1. EPS 가중평균 계산:
   - 최근년도 EPS × 3
   - 전년도 EPS × 2
   - 전전년도 EPS × 1
   - 가중평균 = (최근년도EPS×3 + 전년도EPS×2 + 전전년도EPS×1) ÷ 6

2. 내재가치 계산:
   - 기본 내재가치 = (EPS 가중평균 × 10 + 최근년도 BPS) ÷ 2
   - 자기주식이 있는 경우: 내재가치 = 기본 내재가치 × (100 ÷ (100 - 자기주식비율))

3. 안전마진 계산:
   - 안전마진 = ((내재가치 - 현재가) ÷ 현재가) × 100 