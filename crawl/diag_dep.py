"""
raw/fs/ 에 받은 전체 재무제표에서 '상각' 관련 계정과 현금흐름표 구조를 확인하는 진단 스크립트.
API를 다시 호출하지 않는다.

사용법:  python diag_dep.py
"""
import glob
import json
import os

files = sorted(glob.glob(os.path.join("raw", "fs", "*.json")))
print(f"파일 {len(files)}개\n")
for p in files:
    data = json.load(open(p, encoding="utf-8"))
    rows = data.get("list", [])
    cf = [r for r in rows if r.get("sj_div") == "CF"]
    dep = [r for r in rows if "상각" in (r.get("account_nm") or "")]
    has_gen = any("창출" in (r.get("account_nm") or "") for r in cf)
    print(f"■ {os.path.basename(p)}  CF행 {len(cf)}개, '영업에서 창출된 현금' 표기 {'있음' if has_gen else '없음'}")
    for r in dep:
        print(f"    {r.get('sj_div'):4s} {r.get('account_id', '')[:45]:45s} {r.get('account_nm')}  {r.get('thstrm_amount')}")
    if not dep:
        print("    (상각 관련 계정 없음) CF 앞부분:", [r.get("account_nm") for r in cf[:6]])