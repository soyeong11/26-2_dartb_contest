"""
성과급 재원 시뮬레이션 v2

질문: "우리 회사는 성과급을 얼마나 줄 수 있나(감당 가능) / 줘야 하나(시장 수준)?"
      설계안 5가지를 상장사 2,231곳에 적용하고, 생산성(부가가치)이 흔들릴 때 설계안별 반응을 비교한다.

사용법 (simulation 폴더에서)
  python kosis_to_csv.py
  python bonus_sim.py                              # 기본: 업계평균 = 최근 3년 평균, 규모 구간 자동 선택
  python bonus_sim.py --company 삼진               # 회사 1곳의 5가지 설계안 비교표 추가 출력
  python bonus_sim.py --bench-years 1 --band fine  # v1과 같은 방식(2025년 한 해, 세부 규모)

설계안 (Pool = 회사 전체 성과급 재원, 백만원 / OP 영업이익, W 인건비, VA = OP + W, N 직원수)
  D1 이익배분 5%   : 0.05 × max(OP, 0)
  D2 이익배분 10%  : 0.10 × max(OP, 0)                 ← SK하이닉스 PS형
  D3 10% + 상한    : min(0.10 × max(OP,0), 0.5 × W)     ← 삼성 OPI형(연봉 50% 상한) 근사
  D4 Rucker형      : 0.5 × max(0, R* × VA − W)           R* = 비교집단 노동분배율(W/VA) 중앙값
  D5 업계평균형    : N × KOSIS 1인당 연간 상여금·성과금  ← 실적과 무관한 시장 수준

v2에서 바뀐 점
  ① 업계평균 = 최근 N년(기본 3년: 2023~2025) 평균 → 한 해 급변(예: 정보통신 소규모 2025 급감)에 덜 흔들림
  ② 규모 구간 자동 선택(--band auto): 세부 규모 값이 같은 업종 큰 규모 값의 1/3~3배를 벗어나면
     표본이 작아 튄 값으로 보고 큰 규모(10~299인 / 300인 이상) 값을 쓴다
  ③ 흑자 기업만의 1인당 중앙값, 기업 10곳 미만 칸 표시
  ④ 직원 위험 지표: 부가가치 −20%일 때 1인당 성과급이 기준 대비 몇 %로 줄어드는가(기준 흑자 기업 중앙값)
     회사 위험 지표: '재원초과'(성과급 > 그해 영업이익)는 업계평균형에만 의미가 있음
  ⑤ 발표용 표(bonus_presentation.csv)와 회사 1곳 비교표(--company)
  ⑥ 부동소수점 비교 오차, 0.0000…1 같은 계산 잔여값 정리

출력 (outputs 폴더)
  bonus_sim_firm.csv          기업 × 시나리오 × 설계안 전체 결과
  bonus_sim_summary.csv       업종 × 규모 × 설계안 요약 (기준 시나리오)
  bonus_sim_stress.csv        부가가치 충격별 설계안 요약
  bonus_presentation.csv      발표용: 기업 10곳 이상 업종×규모, 핵심 지표만
  kosis_trend.csv             업종별 상여금·성과금 추이
  company_<기업명>.csv        --company 사용 시

주의
  - W(직원 연간급여총액)에는 그해 이미 지급된 상여·성과급이 들어 있다 → '추가로 줄 돈'이 아니라
    '이 규칙이었다면 얼마였을까'의 비교값이다.
  - 충격은 VA에 비율을 곱하고 W는 고정한다(OP = VA(1+s) − W). 비용 구조를 예측하는 모형이 아니다.
  - KOSIS 2025년부터 산업분류 기준이 바뀌었다. 3년 평균은 2023·2024(구 기준)와 2025(신 기준)를 섞는다.
"""
import argparse
import os
import re

import numpy as np
import pandas as pd

SHOCKS = [-0.5, -0.2, 0.0, 0.2, 0.5]
BASE = "부가가치 +0%"
DOWN = "부가가치 -20%"
EPS = 1e-9
SEC_NAME = {"C": "제조", "J": "정보통신", "G": "도소매", "M": "전문서비스", "F": "건설", "H": "운수",
            "D": "전기가스", "N": "사업지원", "E": "수도환경", "I": "숙박음식", "B": "광업", "L": "부동산",
            "R": "예술여가", "S": "기타서비스", "A": "농림어업", "P": "교육"}
FINE = [(29, "10~29인"), (99, "30~99인"), (299, "100~299인"), (499, "300~499인"), (999, "500~999인")]


