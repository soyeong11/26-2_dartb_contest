"""
노동위원회 결정문 전체 수집기 → CSV (주제 1: 징계 양정 벤치마크용)

흐름
  1단계 list : 검색어별 결정문 목록을 전부 받아 list.csv에 저장 (결정문일련번호 기준 중복 제거)
  2단계 body : list.csv의 결정문마다 본문(판정사항·판정요지·판정결과 등)을 받아 bodies.jsonl에 한 줄씩 추가
  3단계 csv  : bodies.jsonl + list.csv를 합쳐 nlrc_decisions.csv 생성 (엑셀에서 바로 열림)
  all        : 위 세 단계를 순서대로 실행

중간에 끊겨도 같은 명령을 다시 실행하면 받은 것은 건너뛰고 이어서 받는다.

사용법 (PowerShell)
  $env:LAW_OC="yisy7168"
  python nlrc_collect.py all --query 부당해고 --limit 30      # 먼저 30건으로 시험
  python nlrc_collect.py all --query 부당해고                 # 전체 (수 시간 소요)
  python nlrc_collect.py body --retry-failed                  # 실패분만 다시
  python nlrc_collect.py selftest                             # 네트워크 없이 동작 확인

출력 폴더 (기본 nlrc_data)
  list.csv           목록 (결정문일련번호, 사건번호, 제목, 등록일, 검색어 ...)
  list_progress.json 목록 수집 진행 상황 (이어받기용)
  bodies.jsonl       본문 원자료 (한 줄 = 결정문 1건)
  failed.csv         본문 수집 실패 목록
  nlrc_decisions.csv 최종 결과
"""

import argparse
import csv
import html
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

LIST_URL = "http://www.law.go.kr/DRF/lawSearch.do"
BODY_URL = "http://www.law.go.kr/DRF/lawService.do"
DISPLAY = 100

# 최종 CSV 앞쪽에 둘 열 순서 (나머지 필드는 뒤에 자동으로 붙음)
PREFERRED_COLS = ["결정문일련번호", "사건번호", "제목", "등록일", "기관명", "자료구분", "담당부서",
                  "판정결과", "판정사항", "판정요지", "검색어", "본문글자수"]


# ---------------------------------------------------------------------------
# 공통
# ---------------------------------------------------------------------------

class ApiError(Exception):
    pass


