"""
OpenDART 수집기: 부가가치(한국은행 방식) 계산에 빠진 계정 + 연도별 직원 현황

한국은행 기업경영분석 부가가치 = 영업잉여 + 인건비 + 금융비용 + 조세공과 + 감가상각비
기존 DART 통합 파일에는 매출·영업이익·자산·부채·순이익만 있으므로 아래를 추가로 받는다.

  fs   : 단일회사 전체 재무제표 (fnlttSinglAcntAll) → 감가상각비, 상각비, 이자비용, 금융원가,
         종업원급여, 세금과공과, 임차료 후보 계정
  emp  : 직원 현황 (empSttus) → 연간급여총액(인건비 근사), 직원 수 (3년 평균용 과거 연도)
  build: 위 두 결과 + 기존 통합 CSV → 기업×연도 부가가치 표

사용법 (PowerShell)
  0) 이 스크립트와 같은 폴더에 .env 파일을 만들고 한 줄 작성:  DART_KEY=발급받은40자리키
     (.env 는 .gitignore 에 추가해서 깃허브에 올리지 않기)
  python dart_value_added.py fs  --corp-csv DART통합.csv --years 2025 --limit 20     # 20곳 시험
  python dart_value_added.py fs  --corp-csv DART통합.csv --years 2023 2024 2025
  python dart_value_added.py emp --corp-csv DART통합.csv --years 2023 2024 2025
  python dart_value_added.py build --corp-csv DART통합.csv --years 2025
  python dart_value_added.py selftest

특징
  - 이어받기: 받은 (기업, 연도, 종류)는 raw/ 폴더에 JSON으로 저장되어 다시 받지 않음
  - 계정 매칭 근거(어떤 계정명·ID로 잡았는지)를 match_log.csv에 남겨 사람이 검수 가능
  - 키 오류·일일 한도 초과(상태코드 010/011/012/020 등)는 즉시 중단
  - OFS(별도) 기준이 기본값: DART 직원 수가 별도 법인 기준이므로 범위를 맞춤

주의
  - 금융원가(FinanceCosts)는 외환손실 등이 섞일 수 있어 '이자비용' 계정을 우선 사용
  - K-IFRS 1116 이후 리스료 상당액은 '사용권자산 감가상각비 + 리스 이자'로 표시됨 → 임차료 계정은 대부분 비어 있는 것이 정상
  - 종업원급여·세금과공과는 손익계산서를 '성격별'로 표시한 회사만 본표에 있음(대부분 주석) → 확보율 낮을 수 있음
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

API = "https://opendart.fss.or.kr/api/{}.json"
STOP_CODES = {"010": "등록되지 않은 키", "011": "사용할 수 없는 키", "012": "접근할 수 없는 IP",
              "020": "요청 제한 초과(일일 한도)", "021": "조회 가능한 회사 개수 초과", "100": "필드 부적절",
              "800": "시스템 점검", "900": "정의되지 않은 오류", "901": "개인정보 보유기간 만료 키"}
NO_DATA = "013"

# (목표 항목, 허용 재무제표 구분, 우선순위별 규칙[(account_id 집합, 계정명 정규식)])
TARGETS = [
    ("영업이익", {"IS", "CIS"}, [({"dart_OperatingIncomeLoss"}, None), (set(), r"^영업(이익|손실|이익손실)$")]),
    # 현금흐름표 조정 항목은 '…에 대한 조정', '… 조정'처럼 꼬리가 붙는 경우가 많아 허용
    ("감가상각비", {"CF"}, [({"ifrs-full_AdjustmentsForDepreciationExpense"}, None),
                          (set(), r"^(유형자산)?감가상각비(에대한)?(조정)?$"),
                          (set(), r"^감가상각비(및|와)?(무형자산)?(상각비)?(에대한)?(조정)?$")]),
    ("사용권자산감가상각비", {"CF"}, [(set(), r"^사용권자산(감가)?상각비(에대한)?(조정)?$")]),
    ("무형자산상각비", {"CF"}, [({"ifrs-full_AdjustmentsForAmortisationExpense"}, None),
                            (set(), r"^무형자산(감가)?상각비(에대한)?(조정)?$")]),
    ("이자비용", {"IS", "CIS"}, [(set(), r"^이자비용$")]),
    ("금융원가", {"IS", "CIS"}, [({"ifrs-full_FinanceCosts"}, None), (set(), r"^금융(원가|비용)$")]),
    ("이자지급_CF", {"CF"}, [({"ifrs-full_InterestPaidClassifiedAsOperatingActivities",
                             "ifrs-full_InterestPaidClassifiedAsFinancingActivities"}, None),
                           (set(), r"^이자(의)?지급(액)?$")]),
    ("종업원급여", {"IS", "CIS"}, [({"ifrs-full_EmployeeBenefitsExpense"}, None),
                              (set(), r"^(종업원급여|인건비|급여)$")]),
    ("세금과공과", {"IS", "CIS"}, [(set(), r"^세금과공과(금)?$")]),
    ("임차료", {"IS", "CIS"}, [(set(), r"^(지급)?임차료$")]),
]

RAW = "raw"


def load_dotenv():
    """스크립트 폴더 또는 현재 폴더의 .env 에서 KEY=VALUE 를 읽는다(이미 설정된 환경변수는 덮어쓰지 않음)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for folder in (here, os.getcwd()):
        path = os.path.join(folder, ".env")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        return path
    return None


