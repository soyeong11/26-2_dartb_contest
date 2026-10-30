"""
노동위원회 결정문 본문 프로브 (주제 1·2 공통 첫 관문 테스트)

목적
  결정문 본문(판정사항·판정요지·전문 등)에서 아래 정보를 뽑을 수 있는지 빠르게 확인한다.
    - 주제 1용: 징계 수위, 쟁점(사유·양정·절차 등), 판정 결과
    - 주제 2용: 업종 단서, 사업장 규모(상시근로자 수), 관할 지노위
  규칙(정규식·키워드) 기반 1차 표시이므로 "단서가 있는지"를 보는 용도다.
  실제 라벨링은 이후 LLM + 2인 검수로 한다.

사용법
  1) API로 무작위 표본 받기 (본인 PC에서 실행)
       set LAW_OC=발급받은OC        (macOS/Linux: export LAW_OC=발급받은OC)
       python nlrc_probe.py api --query 부당해고 --n 50 --out probe_out
  2) 이미 받은 CSV로 돌리기 (예: 진우 님이 올린 본문 샘플 CSV)
       python nlrc_probe.py csv --path 노동위원회_결정문_본문_샘플.csv --out probe_out
  3) 네트워크 없이 동작 확인
       python nlrc_probe.py selftest

출력 (out 폴더)
  raw/              API 원본 JSON (api 모드)
  probe_rows.csv    결정문별 추출 결과 + 근거 문장 + 사람이 채울 검수 칸
  field_stats.csv   본문 필드별 채움률·평균 길이 (전문이 오는지 확인용)
  summary.md        항목별 추출률과 판단 기준 대비 결과

주의
  - API 응답의 본문 필드명은 아직 확인 전이라, 응답 안의 모든 문자열 필드를 모아서 검사한다.
  - 신청한 형식(JSON)과 다른 형식으로 호출하면 "미신청" 오류가 난다 (2차시 확인 사항).
  - 업종 키워드 매칭은 오탐이 있을 수 있다. probe_rows.csv의 근거 문장을 보고 검수 칸을 채울 것.
"""

import argparse
import csv
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

LIST_URL = "http://www.law.go.kr/DRF/lawSearch.do"
BODY_URL = "http://www.law.go.kr/DRF/lawService.do"

# ---------------------------------------------------------------------------
# 1. 사전(키워드) 정의
# ---------------------------------------------------------------------------

# 업종 단서 → 표준산업분류 대분류 (코드, 이름). 순서대로 검사하며 여러 개가 걸리면 모두 기록.
INDUSTRY_CUES = {
    ("C", "제조업"): ["제조업", "제조", "공장", "생산직", "생산라인", "생산팀"],
    ("F", "건설업"): ["건설업", "건설회사", "공사현장", "건설현장", "현장소장", "건축"],
    ("H", "운수·창고업"): ["운수", "택시", "버스", "화물", "운송", "물류", "택배", "운전기사", "배송"],
    ("Q", "보건·사회복지"): ["병원", "의원", "요양원", "요양병원", "요양보호사", "간호사", "간호조무사",
                          "복지관", "사회복지", "어린이집", "보육교사", "재가", "의료법인"],
    ("P", "교육서비스업"): ["학교", "학원", "교사", "강사", "대학교", "교육청", "학교법인", "교직원"],
    ("I", "숙박·음식점업"): ["음식점", "식당", "호텔", "카페", "주방", "조리사", "숙박"],
    ("G", "도매·소매업"): ["마트", "편의점", "매장", "판매직", "백화점", "도매", "소매"],
    ("N", "사업시설관리·지원"): ["경비원", "경비업", "청소", "미화", "시설관리", "용역업체", "파견", "아파트 관리"],
    ("K", "금융·보험업"): ["은행", "보험회사", "증권", "금고", "신용협동조합", "카드사"],
    ("J", "정보통신업"): ["소프트웨어", "방송", "IT ", "개발자", "콘텐츠", "게임"],
    ("O", "공공행정"): ["지방자치단체", "공무직", "시청", "구청", "군청", "공단", "도청"],
    ("M", "전문·과학·기술"): ["법무법인", "회계법인", "연구소", "연구원", "설계사무소", "컨설팅"],
    ("S", "협회·단체·기타개인서비스"): ["협회", "노동조합 사무", "종교", "교회", "사찰", "미용실"],
}

