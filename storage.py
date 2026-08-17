#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Supabase Storage 입출력.

크롤러와 웹앱이 데이터를 주고받는 유일한 통로다.

이 모듈이 따로 있는 이유는 웹앱 때문이다. 웹앱은 결과 JSON을 내려받기만
하는데, 이 함수들이 크롤러 모듈 안에 있으면 함수 하나를 쓰려고
requests·lxml·FinanceDataReader·tqdm까지 전부 임포트하게 된다.
콜드 스타트가 느려지고 배포 번들이 불필요하게 커진다.

의존성은 supabase와 python-dotenv뿐이다.
"""

import json
import os

from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv('SUPABASE_URL')
# 크롤러는 쓰기 권한이 필요하므로 service_role 키를 우선 사용한다.
# 웹앱은 읽기만 하므로 anon 키(SUPABASE_KEY)로 충분하다.
USING_SERVICE_KEY = bool(os.getenv('SUPABASE_SERVICE_KEY'))
SUPABASE_KEY = os.getenv('SUPABASE_SERVICE_KEY') or os.getenv('SUPABASE_KEY')
SUPABASE_BUCKET = os.getenv('SUPABASE_BUCKET', 'stock-data')


def key_kind() -> str:
    """어느 키로 동작 중인지. 키 값 자체는 절대 로그에 남기지 않는다."""
    if not SUPABASE_KEY:
        return '없음'
    return 'service_role' if USING_SERVICE_KEY else 'anon(폴백)'

supabase: Client = None


def get_supabase_client():
    """Supabase 클라이언트 반환 (싱글톤)"""
    global supabase
    if supabase is None and SUPABASE_URL and SUPABASE_KEY:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return supabase


def upload_to_supabase(file_name: str, data: list) -> bool:
    """JSON 데이터를 Supabase Storage에 업로드"""
    try:
        client = get_supabase_client()
        if client is None:
            print("Supabase 클라이언트 없음, 로컬 저장만 수행", flush=True)
            return False

        json_bytes = json.dumps(data, ensure_ascii=False).encode('utf-8')

        # 기존 파일 삭제 후 업로드 (upsert)
        try:
            client.storage.from_(SUPABASE_BUCKET).remove([file_name])
        except Exception:
            pass  # 파일이 없으면 무시

        client.storage.from_(SUPABASE_BUCKET).upload(
            file_name,
            json_bytes,
            file_options={"content-type": "application/json"}
        )
        print(f"✅ Supabase Storage 업로드 완료: {file_name}", flush=True)
        return True
    except Exception as e:
        print(f"❌ Supabase Storage 업로드 실패: {e}", flush=True)
        return False


def download_from_supabase(file_name: str) -> list:
    """Supabase Storage에서 JSON 데이터 다운로드.

    실패하면 None을 반환한다. 호출부가 '못 받았다'와 '받았는데 비었다'를
    구분할 수 있어야 하므로 빈 리스트를 대신 돌려주지 않는다.
    """
    try:
        client = get_supabase_client()
        if client is None:
            print("⚠️ Supabase 자격증명 없음 (SUPABASE_URL/SUPABASE_KEY)", flush=True)
            return None

        response = client.storage.from_(SUPABASE_BUCKET).download(file_name)
        data = json.loads(response.decode('utf-8'))
        print(f"✅ Supabase Storage에서 다운로드 완료: {file_name} ({len(data)}개 항목)", flush=True)
        return data
    except Exception as e:
        print(f"⚠️ Supabase Storage 다운로드 실패: {file_name} — {e}", flush=True)
        return None