def key():
    src = load_dotenv()
    k = os.environ.get("DART_KEY", "").strip()
    if not k:
        sys.exit("DART_KEY가 없습니다. 스크립트와 같은 폴더에 .env 파일을 만들고 DART_KEY=인증키 를 적어 주세요.")
    if k != "test" and not re.fullmatch(r"[0-9a-fA-F]{40}", k):
        sys.exit(f"DART_KEY 형식이 이상합니다(40자리 영문·숫자가 아님, 길이 {len(k)}). 공백·따옴표를 확인하세요.")
    return k


def http_json(name, params, timeout=30):
    url = API.format(name) + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


FETCH = http_json  # selftest에서 교체


def get(name, params, retries=3):
    last = None
    for i in range(retries):
        try:
            data = FETCH(name, params)
            st = str(data.get("status", ""))
            if st in STOP_CODES:
                raise SystemExit(f"[중단] DART 상태코드 {st}: {STOP_CODES[st]} - {data.get('message')}")
            return data
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001  네트워크 오류는 재시도
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"요청 실패 {name} {params.get('corp_code')}: {last}")


def resolve(path):
    """파일이 없으면 한글 정규화(NFC/NFD) 차이를 무시하고 같은 폴더에서 다시 찾는다."""
    if os.path.exists(path):
        return path
    import unicodedata
    folder = os.path.dirname(path) or "."
    want = unicodedata.normalize("NFC", os.path.basename(path)).lower()
    files = os.listdir(folder)
    for f in files:
        if unicodedata.normalize("NFC", f).lower() == want:
            return os.path.join(folder, f)
    csvs = [unicodedata.normalize("NFC", f) for f in files if f.lower().endswith(".csv")]
    sys.exit(f"파일을 찾을 수 없습니다: {path}\n이 폴더의 CSV 파일: {csvs}\n위 이름 중 하나를 --corp-csv 에 넣어 주세요.")


def corp_codes(path, limit):
    with open(resolve(path), encoding="utf-8-sig", newline="") as f:
        codes = [r["DART기업코드"].strip().zfill(8) for r in csv.DictReader(f) if r.get("DART기업코드")]
    return codes[:limit] if limit else codes


def raw_path(kind, corp, year, extra=""):
    return os.path.join(RAW, kind, f"{corp}_{year}{extra}.json")


def collect(kind, args):
    k = key()
    codes = corp_codes(args.corp_csv, args.limit)
    jobs = [(c, y) for y in args.years for c in codes]
    os.makedirs(os.path.join(RAW, kind), exist_ok=True)
    done = skip = nodata = 0
    for n, (corp, year) in enumerate(jobs, 1):
        p = raw_path(kind, corp, year, f"_{args.fs_div}" if kind == "fs" else "")
        if os.path.exists(p):
            skip += 1
            continue
        params = {"crtfc_key": k, "corp_code": corp, "bsns_year": str(year), "reprt_code": "11011"}
        if kind == "fs":
            params["fs_div"] = args.fs_div
        data = get("fnlttSinglAcntAll" if kind == "fs" else "empSttus", params)
        if str(data.get("status")) == NO_DATA:
            nodata += 1
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        done += 1
        time.sleep(args.sleep)
        if n % 100 == 0:
            print(f"[{kind}] {n}/{len(jobs)} 신규 {done}, 건너뜀 {skip}, 데이터없음 {nodata}")
    print(f"[{kind}] 끝: 신규 {done}, 건너뜀 {skip}, 데이터없음 {nodata} (raw/{kind}/)")


