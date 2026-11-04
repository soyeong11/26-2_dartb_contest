"""
인적자본 생산성 분석 (로드맵 ①~④)

  ① 정제   : 금융업·지주회사·소규모·결측 기업에 '제외 사유' 표시
  ② 지표   : 1인당 간이 부가가치, HC ROI, 노동소득분배율(근사), 1인당 매출총이익, 자본생산성
  ③ 동종 비교: 업종(중분류, 표본 부족 시 대분류) × 규모 안 백분위 + 한국은행 업종 기준값과 비교
  ④ 간이식 검증: 감가상각비 실제 값이 있는 기업에서 '간이식 vs 보완식' 순위 상관(스피어만)

입력 (crawl 폴더)
  value_added_panel.csv   dart_value_added.py build 결과
  dart_merged.csv         DART 통합 파일 (직원 수·매출 등)
  bok_2025_long.csv       bok_pdf_to_csv.py 결과
  raw/fs/                 전체 재무제표 원본 (매출원가·유형자산 추출용, 없어도 동작)

사용법
  python hr_productivity.py --panel value_added_panel.csv --corp-csv dart_merged.csv --bok-csv bok_2025_long.csv

출력
  firm_productivity.csv   기업별 지표·제외 사유·동종 백분위·한국은행 비교
  validation_summary.csv  업종별 간이식 검증 결과
  화면 요약               표본 선정 과정, 업종별 중앙값, 검증 결과

해석 원칙
  - 모든 값은 상장사 별도재무제표 기준. 인건비는 직원 급여(임원·퇴직급여·복리후생 제외)라 실제보다 작음
  - 백분위는 '같은 업종×규모 안에서의 상대 위치'이며 업종 간 절대 비교에 쓰지 않음
  - 규모는 DART에 공식 구분이 없어 직원 수로 근사
"""

import argparse
import csv
import glob
import json
import os
import re
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from dart_value_added import amount, bok_prefixes, norm, resolve  # noqa: E402

SECTIONS = [(1, 3, "A"), (5, 8, "B"), (10, 34, "C"), (35, 35, "D"), (36, 39, "E"), (41, 42, "F"), (45, 47, "G"),
            (49, 52, "H"), (55, 56, "I"), (58, 63, "J"), (64, 66, "K"), (68, 68, "L"), (70, 73, "M"), (74, 76, "N"),
            (84, 84, "O"), (85, 85, "P"), (86, 87, "Q"), (90, 91, "R"), (94, 96, "S")]
SECTION_NAME = {"A": "농림어업", "B": "광업", "C": "제조업", "D": "전기가스", "E": "수도·폐기물", "F": "건설업",
                "G": "도소매", "H": "운수·창고", "I": "숙박·음식", "J": "정보통신", "K": "금융·보험", "L": "부동산",
                "M": "전문·과학·기술", "N": "사업지원", "O": "공공행정", "P": "교육", "Q": "보건·복지",
                "R": "예술·여가", "S": "기타서비스"}
# 업종별 주지표 (설계안 3-3)
MAIN_METRIC = {"G": "1인당매출총이익_백만원", "J": "HC_ROI", "M": "HC_ROI"}  # 규칙표가 없을 때만 사용
DEFAULT_METRIC = "1인당간이부가가치_백만원"


def load_rules(path):
    """productivity_rules.csv → [(중분류 집합 또는 None(*), 규칙 dict)], 좁은 범위가 먼저 매칭되도록 정렬"""
    rules = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            rng = r["KSIC중분류"].strip()
            if rng == "*":
                codes = None
            else:
                codes = set()
                for part in rng.split(","):
                    lo, _, hi = part.strip().partition("-")
                    codes |= set(range(int(lo), int(hi or lo) + 1))
            rules.append((codes, r))
    rules.sort(key=lambda x: 10**6 if x[0] is None else len(x[0]))
    return rules


def rule_for(rules, mid):
    n = int(mid) if str(mid).isdigit() else None
    for codes, r in rules:
        if codes is None or (n is not None and n in codes):
            return r
    return None