# 업종을 명시하는 문장 패턴 (예: "자동차부품 제조업을 영위하는", "~을 목적으로 하는 법인")
INDUSTRY_PHRASE_PATTERNS = [
    re.compile(r"([가-힣A-Za-z0-9·,\s]{2,30}?(?:업|사업))(?:을|를)\s*(?:영위|운영|경영)"),
    re.compile(r"([가-힣A-Za-z0-9·,\s]{2,30}?)(?:을|를)\s*목적으로\s*(?:하는|설립된)"),
    re.compile(r"업종[은는:]?\s*([가-힣A-Za-z0-9·\s]{2,20})"),
]

# 상시근로자 수
SIZE_PATTERNS = [
    re.compile(r"상시\s*(?:근로자\s*)?(?:약\s*)?([\d,]+)\s*(?:여\s*)?명"),
    re.compile(r"상시\s*([\d,]+)\s*(?:여\s*)?명(?:의|을)?\s*근로자"),
    re.compile(r"근로자\s*(?:약\s*)?([\d,]+)\s*(?:여\s*)?명을\s*(?:사용|고용)"),
    re.compile(r"상시\s*사용\s*근로자\s*(?:수\s*)?(?:는|가)?\s*(?:약\s*)?([\d,]+)\s*명"),
]
SMALL_FIRM_PATTERNS = [re.compile(p) for p in [r"5인\s*미만", r"5명\s*미만", r"4인\s*이하", r"4명\s*이하"]]

# 징계 수위 (무거운 것부터 검사)
SANCTION_CUES = [
    ("징계해고", [r"징계\s*해고", r"파면"]),
    # "부당해고(구제신청)", "해고예고" 같은 사건명·제도명은 징계 수위로 세지 않음
    ("해고(일반)", [r"(?<!부당)(?<!부당\s)(?<!징계)(?<!징계\s)해고(?!\s*예고)(?!\s*등\s*구제)"]),
    ("권고사직", [r"권고\s*사직", r"사직\s*강요"]),
    ("계약만료·갱신거절", [r"갱신\s*거절", r"계약\s*기간\s*만료", r"근로계약\s*종료", r"계약\s*만료"]),
    ("본채용거부", [r"본\s*채용\s*거부", r"시용\s*기간"]),
    ("강등", [r"강등"]),
    ("정직", [r"정직"]),
    ("감봉", [r"감봉"]),
    ("견책", [r"견책"]),
    ("경고", [r"경고"]),
    ("대기발령·전보", [r"대기\s*발령", r"전보", r"전직", r"직위\s*해제"]),
]
SUSPENSION_LEN = re.compile(r"정직\s*(\d+)\s*(개월|월|일)")

# 쟁점
ISSUE_CUES = {
    "사유(징계사유 존재)": [r"징계\s*사유", r"해고\s*사유", r"사유가\s*(?:존재|인정)"],
    "양정(과다)": [r"양정", r"과다", r"과중", r"재량권.{0,6}(?:일탈|남용)"],
    "절차(소명·통지·징계위)": [r"소명\s*기회", r"서면\s*통지", r"제27조", r"징계\s*위원회", r"절차상\s*하자",
                           r"징계\s*절차", r"해고\s*절차"],
    "갱신기대권": [r"갱신\s*기대권"],
    "시용·수습": [r"시용", r"수습"],
    "경영상 해고": [r"경영상\s*(?:이유|해고)", r"정리\s*해고", r"제24조"],
    "근로자성·당사자적격": [r"근로자성", r"근로자에\s*해당", r"당사자\s*적격", r"사용자\s*적격"],
    "괴롭힘·성희롱": [r"직장\s*내\s*괴롭힘", r"성희롱"],
}