def amount(s):
    s = (s or "").replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def norm(s):
    return re.sub(r"[\s()（）\[\]·ㆍ,]", "", s or "")


def match_targets(rows):
    """한 회사·연도의 전체 재무제표 행에서 목표 항목을 찾는다. 반환: {항목: (금액, 근거)}"""
    found = {}
    for target, sj_ok, rules in TARGETS:
        for ids, pattern in rules:
            hits = []
            for r in rows:
                if r.get("sj_div") not in sj_ok:
                    continue
                aid, nm = r.get("account_id", ""), norm(r.get("account_nm"))
                if (ids and aid in ids) or (pattern and re.match(pattern, nm)):
                    v = amount(r.get("thstrm_amount"))
                    if v is not None:
                        hits.append((v, f"{r.get('sj_div')}|{aid}|{r.get('account_nm')}"))
            if hits:
                v, why = hits[0]  # 같은 규칙에 여러 행이면 첫 행(본표 순서) 사용, 개수는 근거에 기록
                found[target] = (v, why + (f" (후보 {len(hits)}개)" if len(hits) > 1 else ""))
                break
    return found


def emp_summary(rows):
    """직원 현황 행(성별·부문별)을 합산. 합계/소계 행이 섞이는 경우를 피하려고 '합계' 표기 행은 제외."""
    tot_n = tot_pay = 0.0
    used = 0
    for r in rows:
        if "합계" in (r.get("fo_bbm") or "") or "합계" in (r.get("sexdstn") or ""):
            continue
        n, pay = amount(r.get("sm")), amount(r.get("fyer_salary_totamt"))
        if n:
            tot_n += n
            used += 1
        if pay:
            tot_pay += pay
    return tot_n or None, tot_pay or None, used


def bok_prefixes(code):
    """한국은행 업종코드 → 표준산업분류 숫자 접두어 목록.
    예) C10-34 → 10..34 / C261,2 → 261,262 / G472-8,G4792-9 → 472..478, 4792..4799 / M714,7153,716"""
    if code == "ALL":
        return [""]
    out, prev = [], ""
    for tok in code.replace(" ", "").split(","):
        tok = re.sub(r"^[A-Z]+", "", tok)
        if not tok:
            continue
        if len(tok) == 1 and prev:          # 한 자리 = 앞 코드의 마지막 자리만 바꿈
            tok = prev[:-1] + tok
        if "-" in tok:
            a, b = tok.split("-", 1)
            if len(b) < len(a):
                b = a[: len(a) - len(b)] + b
            out += [str(i).zfill(len(a)) for i in range(int(a), int(b) + 1)]
            prev = a
        else:
            out.append(tok)
            prev = tok
    return out


def load_bok_ratios(path, year="2025", size="전체"):
    """bok_pdf_to_csv.py 결과에서 업종별 '감가상각비 ÷ (영업잉여 + 인건비)' 비율을 만든다."""
    comp = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r["규모"] == size and r["연도"] == year and r["지표코드"] in ("1001", "1002", "1005") and r["값"]:
                comp.setdefault(r["업종코드"], {})[r["지표코드"]] = float(r["값"])
    table = []
    for code, c in comp.items():
        if len(c) == 3 and c["1001"] + c["1002"] > 0:
            ratio = c["1005"] / (c["1001"] + c["1002"])
            for p in bok_prefixes(code):
                table.append((p, code, ratio))
    table.sort(key=lambda x: -len(x[0]))    # 가장 세분된 업종부터 매칭
    return table


def match_bok(table, ksic):
    ksic = re.sub(r"\D", "", ksic or "")
    for p, code, ratio in table:
        if ksic.startswith(p):
            return code, ratio
    return None, None