# raw/fs 에서 추가로 꺼낼 계정
EXTRA = [
    ("매출액_fs", {"IS", "CIS"}, [({"ifrs-full_Revenue"}, None), (set(), r"^(매출액|수익매출액|영업수익|매출)$")]),
    ("매출원가_fs", {"IS", "CIS"}, [({"ifrs-full_CostOfSales"}, None), (set(), r"^(매출원가|영업원가)$")]),
    ("유형자산_fs", {"BS"}, [({"ifrs-full_PropertyPlantAndEquipment"}, None), (set(), r"^유형자산$")]),
]


def section(code):
    d = re.sub(r"\D", "", str(code or ""))[:2]
    if not d:
        return None
    n = int(d)
    for a, b, s in SECTIONS:
        if a <= n <= b:
            return s
    return None


def extract_extra(raw_dir, year, fs_div):
    out = {}
    for p in glob.glob(os.path.join(raw_dir, "fs", f"*_{year}_{fs_div}.json")):
        corp = os.path.basename(p).split("_")[0]
        rows = json.load(open(p, encoding="utf-8")).get("list", [])
        rec = {}
        for target, sj_ok, rules in EXTRA:
            for ids, pattern in rules:
                hit = next((amount(r.get("thstrm_amount")) for r in rows if r.get("sj_div") in sj_ok and (
                    (ids and r.get("account_id") in ids) or (pattern and re.match(pattern, norm(r.get("account_nm")))))
                    and amount(r.get("thstrm_amount")) is not None), None)
                if hit is not None:
                    rec[target] = hit / 1e6
                    break
        out[corp] = rec
    return out


def load_bok(path, year="2025"):
    bok = pd.read_csv(path, dtype=str)
    bok = bok[(bok["연도"] == year) & bok["지표코드"].isin(["905", "1002"])]
    piv = bok.pivot_table(index=["업종코드", "규모"], columns="지표코드", values="값", aggfunc="first").reset_index()
    piv = piv.rename(columns={"905": "BOK_노동소득분배율", "1002": "BOK_인건비비중"})
    for c in ["BOK_노동소득분배율", "BOK_인건비비중"]:
        piv[c] = pd.to_numeric(piv[c], errors="coerce")
    prefixes = sorted({(p, code) for code in piv["업종코드"].unique() for p in bok_prefixes(code)}, key=lambda x: -len(x[0]))
    return piv, prefixes


