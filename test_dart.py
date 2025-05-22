# DART API 키 설정
import dart_fss as dart
import pickle, os

dart.set_api_key('d8478b1094233f7d77c7eadc84a4485be4f94473')
corp_list = dart.get_corp_list()

# — 저장
raw_info = [corp._info for corp in corp_list]       # Corp 객체 내부의 순수 dict
with open('corp_list_raw.pkl', 'wb') as f:
    pickle.dump(raw_info, f, protocol=pickle.HIGHEST_PROTOCOL)

# — 복원
with open('corp_list_raw.pkl', 'rb') as f:
    raw_info = pickle.load(f)

# Corp 객체로 다시 감싸기
from dart_fss.corp.corp import Corp, CorpList
corp_list = CorpList([Corp(info) for info in raw_info])