def build(args):
    with open(resolve(args.corp_csv), encoding="utf-8-sig", newline="") as f:
        base = {r["DART기업코드"].strip().zfill(8): r for r in csv.DictReader(f)}
    bok = load_bok_ratios(resolve(args.bok_csv)) if args.bok_csv else []
    out_rows, log_rows = [], []
    for year in args.years:
        for corp, b in base.items():
            row = {"DART기업코드": corp, "기업명": b.get("기업명"), "업종코드": b.get("업종코드"), "연도": year}
            p = raw_path("fs", corp, year, f"_{args.fs_div}")
            if os.path.exists(p):
                data = json.load(open(p, encoding="utf-8"))
                for t, (v, why) in match_targets(data.get("list", [])).items():
                    row[t + "_백만원"] = round(v / 1e6, 3)
                    log_rows.append({"DART기업코드": corp, "연도": year, "항목": t, "근거": why})
            pe = raw_path("emp", corp, year)
            if os.path.exists(pe):
                n, pay, used = emp_summary(json.load(open(pe, encoding="utf-8")).get("list", []))
                row["직원수_emp"] = n
                row["연간급여총액_백만원"] = round(pay / 1e6, 3) if pay else None
            # 영업이익: 전체 재무제표 값 우선, 없으면 기존 통합 파일(2025) 값
            op = row.get("영업이익_백만원")
            if op is None and str(year) == "2025":
                op = amount(b.get("영업이익_OFS_백만원"))
            labor = row.get("연간급여총액_백만원") or (
                (amount(b.get("직원수_명")) or 0) * (amount(b.get("연평균급여_원_가중추정")) or 0) / 1e6 or None)
            fin = row.get("이자비용_백만원") or row.get("이자지급_CF_백만원")  # 금융원가는 외환손실 섞일 수 있어 후순위
            dep = sum(filter(None, [row.get("감가상각비_백만원"), row.get("사용권자산감가상각비_백만원")])) or None
            row["fs_수집여부"] = os.path.exists(p)
            if bok:
                code, ratio = match_bok(bok, b.get("업종코드"))
                row["한국은행_매칭업종"], row["추정비율_감가상각"] = code, round(ratio, 4) if ratio else None
                if op is not None and labor and ratio is not None and op + labor > 0:
                    row["감가상각비_추정_백만원"] = round((op + labor) * ratio, 3)
            # 업종 비율 추정은 기업 단위 오차가 커서(20곳 시험: −75% ~ +2,483%) 부가가치 계산에 쓰지 않고 참고 열로만 둔다
            row["감가상각비_출처"] = "실제(DART)" if dep is not None else "없음(주석에만 있음)"
            row["감가상각비_적용_백만원"] = dep
            parts = {"영업이익": op, "인건비": labor, "금융비용": fin, "감가상각비": dep,
                     "조세공과": row.get("세금과공과_백만원")}
            if op is not None and labor:
                row["부가가치_간이_백만원"] = round(op + labor, 3)
                row["부가가치_보완_백만원"] = round(sum(v for v in parts.values() if v), 3)
                row["포함항목"] = "+".join(k for k, v in parts.items() if v)
            out_rows.append(row)
    cols = sorted({k for r in out_rows for k in r}, key=lambda c: (c not in ("DART기업코드", "기업명", "업종코드", "연도"), c))
    with open("value_added_panel.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(out_rows)
    with open("match_log.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["DART기업코드", "연도", "항목", "근거"])
        w.writeheader()
        w.writerows(log_rows)
    n = len(out_rows)
    got = [r for r in out_rows if r.get("fs_수집여부")]
    m = len(got) or 1
    print(f"[build] {n}행 → value_added_panel.csv, 매칭 근거 → match_log.csv")
    print(f"  ※ 아래 비율의 분모는 전체 재무제표를 받은 {len(got)}개 (기업×연도)")
    for t, *_ in TARGETS:
        c = sum(1 for r in got if r.get(t + "_백만원") is not None)
        print(f"  {t:12s} {c:5d} / {len(got)} ({c / m:.0%})")
    if bok:
        # 참고용: 실제 값이 있는 기업으로 업종 비율 추정의 오차 확인 (추정값은 부가가치에 넣지 않음)
        errs = sorted(abs(r["감가상각비_추정_백만원"] / r["감가상각비_적용_백만원"] - 1)
                      for r in got if r.get("감가상각비_출처") == "실제(DART)"
                      and r.get("감가상각비_추정_백만원") and r.get("감가상각비_적용_백만원"))
        if errs:
            print(f"  추정 검증: 실제 값 있는 {len(errs)}곳, 상대오차 중앙값 {errs[len(errs) // 2]:.0%} "
                  f"(작을수록 좋음, 표본이 적으면 참고용)")
    return out_rows


def selftest():
    import shutil
    import tempfile
    global FETCH
    tmp = tempfile.mkdtemp()
    os.chdir(tmp)
    with open("corp.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["DART기업코드", "기업명", "업종코드", "직원수_명", "연평균급여_원_가중추정", "영업이익_OFS_백만원"])
        w.writerow(["00000001", "가상제조", "264", "100", "50000000", "1000"])
        w.writerow(["00000002", "가상서비스", "582", "50", "60000000", "-200"])

    def fake(name, p):
        if p["corp_code"] == "00000002" and name == "fnlttSinglAcntAll":
            return {"status": "013", "message": "조회된 데이타가 없습니다."}
        if name == "fnlttSinglAcntAll":
            return {"status": "000", "list": [
                {"sj_div": "CF", "account_id": "ifrs-full_AdjustmentsForDepreciationExpense", "account_nm": "감가상각비", "thstrm_amount": "300,000,000"},
                {"sj_div": "CF", "account_id": "-표준계정코드 미사용-", "account_nm": "사용권자산 감가상각비", "thstrm_amount": "50000000"},
                {"sj_div": "CIS", "account_id": "ifrs-full_FinanceCosts", "account_nm": "금융원가", "thstrm_amount": "90000000"},
                {"sj_div": "CIS", "account_id": "dart_InterestExpenseFinanceExpense", "account_nm": "이자비용", "thstrm_amount": "40000000"},
                {"sj_div": "IS", "account_id": "x", "account_nm": "세금과공과", "thstrm_amount": "-"},
                {"sj_div": "CIS", "account_id": "dart_OperatingIncomeLoss", "account_nm": "영업이익(손실)", "thstrm_amount": "1,000,000,000"}]}
        return {"status": "000", "list": [
            {"fo_bbm": "전사", "sexdstn": "남", "sm": "70", "fyer_salary_totamt": "3,600,000,000"},
            {"fo_bbm": "전사", "sexdstn": "여", "sm": "30", "fyer_salary_totamt": "1,400,000,000"},
            {"fo_bbm": "합계", "sexdstn": "", "sm": "100", "fyer_salary_totamt": "5,000,000,000"}]}

    FETCH = fake
    os.environ.setdefault("DART_KEY", "test")
    ns = argparse.Namespace(corp_csv="corp.csv", years=[2025], limit=0, fs_div="OFS", sleep=0, bok_csv=None)
    collect("fs", ns)
    collect("emp", ns)
    collect("fs", ns)  # 이어받기 확인
    rows = {r["DART기업코드"]: r for r in build(ns)}
    a, b = rows["00000001"], rows["00000002"]
    checks = [
        (a["감가상각비_백만원"] == 300.0, "감가상각비 표준 ID 매칭"),
        (a["사용권자산감가상각비_백만원"] == 50.0, "사용권자산 감가상각비 분리"),
        (a["이자비용_백만원"] == 40.0 and a["금융원가_백만원"] == 90.0, "이자비용·금융원가 구분"),
        (a.get("세금과공과_백만원") is None, "'-' 값은 결측 처리"),
        (a["연간급여총액_백만원"] == 5000.0 and a["직원수_emp"] == 100, "직원현황 합계행 제외 후 합산"),
        (a["부가가치_보완_백만원"] == 1000 + 5000 + 40 + 350, "보완 부가가치 = 영업이익+인건비+이자비용+감가상각"),
        (a["부가가치_간이_백만원"] == 6000, "간이 부가가치"),
        ("감가상각비_백만원" not in b, "데이터 없음(013) 회사는 결측"),
        (os.path.exists("match_log.csv"), "매칭 근거 기록"),
    ]
    for ok, name in checks:
        print(("  OK   " if ok else "  FAIL ") + name)
    os.chdir("/")
    shutil.rmtree(tmp, ignore_errors=True)
    return all(ok for ok, _ in checks)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["fs", "emp", "build", "selftest"])
    ap.add_argument("--corp-csv")
    ap.add_argument("--years", nargs="+", type=int, default=[2025])
    ap.add_argument("--fs-div", default="OFS", choices=["OFS", "CFS"])
    ap.add_argument("--limit", type=int, default=0, help="시험용: 앞에서부터 N개 기업만")
    ap.add_argument("--sleep", type=float, default=0.5, help="요청 간격(초)")
    ap.add_argument("--bok-csv", help="bok_pdf_to_csv.py 결과 CSV. 주면 감가상각비가 없는 기업을 업종 구성비로 추정")
    a = ap.parse_args()
    if a.mode == "selftest":
        sys.exit(0 if selftest() else 1)
    if not a.corp_csv:
        sys.exit("--corp-csv 가 필요합니다")
    if a.mode in ("fs", "emp"):
        collect(a.mode, a)
    else:
        build(a)


if __name__ == "__main__":
    main()