def match_bok_code(prefixes, ksic):
    k = re.sub(r"\D", "", str(ksic or ""))
    return next((code for p, code in prefixes if k.startswith(p)), None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default="value_added_panel.csv")
    ap.add_argument("--corp-csv", default="dart_merged.csv")
    ap.add_argument("--bok-csv", default="bok_2025_long.csv")
    ap.add_argument("--raw", default="raw")
    ap.add_argument("--year", default="2025")
    ap.add_argument("--fs-div", default="OFS")
    ap.add_argument("--min-emp", type=float, default=10, help="이 직원 수 미만은 제외(1인당 지표 극단값 방지)")
    ap.add_argument("--size-cut", type=float, default=300, help="규모 구분 직원 수 기준")
    ap.add_argument("--min-peer", type=int, default=10, help="동종 비교 최소 기업 수")
    ap.add_argument("--rules", default="productivity_rules.csv", help="업종별 생산성 산식 규칙표")
    a = ap.parse_args()

    panel = pd.read_csv(resolve(a.panel), dtype={"DART기업코드": str, "업종코드": str})
    panel = panel[panel["연도"].astype(str) == str(a.year)].copy()
    base = pd.read_csv(resolve(a.corp_csv), dtype={"DART기업코드": str, "업종코드": str, "종목코드": str}, encoding="utf-8-sig")
    base["DART기업코드"] = base["DART기업코드"].str.zfill(8)
    panel["DART기업코드"] = panel["DART기업코드"].str.zfill(8)
    keep = ["DART기업코드", "종목코드", "시장", "직원수_명", "연평균급여_원_가중추정", "매출액_OFS_백만원", "영업이익_OFS_백만원", "자산총계_OFS_백만원"]
    df = panel.merge(base[[c for c in keep if c in base.columns]], on="DART기업코드", how="left")

    extra = extract_extra(a.raw, a.year, a.fs_div) if os.path.isdir(a.raw) else {}
    for col in ["매출액_fs", "매출원가_fs", "유형자산_fs"]:
        df[col] = df["DART기업코드"].map(lambda c, col=col: extra.get(c, {}).get(col))

    def num(c):
        """열 이름 또는 Series → 숫자 Series (열이 없으면 결측으로 채움)"""
        if isinstance(c, str):
            c = df[c] if c in df.columns else pd.Series(float("nan"), index=df.index)
        return pd.to_numeric(c, errors="coerce")

    df["직원수"] = num("직원수_emp").fillna(num("직원수_명"))
    df["인건비_백만원"] = num("연간급여총액_백만원").fillna(num("직원수_명") * num("연평균급여_원_가중추정") / 1e6)
    df["영업이익"] = num("영업이익_백만원").fillna(num("영업이익_OFS_백만원"))
    df["이자_백만원"] = num("이자비용_백만원").fillna(num("이자지급_CF_백만원"))
    df["감가상각_실제"] = num("감가상각비_적용_백만원")
    df["매출"] = num("매출액_fs").fillna(num("매출액_OFS_백만원"))
    df["대분류"] = df["업종코드"].map(section)
    df["중분류"] = df["업종코드"].astype(str).str.replace(r"\D", "", regex=True).str[:2]

    # ① 정제: 제외 사유
    name = df["기업명"].fillna("")
    code = df["업종코드"].astype(str)
    reasons = pd.Series([[] for _ in range(len(df))], index=df.index)

    def flag(mask, text):
        for i in df.index[mask.fillna(False)]:
            reasons[i].append(text[i] if isinstance(text, pd.Series) else text)

    rules = load_rules(resolve(a.rules)) if os.path.exists(a.rules) else None
    if rules:
        rr = df["중분류"].map(lambda m: rule_for(rules, m) or {})
        df["적용규칙"] = rr.map(lambda r: r.get("규칙ID"))
        proc = rr.map(lambda r: r.get("처리", "분석"))
        flag(proc.eq("제외"), "규칙상 제외 업종(" + rr.map(lambda r: r.get("업종명", "")) + ")")
        flag(proc.eq("보류"), "생산성 판단 보류 업종(" + rr.map(lambda r: r.get("업종명", "")) + ")")
    else:
        print(f"[알림] 규칙표 {a.rules} 가 없어 기본 규칙을 사용합니다.")
        flag(df["대분류"].eq("K"), "금융·보험업(별도 지표 필요)")
    flag(name.str.contains(r"홀딩스|지주|Holdings|홀딩", case=False) | code.str.startswith(("7152", "64992")), "지주회사(별도재무 ≠ 실제 사업)")
    flag(df["직원수"].isna() | (df["직원수"] < a.min_emp), f"직원 수 결측 또는 {a.min_emp:g}명 미만")
    flag(df["인건비_백만원"].isna() | (df["인건비_백만원"] <= 0), "인건비 결측")
    flag(df["영업이익"].isna(), "영업이익 결측")
    flag(df["대분류"].isna(), "업종코드 결측")
    df["제외사유"] = reasons.map(lambda r: "; ".join(r))
    df["분석대상"] = df["제외사유"].eq("")

    # ② 지표
    va = df["영업이익"] + df["인건비_백만원"]
    df["간이부가가치_백만원"] = va
    df["1인당간이부가가치_백만원"] = va / df["직원수"]
    df["HC_ROI"] = va / df["인건비_백만원"]
    df["노동소득분배율_근사"] = 100 * df["인건비_백만원"] / (va + df["이자_백만원"].fillna(0))
    df.loc[(va + df["이자_백만원"].fillna(0)) <= 0, "노동소득분배율_근사"] = None  # 분모 음수는 해석 불가(한국은행도 '*')
    df["보완부가가치_백만원"] = va + df["이자_백만원"].fillna(0) + df["감가상각_실제"]
    df["1인당보완부가가치_백만원"] = df["보완부가가치_백만원"] / df["직원수"]
    df["1인당매출총이익_백만원"] = (df["매출"] - num("매출원가_fs")) / df["직원수"]
    df["자본생산성_간이"] = va / num("유형자산_fs").where(num("유형자산_fs") > 0)
    gp = df["매출"] - num("매출원가_fs")
    df["인건비_매출총이익_비율"] = df["인건비_백만원"] / gp.where(gp > 0)

    # 업종 규칙 적용: 주지표·보조지표 선택
    if rules:
        df["주지표"] = rr.map(lambda r: r.get("주지표_열") or DEFAULT_METRIC)
        df["주지표_산식"] = rr.map(lambda r: r.get("주지표_산식") or "(영업이익 + 인건비) ÷ 직원 수")
        df["보조지표"] = rr.map(lambda r: r.get("보조지표_열") or "")
        df["보조지표_산식"] = rr.map(lambda r: r.get("보조지표_산식") or "")
        df["주의사항"] = rr.map(lambda r: r.get("한계_주의") or "")
    else:
        df["주지표"] = df["대분류"].map(lambda s: MAIN_METRIC.get(s, DEFAULT_METRIC))
        df["주지표_산식"], df["보조지표"], df["보조지표_산식"], df["주의사항"] = "", "", "", ""
    # 업종 주지표 값이 없으면(예: 도소매인데 매출원가 미확보) 기본 지표로 대체하고 그 사실을 남김
    missing = df.apply(lambda r: pd.isna(r[r["주지표"]]), axis=1)
    df.loc[missing & df["주지표"].ne(DEFAULT_METRIC), "주의사항"] = "업종 주지표 계산 불가 → 1인당 간이부가가치로 대체"
    df.loc[missing, "주지표_산식"] = "(영업이익 + 인건비) ÷ 직원 수"
    df.loc[missing, "주지표"] = DEFAULT_METRIC
    df["주지표값"] = df.apply(lambda r: r[r["주지표"]], axis=1)
    df["보조지표값"] = df.apply(lambda r: r[r["보조지표"]] if r["보조지표"] in df.columns else None, axis=1)

    # ③ 동종 비교
    df["규모구간"] = (df["직원수"] >= a.size_cut).map({True: f"{a.size_cut:g}명 이상", False: f"{a.size_cut:g}명 미만"})
    t = df[df["분석대상"]]
    n_mid = t.groupby(["중분류", "규모구간"]).size()
    n_sec = t.groupby(["대분류", "규모구간"]).size()

    def peer_key(r):
        if n_mid.get((r["중분류"], r["규모구간"]), 0) >= a.min_peer:
            return f"중분류 {r['중분류']} × {r['규모구간']}"
        if n_sec.get((r["대분류"], r["규모구간"]), 0) >= a.min_peer:
            return f"대분류 {r['대분류']} × {r['규모구간']}"
        return f"대분류 {r['대분류']} 전체"

    df["비교집단"] = df.apply(peer_key, axis=1)
    for m in ["1인당간이부가가치_백만원", "HC_ROI", "주지표값", "보조지표값"]:
        df[m + "_백분위"] = None
        tt = df[df["분석대상"]]
        # 같은 비교집단이라도 업종 규칙상 주지표가 다를 수 있어(대분류로 넓힌 경우) 지표 종류까지 묶어서 순위
        key = ["비교집단"] + (["주지표"] if m == "주지표값" else ["보조지표"] if m == "보조지표값" else [])
        df.loc[tt.index, m + "_백분위"] = tt.groupby(key)[m].rank(pct=True).mul(100).round(1)
    df["비교집단_기업수"] = df.loc[df["분석대상"]].groupby("비교집단")["DART기업코드"].transform("count")

    if os.path.exists(a.bok_csv) or True:
        piv, prefixes = load_bok(resolve(a.bok_csv), a.year)
        df["BOK_업종"] = df["업종코드"].map(lambda c: match_bok_code(prefixes, c))
        allsize = piv[piv["규모"] == "전체"].set_index("업종코드")
        df["BOK_노동소득분배율_전체"] = df["BOK_업종"].map(allsize["BOK_노동소득분배율"])
        df["BOK_인건비비중_전체"] = df["BOK_업종"].map(allsize["BOK_인건비비중"])
        df["노동소득분배율_차이_pp"] = df["노동소득분배율_근사"] - df["BOK_노동소득분배율_전체"]

    # ④ 간이식 검증
    v = df[df["분석대상"] & df["감가상각_실제"].notna() & (df["감가상각_실제"] > 0)]
    val_rows = []
    for grp, g in [("전체", v)] + [(SECTION_NAME.get(s, s), g) for s, g in v.groupby("대분류")]:
        if len(g) < 10:
            val_rows.append({"업종": grp, "기업수": len(g), "스피어만": None, "감가상각비_비중_중앙값": None, "판단": "표본 부족(10곳 미만)"})
            continue
        # 스피어만 상관 = 순위끼리의 피어슨 상관 (scipy 없이 계산)
        rho = g["1인당간이부가가치_백만원"].rank().corr(g["1인당보완부가가치_백만원"].rank())
        share = (g["감가상각_실제"] / g["보완부가가치_백만원"]).median()
        val_rows.append({"업종": grp, "기업수": len(g), "스피어만": round(rho, 3),
                         "감가상각비_비중_중앙값": round(100 * share, 1),
                         "판단": "간이식 순위 유지" if rho >= 0.9 else ("주의" if rho >= 0.8 else "간이식 순위 불안정")})
    val = pd.DataFrame(val_rows)

    out_cols = ["DART기업코드", "종목코드", "기업명", "시장", "업종코드", "대분류", "중분류", "BOK_업종", "규모구간", "분석대상", "제외사유",
                "직원수", "인건비_백만원", "영업이익", "이자_백만원", "감가상각_실제", "매출",
                "간이부가가치_백만원", "1인당간이부가가치_백만원", "HC_ROI", "노동소득분배율_근사",
                "BOK_노동소득분배율_전체", "노동소득분배율_차이_pp", "BOK_인건비비중_전체",
                "1인당보완부가가치_백만원", "1인당매출총이익_백만원", "자본생산성_간이",
                "인건비_매출총이익_비율", "적용규칙", "주지표", "주지표_산식", "주지표값", "주지표값_백분위",
                "보조지표", "보조지표_산식", "보조지표값", "보조지표값_백분위", "주의사항",
                "비교집단", "비교집단_기업수", "1인당간이부가가치_백만원_백분위", "HC_ROI_백분위"]
    df[[c for c in out_cols if c in df.columns]].to_csv("firm_productivity.csv", index=False, encoding="utf-8-sig")
    val.to_csv("validation_summary.csv", index=False, encoding="utf-8-sig")

    # 화면 요약
    print(f"[①] 전체 {len(df)}곳 → 분석 대상 {df['분석대상'].sum()}곳")
    ex = df.loc[~df["분석대상"], "제외사유"].str.split("; ").explode().value_counts()
    for k, c in ex.items():
        print(f"     제외: {k} {c}곳 (중복 가능)")
    t = df[df["분석대상"]]
    print("[②③] 업종별 중앙값 (분석 대상, 기업 10곳 이상 업종)")
    summ = t.groupby("대분류").agg(기업수=("DART기업코드", "count"),
                                 인당간이부가가치=("1인당간이부가가치_백만원", "median"),
                                 HC_ROI=("HC_ROI", "median"),
                                 노동소득분배율_근사=("노동소득분배율_근사", "median"),
                                 BOK_노동소득분배율=("BOK_노동소득분배율_전체", "median"))
    summ = summ[summ["기업수"] >= 10].round(2)
    summ.index = [f"{SECTION_NAME.get(i, i)}({i})" for i in summ.index]
    print(summ.to_string())
    print(f"     매출총이익 계산 가능 {t['1인당매출총이익_백만원'].notna().sum()}곳, 자본생산성 {t['자본생산성_간이'].notna().sum()}곳")
    if "적용규칙" in df:
        print("[규칙] 적용 규칙별 기업 수 (분석 대상 / 전체)")
        g = df.groupby("적용규칙").agg(전체=("DART기업코드", "count"), 분석대상=("분석대상", "sum"),
                                     주지표=("주지표", lambda x: x.mode().iat[0] if len(x.mode()) else ""))
        print(g.sort_values("전체", ascending=False).to_string())
    print("[④] 간이식 검증 (감가상각비 실제 값 있는 기업)")
    print(val.to_string(index=False))
    print("→ firm_productivity.csv, validation_summary.csv 저장")


if __name__ == "__main__":
    main()