# 판정 결과
RESULT_CUES = [
    ("일부인정", [r"일부\s*인정"]),
    ("전부인정", [r"전부\s*인정", r"(?<!일부\s)(?<!일부)인정한다", r"부당해고(?:임|에\s*해당)"]),
    ("기각", [r"기각"]),
    ("각하", [r"각하"]),
    ("화해·취하", [r"화해", r"취하"]),
]

NLRC_PATTERN = re.compile(r"(서울|부산|경기|인천|강원|충북|충남|전북|전남|경북|경남|울산|제주|중앙)\s*(?:지방)?노동위원회")

CONTEXT = 35  # 근거 문장 앞뒤 글자 수


# ---------------------------------------------------------------------------
# 2. 텍스트 모으기
# ---------------------------------------------------------------------------

def flatten_strings(obj, prefix=""):
    """JSON 안의 모든 문자열 필드를 (경로, 값)으로 펼친다. 응답 구조를 몰라도 동작하도록."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(flatten_strings(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(flatten_strings(v, f"{prefix}[{i}]"))
    elif isinstance(obj, str):
        val = obj.strip()
        if val:
            out.append((prefix, val))
    return out


def clean_text(s):
    s = re.sub(r"<[^>]+>", " ", s)          # HTML 태그 제거
    s = s.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", s).strip()


def short_key(path):
    """'Nlrc.판정요지' → '판정요지', 'x[0].내용' → '내용'"""
    return re.split(r"[.\]]", path)[-1].lstrip("[") or path


# ---------------------------------------------------------------------------
# 3. 추출
# ---------------------------------------------------------------------------

def snippet(text, m):
    a, b = max(0, m.start() - CONTEXT), min(len(text), m.end() + CONTEXT)
    return ("…" if a > 0 else "") + text[a:b] + ("…" if b < len(text) else "")


def find_first(text, patterns):
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m
    return None


def extract(text):
    r = {}

    # 업종: 명시 문장 우선, 다음은 키워드
    phrases = []
    for p in INDUSTRY_PHRASE_PATTERNS:
        for m in p.finditer(text):
            phrases.append(m.group(1).strip())
    r["업종_명시문장"] = " | ".join(dict.fromkeys(phrases))[:200]

    hits, ev = [], []
    for (code, name), words in INDUSTRY_CUES.items():
        for w in words:
            idx = text.find(w)
            if idx >= 0:
                hits.append(f"{code}:{name}")
                m = re.search(re.escape(w), text)
                ev.append(f"[{w}] {snippet(text, m)}")
                break
    r["업종_후보"] = " / ".join(hits)
    r["업종_후보수"] = len(hits)
    r["업종_근거"] = " || ".join(ev[:3])
    r["업종_단서있음"] = int(bool(hits or phrases))

    # 규모
    size_val, size_ev = "", ""
    for p in SIZE_PATTERNS:
        m = p.search(text)
        if m:
            size_val = m.group(1).replace(",", "")
            size_ev = snippet(text, m)
            break
    small = find_first(text, [p.pattern for p in SMALL_FIRM_PATTERNS])
    r["상시근로자수"] = size_val
    r["5인미만_언급"] = int(bool(small))
    r["규모_근거"] = size_ev or (snippet(text, small) if small else "")
    r["규모_단서있음"] = int(bool(size_val or small))
    r["규모구간"] = size_bucket(size_val, bool(small))

    # 징계 수위
    sanctions, s_ev = [], []
    for label, pats in SANCTION_CUES:
        m = find_first(text, pats)
        if m:
            sanctions.append(label)
            if len(s_ev) < 2:
                s_ev.append(snippet(text, m))
    susp = SUSPENSION_LEN.search(text)
    r["징계수위_후보"] = " / ".join(sanctions)
    r["징계수위_대표"] = sanctions[0] if sanctions else ""
    r["정직기간"] = f"{susp.group(1)}{susp.group(2)}" if susp else ""
    r["징계수위_근거"] = " || ".join(s_ev)
    r["징계수위_단서있음"] = int(bool(sanctions))

    # 쟁점
    issues = [name for name, pats in ISSUE_CUES.items() if find_first(text, pats)]
    r["쟁점_후보"] = " / ".join(issues)
    r["쟁점수"] = len(issues)

    # 판정 결과
    res = ""
    for label, pats in RESULT_CUES:
        if find_first(text, pats):
            res = label
            break
    r["판정결과_텍스트추정"] = res

    # 관할
    nl = NLRC_PATTERN.search(text)
    r["노동위원회"] = (nl.group(1) + ("지노위" if nl.group(1) != "중앙" else "노위")) if nl else ""
    return r


def size_bucket(size_val, small_flag):
    if size_val:
        n = int(size_val)
        if n < 5:
            return "5인 미만"
        if n < 30:
            return "5~29인"
        if n < 100:
            return "30~99인"
        if n < 300:
            return "100~299인"
        return "300인 이상"
    return "5인 미만(언급)" if small_flag else ""


# ---------------------------------------------------------------------------
# 4. 데이터 가져오기
# ---------------------------------------------------------------------------

def http_json(url, params, retries=3, wait=1.0):
    q = url + "?" + urllib.parse.urlencode(params)
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(q, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                raise RuntimeError(f"JSON이 아닌 응답(형식 미신청 오류일 수 있음): {raw[:200]}")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(wait * (i + 1))
    raise RuntimeError(f"호출 실패: {q}\n{last}")


def find_list(obj):
    """응답 안에서 dict 목록을 찾는다 (구조: Nlrc → nlrc 목록)."""
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
            if k.lower() in ("totalcnt", "total_cnt", "totalcount") and str(v).isdigit():
                return int(v)
            got = find_total(v)
            if got:
                return got
    return None


def sample_ids(oc, query, n, seed, display=100):
    first = http_json(LIST_URL, dict(OC=oc, target="nlrc", type="JSON", query=query, display=display, page=1))
    total = find_total(first)
    rows = find_list(first)
    if not rows:
        raise RuntimeError("목록이 비어 있음. OC 값·검색어·신청 형식을 확인하세요.")
    pages = max(1, (total or len(rows)) // display)
    rng = random.Random(seed)
    want_pages = sorted(rng.sample(range(1, pages + 1), k=min(pages, max(1, n // 10 + 1))))
    print(f"[목록] 총 {total if total is not None else '알수없음'}건, {len(want_pages)}개 페이지에서 표본 추출")

    pool = []
    for p in want_pages:
        data = first if p == 1 else http_json(LIST_URL, dict(OC=oc, target="nlrc", type="JSON",
                                                            query=query, display=display, page=p))
        for r in find_list(data):
            # 결정문일련번호를 ID로 사용 (id는 페이지 내 순번이라 쓰면 안 됨)
            did = r.get("결정문일련번호") or r.get("결정문 일련번호")
            if did:
                pool.append((str(did), r.get("제목", ""), r.get("사건번호", "")))
        time.sleep(0.5)
    rng.shuffle(pool)
    return pool[:n]


def run_api(args):
    oc = os.environ.get("LAW_OC")
    if not oc:
        sys.exit("환경변수 LAW_OC가 없습니다. 예: export LAW_OC=발급받은OC")
    raw_dir = os.path.join(args.out, "raw")
    os.makedirs(raw_dir, exist_ok=True)
    picked = sample_ids(oc, args.query, args.n, args.seed)
    docs = []
    for i, (did, title, caseno) in enumerate(picked, 1):
        path = os.path.join(raw_dir, f"{did}.json")
        if os.path.exists(path):  # 이어받기
            with open(path, encoding="utf-8") as f:
                body = json.load(f)
        else:
            try:
                body = http_json(BODY_URL, dict(OC=oc, target="nlrc", ID=did, type="JSON"))
            except RuntimeError as e:
                print(f"  [{i}/{len(picked)}] {did} 실패: {e}")
                continue
            with open(path, "w", encoding="utf-8") as f:
                json.dump(body, f, ensure_ascii=False, indent=1)
            time.sleep(args.sleep)
        fields = {short_key(k): clean_text(v) for k, v in flatten_strings(body)}
        docs.append(dict(id=did, 제목=title, 사건번호=caseno, fields=fields))
        print(f"  [{i}/{len(picked)}] {did} 본문 필드 {len(fields)}개")
    return docs


def run_csv(args):
    docs = []
    with open(args.path, encoding="utf-8-sig", newline="") as f:
        for i, row in enumerate(csv.DictReader(f), 1):
            fields = {k.strip(): clean_text(v or "") for k, v in row.items() if k}
            did = fields.get("결정문일련번호") or fields.get("id") or str(i)
            docs.append(dict(id=did, 제목=fields.get("제목", ""), 사건번호=fields.get("사건번호", ""), fields=fields))
    print(f"[CSV] {len(docs)}건 읽음")
    return docs


# ---------------------------------------------------------------------------
# 5. 집계·저장
# ---------------------------------------------------------------------------

META_KEYS = {"id", "결정문일련번호", "사건번호", "등록일", "결정문상세링크", "제목"}

THRESHOLDS = [  # (항목, 컬럼, 기준, 쓰는 곳)
    ("징계수위 단서", "징계수위_단서있음", 0.70, "주제 1 핵심 (양정 벤치마크)"),
    ("쟁점 1개 이상", "쟁점있음", 0.70, "주제 1 RQ (판정 요인)"),
    ("업종 단서", "업종_단서있음", 0.50, "주제 2 연결 축"),
    ("규모 단서", "규모_단서있음", 0.50, "주제 2 규모 비교"),
    ("관할 노동위원회", "노동위원회있음", 0.50, "주제 2 지역 연결(대안)"),
]


def analyze(docs, out):
    os.makedirs(out, exist_ok=True)
    rows, fstat = [], defaultdict(lambda: [0, 0])
    for d in docs:
        body_fields = {k: v for k, v in d["fields"].items() if k not in META_KEYS and v}
        for k, v in body_fields.items():
            fstat[k][0] += 1
            fstat[k][1] += len(v)
        text = " ".join(body_fields.values())
        r = dict(결정문ID=d["id"], 사건번호=d["사건번호"], 제목=d["제목"], 본문글자수=len(text))
        r.update(extract(text))
        r["쟁점있음"] = int(r["쟁점수"] > 0)
        r["노동위원회있음"] = int(bool(r["노동위원회"]))
        # 사람이 채울 검수 칸
        r.update({"검수_업종": "", "검수_규모": "", "검수_징계수위": "", "검수_판정결과": "", "검수_메모": ""})
        rows.append(r)

    if not rows:
        sys.exit("분석할 결정문이 없습니다.")

    with open(os.path.join(out, "probe_rows.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    n = len(rows)
    with open(os.path.join(out, "field_stats.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["필드", "채워진 건수", "채움률", "평균 글자수"])
        for k, (cnt, total) in sorted(fstat.items(), key=lambda x: -x[1][0]):
            w.writerow([k, cnt, f"{cnt / n:.0%}", round(total / cnt) if cnt else 0])

    write_summary(rows, fstat, out)
    return rows


def write_summary(rows, fstat, out):
    n = len(rows)
    L = [f"# 결정문 프로브 결과 (n={n})", ""]
    L += ["## 판단 기준 대비", "", "| 항목 | 추출률 | 기준 | 통과 | 쓰는 곳 |", "|---|---|---|---|---|"]
    verdict = {}
    for name, col, th, use in THRESHOLDS:
        rate = sum(r[col] for r in rows) / n
        ok = rate >= th
        verdict[col] = ok
        L.append(f"| {name} | {rate:.0%} | {th:.0%} | {'O' if ok else 'X'} | {use} |")
    L += ["", "> 기준값은 임의 제안이며 팀에서 조정 가능. 규칙 기반 추출이라 실제보다 높거나 낮을 수 있으므로,",
          "> probe_rows.csv의 검수 칸을 20건 이상 채워 정밀도를 함께 확인할 것.", ""]

    L += ["## 해석", ""]
    if verdict["징계수위_단서있음"] and verdict["쟁점있음"]:
        L.append("- 주제 1: 징계 수위·쟁점 단서가 충분함 → 양정 벤치마크 진행 가능")
    else:
        L.append("- 주제 1: 징계 수위 또는 쟁점 단서 부족 → 요지 기반 기술통계로 범위 축소 검토")
    if verdict["업종_단서있음"] and verdict["규모_단서있음"]:
        L.append("- 주제 2: 업종·규모 단서가 충분함 → 사전 리스크 지수와 업종 단위 연결 가능")
    elif verdict["노동위원회있음"]:
        L.append("- 주제 2: 업종·규모 단서 부족, 관할 노동위원회는 있음 → 지역 단위 연결을 대안으로 검토")
    else:
        L.append("- 주제 2: 연결 축 확보 어려움 → 주제 1 중심 + 리스크 지수는 별도 기술통계로")
    L.append("")

    def dist(col, top=10):
        c = Counter(v for r in rows for v in (r[col].split(" / ") if r[col] else ["(없음)"]))
        return [f"| {k} | {v} | {v / n:.0%} |" for k, v in c.most_common(top)]

    for title, col in [("업종 후보 분포", "업종_후보"), ("징계 수위 대표값", "징계수위_대표"),
                       ("쟁점 분포", "쟁점_후보"), ("규모 구간", "규모구간"),
                       ("판정 결과(텍스트 추정)", "판정결과_텍스트추정"), ("관할 노동위원회", "노동위원회")]:
        L += [f"## {title}", "", "| 값 | 건수 | 비율 |", "|---|---|---|"] + dist(col) + [""]

    multi = sum(1 for r in rows if r["업종_후보수"] >= 2)
    L += ["## 주의 신호", "",
          f"- 업종 후보가 2개 이상 걸린 결정문: {multi}건 ({multi / n:.0%}) → 오탐 가능성, 검수 필요",
          f"- 본문 평균 글자수: {sum(r['본문글자수'] for r in rows) / n:.0f}자 (전문이 거의 없으면 수백 자 수준)", ""]

    L += ["## 본문 필드 채움률", "", "| 필드 | 채움률 | 평균 글자수 |", "|---|---|---|"]
    for k, (cnt, total) in sorted(fstat.items(), key=lambda x: -x[1][0])[:15]:
        L.append(f"| {k} | {cnt / n:.0%} | {round(total / cnt) if cnt else 0} |")

    with open(os.path.join(out, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


# ---------------------------------------------------------------------------
# 6. 자체 테스트 (네트워크 없음, 가상 판정문)
# ---------------------------------------------------------------------------

FAKE = [
    {"결정문일련번호": "T1", "판정결과": "기각",
     "판정사항": "근무 중 폭언으로 경고 2회를 받은 근로자에 대한 정직 3개월 징계의 정당성",
     "판정요지": "자동차부품 제조업을 영위하는 사용자는 상시 근로자 45명을 사용하고 있다. 징계사유가 인정되고 "
             "징계위원회를 개최하여 소명 기회를 부여하였으므로 절차상 하자가 없고 양정도 과다하지 않다. 기각한다."},
    {"결정문일련번호": "T2", "판정결과": "전부인정",
     "판정사항": "요양보호사에 대한 계약기간 만료 통보가 부당해고인지 여부",
     "판정요지": "근로자는 요양원에서 근무하였고 갱신기대권이 인정되므로 합리적 이유 없는 갱신거절은 부당해고에 해당한다. "
             "경기지방노동위원회"},
    {"결정문일련번호": "T3", "판정결과": "각하",
     "판정사항": "택시 운전기사 해고 사건",
     "판정요지": "사용자는 상시 5인 미만 사업장이므로 근로기준법 제23조가 적용되지 않아 각하한다."},
    {"결정문일련번호": "T4", "판정결과": "일부인정",
     "판정사항": "감봉 징계의 정당성",
     "판정요지": "징계사유 일부는 인정되나 서면 통지를 하지 않아 절차상 하자가 있다."},
    {"결정문일련번호": "T5", "판정결과": "각하",
     "판정사항": "부당해고 구제신청 사건",
     "판정요지": "근로자가 구제신청 기간을 도과하여 신청하였으므로 부당해고 등 구제신청을 각하한다."},
]


def selftest(out):
    docs = [dict(id=d["결정문일련번호"], 제목="", 사건번호="", fields={k: clean_text(v) for k, v in d.items()})
            for d in FAKE]
    rows = analyze(docs, out)
    by = {r["결정문ID"]: r for r in rows}
    checks = [
        (by["T1"]["상시근로자수"] == "45", "T1 상시근로자 45명"),
        (by["T1"]["규모구간"] == "30~99인", "T1 규모구간"),
        ("C:제조업" in by["T1"]["업종_후보"], "T1 제조업"),
        ("제조업" in by["T1"]["업종_명시문장"], "T1 업종 명시문장"),
        (by["T1"]["정직기간"] == "3개월", "T1 정직 3개월"),
        ("양정(과다)" in by["T1"]["쟁점_후보"], "T1 양정 쟁점"),
        ("Q:보건·사회복지" in by["T2"]["업종_후보"], "T2 보건복지"),
        ("갱신기대권" in by["T2"]["쟁점_후보"], "T2 갱신기대권"),
        (by["T2"]["징계수위_대표"] in ("해고(일반)", "계약만료·갱신거절"), "T2 수위"),
        (by["T2"]["노동위원회"] == "경기지노위", "T2 경기지노위"),
        ("H:운수·창고업" in by["T3"]["업종_후보"], "T3 운수"),
        (by["T3"]["5인미만_언급"] == 1, "T3 5인 미만"),
        (by["T4"]["징계수위_대표"] == "감봉", "T4 감봉"),
        ("절차(소명·통지·징계위)" in by["T4"]["쟁점_후보"], "T4 절차"),
        (by["T4"]["판정결과_텍스트추정"] == "일부인정", "T4 판정결과"),
        (by["T5"]["징계수위_대표"] == "", "T5 '부당해고 구제신청'을 징계 수위로 세지 않음"),
        (by["T5"]["판정결과_텍스트추정"] == "각하", "T5 각하"),
    ]
    failed = [name for ok, name in checks if not ok]
    for ok, name in checks:
        print(("  OK  " if ok else "  FAIL ") + name)
    print(f"\n{len(checks) - len(failed)}/{len(checks)} 통과. 결과 파일: {out}")
    return not failed


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="노동위원회 결정문 본문 프로브")
    sub = ap.add_subparsers(dest="mode", required=True)
    a = sub.add_parser("api", help="API로 무작위 표본 수집 후 분석")
    a.add_argument("--query", default="부당해고")
    a.add_argument("--n", type=int, default=50)
    a.add_argument("--seed", type=int, default=42)
    a.add_argument("--sleep", type=float, default=0.7, help="본문 호출 간격(초)")
    a.add_argument("--out", default="probe_out")
    c = sub.add_parser("csv", help="이미 받은 CSV로 분석")
    c.add_argument("--path", required=True)
    c.add_argument("--out", default="probe_out")
    s = sub.add_parser("selftest", help="가상 판정문으로 동작 확인")
    s.add_argument("--out", default="probe_selftest")
    args = ap.parse_args()

    if args.mode == "api":
        analyze(run_api(args), args.out)
        print(f"완료 → {args.out}/summary.md")
    elif args.mode == "csv":
        analyze(run_csv(args), args.out)
        print(f"완료 → {args.out}/summary.md")
    else:
        sys.exit(0 if selftest(args.out) else 1)


if __name__ == "__main__":
    main()
    