def fine_band(n):
    for hi, lab in FINE:
        if n <= hi:
            return lab
    return "1000인 이상"


def coarse_band(n):
    return "10~299인" if n < 300 else "300인 이상"


def load_benchmark(path, year, n_years):
    """최근 n_years 평균 상여금·성과금(월, 천원). {(대분류, 규모구분): 값}"""
    k = pd.read_csv(path)
    k = k[(k["항목"] == "상여금및성과금") & k["연도"].between(year - n_years + 1, year)]
    k = k[k["월평균_천원"] > 0]
    avg = k.groupby(["대분류", "규모구분"])["월평균_천원"].mean()
    return avg.to_dict(), sorted(k["연도"].unique())


def lookup(bm, sec, n, band):
    fb, cb = fine_band(n), coarse_band(n)
    fine_v, coarse_v = bm.get((sec, fb)), bm.get((sec, cb))
    if band == "coarse" and coarse_v is not None:
        return coarse_v, "업종×큰규모"
    if fine_v is not None:
        if band == "fine" or coarse_v is None or (coarse_v / 3 <= fine_v <= coarse_v * 3):
            return fine_v, "업종×세부규모"
        return coarse_v, "업종×큰규모(세부값 이상치)"
    if coarse_v is not None:
        return coarse_v, "업종×큰규모"
    v = bm.get(("ALL", fb)) or bm.get(("ALL", cb))
    return (v, "전산업") if v is not None else (np.nan, "없음")


def summ(g):
    profit = g[~g["적자기업"]]
    return pd.Series({
        "기업수": len(g),
        "1인당_중앙값_만원": g["1인당_백만원"].median() * 100,
        "흑자기업_1인당_중앙값_만원": profit["1인당_백만원"].median() * 100,
        "인건비대비_중앙값_%": g["인건비대비_%"].median(),
        "지급0_비율_%": g["지급0"].mean() * 100,
        "영업적자_비율_%": g["적자기업"].mean() * 100,
        "재원초과_비율_%": g["재원초과"].mean() * 100,
        "업계평균이상_비율_%": (g["벤치대비_배"] >= 1 - EPS).mean() * 100,
        "소표본(10곳미만)": len(g) < 10,
    })


def build(df, shocks):
    out = []
    for s in shocks:
        d = df.copy()
        d["시나리오"] = f"부가가치 {int(s * 100):+d}%"
        d["OP_s"] = d.VA * (1 + s) - d.W
        opp = d.OP_s.clip(lower=0)
        pools = {
            "D1_이익5%": 0.05 * opp,
            "D2_이익10%": 0.10 * opp,
            "D3_10%+상한": np.minimum(0.10 * opp, 0.5 * d.W),
            "D4_Rucker": 0.5 * (d.R_star * (d.OP_s + d.W) - d.W).clip(lower=0),
            "D5_업계평균": d.N * d["벤치_1인연간_백만원"],
        }
        for name, p in pools.items():
            p = pd.Series(np.where(p < 1e-6, 0.0, p), index=d.index)
            e = d[["기업명", "대분류", "업종", "규모", "비교집단", "N", "W", "OP", "OP_s", "R_star",
                   "벤치_1인연간_백만원", "벤치_출처", "시나리오"]].copy()
            e["설계안"] = name
            e["Pool_백만원"] = p
            e["1인당_백만원"] = p / d.N
            e["인건비대비_%"] = p / d.W * 100
            e["영업이익대비_%"] = np.where(d.OP_s > 0, p / d.OP_s * 100, np.nan)
            e["지급0"] = p <= 0
            e["적자기업"] = d.OP_s <= 0
            e["재원초과"] = p > opp + EPS
            e["벤치대비_배"] = e["1인당_백만원"] / d["벤치_1인연간_백만원"]
            out.append(e)
    return pd.concat(out, ignore_index=True)


def employee_risk(res, by):
    """기준 시나리오 흑자 기업에서, 부가가치 −20%일 때 1인당 성과급이 기준 대비 몇 %인가(중앙값)"""
    b = res[(res["시나리오"] == BASE) & (~res["적자기업"])].set_index(["기업명", "설계안"])
    d = res[res["시나리오"] == DOWN].set_index(["기업명", "설계안"])
    j = b[by + ["1인당_백만원"]].join(d["1인당_백만원"].rename("하락"), how="inner")
    j = j[j["1인당_백만원"] > 0]
    j["유지율_%"] = j["하락"] / j["1인당_백만원"] * 100
    return j.reset_index().groupby(by + ["설계안"])["유지율_%"].median()


