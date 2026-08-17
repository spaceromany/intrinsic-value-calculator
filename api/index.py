"""Vercel 서버리스 함수 진입점.

Vercel의 파이썬 런타임은 api/ 디렉터리의 모듈에서 WSGI 애플리케이션을
찾는다. 실제 앱은 저장소 루트의 app.py에 있으므로 그것을 그대로 노출한다.

app.py를 api/ 안으로 옮기지 않은 이유는, 그렇게 하면 Flask가
templates/·static/ 을 api/ 기준으로 찾게 되고 로컬 실행(python app.py)도
깨지기 때문이다. 여기서는 경로만 잡아주고 앱은 손대지 않는다.
"""

import os
import sys

# 루트를 import 경로에 넣어야 app.py와 storage.py를 찾을 수 있다.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import app  # noqa: E402

# Vercel이 이 이름을 찾는다.
application = app