def default_fetch(url, params, timeout=30):
    q = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(q, headers={"User-Agent": "Mozilla/5.0 (academic research, non-commercial)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 인증 실패·미신청 등은 HTTP 200에 HTML/텍스트로 오는 경우가 있음
        text = re.sub(r"<[^>]+>", " ", raw)
        raise ApiError("JSON이 아닌 응답: " + re.sub(r"\s+", " ", text).strip()[:200])


FETCH = default_fetch  # selftest에서 가짜 함수로 바꿔 끼움


def call(url, params, retries=4, base_wait=2.0):
    last = None
    for i in range(retries):
        try:
            return FETCH(url, params)
        except ApiError as e:
            msg = str(e)
            # 인증·미신청 오류는 재시도해도 소용없으므로 바로 중단
            if any(k in msg for k in ("인증", "미신청", "권한")):
                raise
            last = e
        except Exception as e:  # noqa: BLE001  (타임아웃·연결 끊김)
            last = e
        time.sleep(base_wait * (2 ** i))
    raise ApiError(f"재시도 {retries}회 실패: {last}")


def find_list(obj):
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        return obj
    if isinstance(obj, dict):
        for v in obj.values():
            got = find_list(v)
            if got:
                return got
    return []


def find_total(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in ("totalcnt", "total_cnt", "totalcount") and str(v).strip().isdigit():
                return int(v)
            got = find_total(v)
            if got is not None:
                return got
    return None


def flatten_strings(obj, prefix=""):
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(flatten_strings(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(flatten_strings(v, f"{prefix}[{i}]"))
    elif isinstance(obj, (str, int, float)) and str(obj).strip():
        out.append((prefix, str(obj)))
    return out


def clean_text(s):
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(html.unescape(s))  # &#8228; &quot; 등 모든 HTML 코드 → 문자 (이중 인코딩 대비 2회)
    s = s.replace(" ", " ")
    # "…존재한다.나. 징계절차"처럼 붙은 항목 기호 앞에 줄바꿈 (날짜 "2016. 1. 1."은 건드리지 않음)
    s = re.sub(r"(?<=[.다음됨])\s*(?=[나다라마바사아]\.\s?[가-힣])", "\n", s)
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def body_to_fields(body):
    """본문 응답 → {필드명: 텍스트}. 응답 구조를 몰라도 되도록 모든 문자열을 펼쳐서 마지막 키 이름으로 묶음."""
    fields = {}
    for path, val in flatten_strings(body):
        key = re.split(r"[.\]]", path)[-1].lstrip("[") or path
        val = clean_text(val)
        if not val:
            continue
        fields[key] = (fields[key] + "\n" + val) if key in fields and fields[key] != val else val
    return fields


def get_oc():
    oc = os.environ.get("LAW_OC")
    if not oc:
        sys.exit('환경변수 LAW_OC가 없습니다. PowerShell: $env:LAW_OC="발급받은OC"')
    return oc


# ---------------------------------------------------------------------------
# 1단계: 목록
# ---------------------------------------------------------------------------

LIST_COLS = ["결정문일련번호", "사건번호", "제목", "등록일", "결정문상세링크", "검색어"]


def load_list(out):
    path = os.path.join(out, "list.csv")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {r["결정문일련번호"]: r for r in csv.DictReader(f)}


def step_list(args):
    oc = get_oc()
    os.makedirs(args.out, exist_ok=True)
    list_path = os.path.join(args.out, "list.csv")
    prog_path = os.path.join(args.out, "list_progress.json")
    progress = json.load(open(prog_path, encoding="utf-8")) if os.path.exists(prog_path) else {}
    seen = set(load_list(args.out))
    new_file = not os.path.exists(list_path)

    with open(list_path, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LIST_COLS)
        if new_file:
            w.writeheader()
        for query in args.query:
            st = progress.get(query, {"next_page": 1, "total": None, "done": False})
            if st["done"]:
                print(f"[목록] '{query}' 이미 완료, 건너뜀")
                continue
            page = st["next_page"]
            while True:
                data = call(LIST_URL, dict(OC=oc, target="nlrc", type="JSON",
                                           query=query, display=DISPLAY, page=page))
                if st["total"] is None:
                    st["total"] = find_total(data)
                rows = find_list(data)
                added = 0
                for r in rows:
                    did = str(r.get("결정문일련번호") or "").strip()
                    if not did or did in seen:
                        continue
                    seen.add(did)
                    w.writerow({c: r.get(c, "") for c in LIST_COLS[:-1]} | {"검색어": query})
                    added += 1
                f.flush()
                total = st["total"]
                pages = -(-total // DISPLAY) if total else None
                print(f"[목록] '{query}' {page}/{pages or '?'}쪽, 신규 {added}건 (누적 {len(seen)}건)")
                page += 1
                st["next_page"] = page
                limit_reached = args.limit and len(seen) >= args.limit
                if not rows or (pages and page > pages) or limit_reached:
                    st["done"] = not limit_reached
                    break
                progress[query] = st
                json.dump(progress, open(prog_path, "w", encoding="utf-8"), ensure_ascii=False)
                time.sleep(args.sleep)
            progress[query] = st
            json.dump(progress, open(prog_path, "w", encoding="utf-8"), ensure_ascii=False)
            if args.limit and len(seen) >= args.limit:
                break
    print(f"[목록] 완료: 결정문 {len(seen)}건 → {list_path}")


# ---------------------------------------------------------------------------
# 2단계: 본문
# ---------------------------------------------------------------------------

def done_ids(out):
    path = os.path.join(out, "bodies.jsonl")
    ids = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    ids.add(json.loads(line)["결정문일련번호"])
                except (json.JSONDecodeError, KeyError):
                    pass  # 강제 종료로 깨진 마지막 줄은 무시하고 다시 받음
    return ids


def step_body(args):
    oc = get_oc()
    listing = load_list(args.out)
    if not listing:
        sys.exit("list.csv가 없습니다. 먼저 list 단계를 실행하세요.")
    have = done_ids(args.out)
    failed_path = os.path.join(args.out, "failed.csv")

    if args.retry_failed and os.path.exists(failed_path):
        with open(failed_path, encoding="utf-8-sig", newline="") as f:
            targets = [r["결정문일련번호"] for r in csv.DictReader(f) if r["결정문일련번호"] not in have]
        os.remove(failed_path)
    else:
        targets = [i for i in listing if i not in have]
    if args.limit:
        targets = targets[: max(0, args.limit - len(have))]
    print(f"[본문] 받을 것 {len(targets)}건 (이미 받은 것 {len(have)}건), 동시 {args.workers}개")
    if not targets:
        return

    lock = threading.Lock()
    body_f = open(os.path.join(args.out, "bodies.jsonl"), "a", encoding="utf-8")
    new_failed = not os.path.exists(failed_path)
    fail_f = open(failed_path, "a", encoding="utf-8-sig", newline="")
    fail_w = csv.writer(fail_f)
    if new_failed:
        fail_w.writerow(["결정문일련번호", "오류"])

    stop = threading.Event()
    counter = {"ok": 0, "fail": 0}
    t0 = time.time()

    def work(did):
        if stop.is_set():
            return
        try:
            body = call(BODY_URL, dict(OC=oc, target="nlrc", ID=did, type="JSON"))
            fields = body_to_fields(body)
            rec = {"결정문일련번호": did, "fields": fields}
            with lock:
                body_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                body_f.flush()
                counter["ok"] += 1
        except ApiError as e:
            with lock:
                fail_w.writerow([did, str(e)[:300]])
                fail_f.flush()
                counter["fail"] += 1
                if any(k in str(e) for k in ("인증", "미신청", "권한")):
                    print(f"\n[중단] 인증·신청 문제로 보입니다: {e}")
                    stop.set()
        time.sleep(args.sleep)
        n = counter["ok"] + counter["fail"]
        if n % 100 == 0:
            rate = n / max(1e-9, time.time() - t0)
            remain = (len(targets) - n) / rate / 60 if rate else 0
            print(f"[본문] {n}/{len(targets)} (성공 {counter['ok']}, 실패 {counter['fail']}), 남은 시간 약 {remain:.0f}분")

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for fut in as_completed([ex.submit(work, d) for d in targets]):
                fut.result()
    except KeyboardInterrupt:
        stop.set()
        print("\n[본문] 사용자가 중단함. 같은 명령으로 다시 실행하면 이어서 받습니다.")
    finally:
        body_f.close()
        fail_f.close()
    print(f"[본문] 끝: 성공 {counter['ok']}, 실패 {counter['fail']} (실패분은 --retry-failed로 재시도)")


# ---------------------------------------------------------------------------
# 3단계: CSV
# ---------------------------------------------------------------------------

def step_csv(args):
    listing = load_list(args.out)
    path = os.path.join(args.out, "bodies.jsonl")
    if not os.path.exists(path):
        sys.exit("bodies.jsonl이 없습니다. 먼저 body 단계를 실행하세요.")
    rows, keys = {}, []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            did = rec["결정문일련번호"]
            row = dict(listing.get(did, {}))
            row.pop("결정문상세링크", None)
            for k, v in rec["fields"].items():
                if k in ("결정문일련번호",):
                    continue
                v = clean_text(v)  # 이미 받은 본문도 최신 정리 규칙으로 다시 정리
                row[k] = v if not row.get(k) else row[k]  # 목록 값이 있으면 유지
            row["결정문일련번호"] = did
            body_len = sum(len(row.get(k, "")) for k in ("판정사항", "판정요지", "내용", "전문"))
            row["본문글자수"] = body_len
            rows[did] = row  # 같은 ID가 두 번 있으면 마지막 것
            for k in row:
                if k not in keys:
                    keys.append(k)
    cols = [c for c in PREFERRED_COLS if c in keys] + [k for k in keys if k not in PREFERRED_COLS]
    out_path = os.path.join(args.out, "nlrc_decisions.csv")
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows.values():
            w.writerow(r)
    missing = len(listing) - len(rows)
    print(f"[CSV] {len(rows)}건, 열 {len(cols)}개 → {out_path}")
    if missing > 0:
        print(f"[CSV] 목록 대비 본문 미수집 {missing}건 (body 단계를 다시 실행하거나 --retry-failed)")


# ---------------------------------------------------------------------------
# 자체 테스트 (가짜 API)
# ---------------------------------------------------------------------------

def selftest(out):
    import shutil
    global FETCH
    shutil.rmtree(out, ignore_errors=True)
    total = 235
    calls = {"n": 0}

    def fake(url, params):
        calls["n"] += 1
        if url == LIST_URL:
            p, d = int(params["page"]), int(params["display"])
            ids = range((p - 1) * d + 1, min(p * d, total) + 1)
            return {"Nlrc": {"totalCnt": str(total), "nlrc": [
                {"id": str(i), "결정문일련번호": str(10000 + i), "사건번호": "익명", "제목": "부당해고 구제신청",
                 "등록일": "20240101", "결정문상세링크": "/x"} for i in ids]}}
        did = int(params["ID"])
        if did == 10007 and calls.get("flaky", 0) < 1:  # 한 번 실패 후 성공하는 건
            calls["flaky"] = 1
            raise TimeoutError("timeout")
        if did == 10013:
            raise ApiError("JSON이 아닌 응답: 일시적 서버 오류")
        return {"NlrcService": {"판정사항": f"정직 3개월의 양정이 과다한지 여부 {did}",
                                "판정요지": "징계사유는 인정되나 <br/>양정이 과다하다.",
                                "판정결과": "전부인정", "기관명": "서울지방노동위원회"}}

    FETCH = fake
    os.environ.setdefault("LAW_OC", "test")
    base = dict(out=out, query=["부당해고"], sleep=0, workers=3, retry_failed=False)
    ns = argparse.Namespace

    # 1) 목록 50건 제한 → 2) 전체 목록 이어받기
    step_list(ns(**base, limit=50))
    step_list(ns(**base, limit=0))
    listing = load_list(out)
    # 3) 본문 (call 재시도 대기 줄이기)
    global call
    real_call = call
    call = lambda u, p: real_call(u, p, retries=2, base_wait=0)  # noqa: E731
    step_body(ns(**base, limit=0))
    step_csv(ns(**base, limit=0))
    with open(os.path.join(out, "nlrc_decisions.csv"), encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    checks = [
        (len(listing) == total, f"목록 {total}건 중복 없이 수집 (이어받기 포함)"),
        (len(rows) == total - 1, "본문 234건 (실패 1건 제외)"),
        (any(r["결정문일련번호"] == "10007" for r in rows), "일시 오류 재시도 후 성공"),
        (os.path.exists(os.path.join(out, "failed.csv")), "실패 목록 기록"),
        (rows[0]["판정결과"] == "전부인정" and "\n" in rows[0]["판정요지"], "필드·줄바꿈 정리"),
        (list(rows[0].keys())[:3] == ["결정문일련번호", "사건번호", "제목"], "열 순서"),
    ]
    # 4) 재실행해도 다시 받지 않는지
    before = calls["n"]
    step_body(ns(**base, limit=0))
    checks.append((calls["n"] - before <= 3, "재실행 시 받은 것은 건너뜀 (실패분만 재시도)"))
    call = real_call
    ok = all(c for c, _ in checks)
    for c, name in checks:
        print(("  OK   " if c else "  FAIL ") + name)
    return ok


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="노동위원회 결정문 수집기")
    ap.add_argument("step", choices=["list", "body", "csv", "all", "selftest"])
    ap.add_argument("--query", nargs="+", default=["부당해고"], help="검색어 (여러 개 가능, 사건명 기준 검색)")
    ap.add_argument("--out", default="nlrc_data")
    ap.add_argument("--limit", type=int, default=0, help="시험용: 최대 건수 (0이면 전체)")
    ap.add_argument("--workers", type=int, default=2, help="본문 동시 요청 수 (서버 부담 고려해 1~3 권장)")
    ap.add_argument("--sleep", type=float, default=0.3, help="요청 간 대기(초)")
    ap.add_argument("--retry-failed", action="store_true", help="failed.csv에 있는 것만 다시 받기")
    args = ap.parse_args()

    if args.step == "selftest":
        sys.exit(0 if selftest(os.path.join(args.out + "_selftest")) else 1)
    if args.step in ("list", "all"):
        step_list(args)
    if args.step in ("body", "all"):
        step_body(args)
    if args.step in ("csv", "all"):
        step_csv(args)


if __name__ == "__main__":
    main()