def company_table(res, name, outdir):
    c = res[res["기업명"] == name]
    if c.empty:
        cand = res.loc[res["기업명"].str.contains(name, na=False), "기업명"].unique()[:10]
        print(f"\n[회사] '{name}'을 찾지 못했습니다. 비슷한 이름: {list(cand)}")
        return
    t = c.pivot_table(index="설계안", columns="시나리오", values="1인당_백만원").mul(100).round(0)
    order = [f"부가가치 {int(s * 100):+d}%" for s in SHOCKS]
    t = t[order]
    base = c[c["시나리오"] == BASE].set_index("설계안")
    t.insert(0, "총재원_억원(기준)", (base["Pool_백만원"] / 100).round(1))
    t["영업이익대비_%(기준)"] = base["영업이익대비_%"].round(1)
    info = c.iloc[0]
    print(f"\n[회사] {name} · {info['업종']} · 직원 {int(info['N']):,}명 · 영업이익 {info['OP'] / 100:,.1f}억원 · "
          f"업계평균 기준 1인 연 {info['벤치_1인연간_백만원'] * 100:,.0f}만원({info['벤치_출처']})")
    print("  1인당 성과급(만원), 열 = 부가가치 충격")
    print(t.to_string())
    safe = re.sub(r"[\\/:*?\"<>|]", "_", name)
    t.to_csv(os.path.join(outdir, f"company_{safe}.csv"), encoding="utf-8-sig")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--firm", default="data/firm_productivity.csv")
    ap.add_argument("--kosis", default="data/kosis_bonus.csv")
    ap.add_argument("--year", type=int, default=2025, help="기준 연도")
    ap.add_argument("--bench-years", type=int, default=3, help="업계평균을 낼 연도 수 (1이면 기준 연도만)")
    ap.add_argument("--band", default="auto", choices=["auto", "fine", "coarse"], help="KOSIS 규모 구간 선택 방식")
    ap.add_argument("--min-firms", type=int, default=10, help="발표용 표에 넣을 최소 기업 수")
    ap.add_argument("--company", default=None, help="비교표를 볼 회사 이름 (예: 삼진)")
    ap.add_argument("--outdir", default="outputs")
    a = ap.parse_args()
    os.makedirs(a.outdir, exist_ok=True)

    df = pd.read_csv(a.firm)
    df = df[df["분석대상"] == True].copy()  # noqa: E712
    df = df.rename(columns={"직원수": "N", "인건비_백만원": "W", "영업이익": "OP", "규모구간": "규모"})
    df = df.dropna(subset=["N", "W", "OP"])
    df = df[(df.N > 0) & (df.W > 0)].copy()
    df["VA"] = df.OP + df.W
    df["업종"] = df["대분류"].map(SEC_NAME).fillna("기타")

    bm, used_years = load_benchmark(a.kosis, a.year, a.bench_years)
    got = [lookup(bm, s, n, a.band) for s, n in zip(df["대분류"], df["N"])]
    df["벤치_월_천원"] = [g[0] for g in got]
    df["벤치_출처"] = [g[1] for g in got]
    df["벤치_1인연간_백만원"] = df["벤치_월_천원"] * 12 / 1000

    pos = df[df.VA > 0].assign(LS=lambda d: d.W / d.VA)
    g1 = pos.groupby("비교집단")["LS"].agg(["median", "size"])
    g2 = pos.groupby(["대분류", "규모"])["LS"].median()
    df["R_star"] = df["비교집단"].map(g1["median"])
    small = df["비교집단"].map(g1["size"]).fillna(0) < 10
    df.loc[small, "R_star"] = [g2.get((s, z), np.nan) for s, z in zip(df.loc[small, "대분류"], df.loc[small, "규모"])]

    res = build(df, SHOCKS)
    res.to_csv(os.path.join(a.outdir, "bonus_sim_firm.csv"), index=False, encoding="utf-8-sig")

    order = [f"부가가치 {int(s * 100):+d}%" for s in SHOCKS]
    stress = res.groupby(["시나리오", "설계안"]).apply(summ, include_groups=False).reset_index()
    risk_all = employee_risk(res, [])
    stress["직원위험_-20%시_유지율_%"] = stress["설계안"].map(risk_all)
    stress.to_csv(os.path.join(a.outdir, "bonus_sim_stress.csv"), index=False, encoding="utf-8-sig")

    base = res[res["시나리오"] == BASE]
    summary = base.groupby(["업종", "규모", "설계안"]).apply(summ, include_groups=False).reset_index()
    risk = employee_risk(res, ["업종", "규모"]).rename("직원위험_-20%시_유지율_%").reset_index()
    summary = summary.merge(risk, on=["업종", "규모", "설계안"], how="left")
    summary.to_csv(os.path.join(a.outdir, "bonus_sim_summary.csv"), index=False, encoding="utf-8-sig")

    # 발표용 표: 업종×규모 1행, 기업 min_firms 이상
    big = summary[summary["기업수"] >= a.min_firms]
    p = big.pivot_table(index=["업종", "규모"], columns="설계안",
                        values=["흑자기업_1인당_중앙값_만원", "직원위험_-20%시_유지율_%"])
    b5 = base[(base["설계안"] == "D5_업계평균") & (base.OP > 0)]
    need = b5.groupby(["업종", "규모"])["영업이익대비_%"].median()
    pres = pd.DataFrame({
        "기업수": big[big["설계안"] == "D2_이익10%"].set_index(["업종", "규모"])["기업수"],
        "영업적자_비율_%": big[big["설계안"] == "D2_이익10%"].set_index(["업종", "규모"])["영업적자_비율_%"],
        "업계평균_1인연간_만원": p[("흑자기업_1인당_중앙값_만원", "D5_업계평균")],
        "이익10%안_흑자기업_1인당_만원": p[("흑자기업_1인당_중앙값_만원", "D2_이익10%")],
        "업계평균을_주려면_필요한_영업이익_%": need,
        "업계평균안_재원초과_비율_%": big[big["설계안"] == "D5_업계평균"].set_index(["업종", "규모"])["재원초과_비율_%"],
        "이익10%안_부가가치-20%시_유지율_%": p[("직원위험_-20%시_유지율_%", "D2_이익10%")],
    }).dropna(subset=["기업수"]).round(1)
    pres.to_csv(os.path.join(a.outdir, "bonus_presentation.csv"), encoding="utf-8-sig")

    k = pd.read_csv(a.kosis)
    trend = k[(k["항목"] == "상여금및성과금") & (k["규모구분"].isin(["10~299인", "300인 이상"]))
              & (k["대분류"].isin(["ALL", "C", "J", "G", "F", "M"]))]
    trend = trend.pivot_table(index=["대분류", "규모구분"], columns="연도", values="월평균_천원").div(10).round(1)
    trend.to_csv(os.path.join(a.outdir, "kosis_trend.csv"), encoding="utf-8-sig")

    pd.set_option("display.width", 240)
    pd.set_option("display.max_columns", 30)
    print(f"[데이터] 대상 {len(df):,}곳 · 업계평균 = KOSIS {used_years[0]}~{used_years[-1]} 평균 · 규모 구간 {a.band}")
    print("  기준값 출처:", df["벤치_출처"].value_counts().to_dict())
    print("\n[1] 기준 시나리오: 설계안별 요약")
    cols = ["설계안", "1인당_중앙값_만원", "흑자기업_1인당_중앙값_만원", "영업적자_비율_%", "재원초과_비율_%",
            "업계평균이상_비율_%", "직원위험_-20%시_유지율_%"]
    print(stress[stress["시나리오"] == BASE][cols].round(1).to_string(index=False))
    print(f"\n[2] 발표용 표 (기업 {a.min_firms}곳 이상 업종×규모)")
    print(pres.to_string())
    print("\n[3] 부가가치 충격별 흑자기업 1인당 성과급 중앙값(만원)")
    print(stress.pivot_table(index="설계안", columns="시나리오", values="흑자기업_1인당_중앙값_만원")[order].round(0).to_string())
    print("    영업적자 기업 비율(%):",
          stress[stress["설계안"] == "D1_이익5%"].set_index("시나리오")["영업적자_비율_%"].reindex(order).round(1).to_dict())
    print("\n[4] 업계평균형: 성과급이 그해 영업이익을 넘는 기업 비율(%)")
    print(stress[stress["설계안"] == "D5_업계평균"].set_index("시나리오")["재원초과_비율_%"].reindex(order).round(1).to_dict())
    if a.company:
        company_table(res, a.company, a.outdir)
    print(f"\n결과 파일 → {a.outdir}/")


if __name__ == "__main__":
    main()