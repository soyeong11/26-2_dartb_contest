"""
KOSIS 「기업체노동비용조사」 노동비용 엑셀 → 긴 형식 CSV

사용법 (simulation 폴더에서)
  python kosis_to_csv.py --xlsx data/노동비용_2025.xlsx data/노동비용_2019_2024.xlsx --out data/kosis_bonus.csv

입력 엑셀 구조 (KOSIS 다운로드 그대로)
  0행: 연도, 1행: 항목명(천원), 2행부터: [기업규모별, 산업분류, 값 ...]
  기업규모는 첫 행에만 적혀 있고 아래 행은 비어 있음 → 앞 값으로 채운다.

출력 열
  연도, 규모구분, 대분류, 산업명, 항목, 월평균_천원
  - 대분류: 'C' 같은 알파벳, 전체는 'ALL'
  - 항목: 노동비용총액, 직접노동비용, 정액및초과급여, 상여금및성과금, ...
"""
import argparse
import re

import pandas as pd

SIZE_MAP = {
    "전규모(상용근로자 10인 이상)": "전체",
    "중소규모(상용근로자 10~299인)": "10~299인",
    "대규모(상용근로자 300인 이상)": "300인 이상",
    "1규모(상용근로자 10~29인)": "10~29인",
    "2규모(상용근로자 30~99인)": "30~99인",
    "3규모(상용근로자 100~299인)": "100~299인",
    "4규모(상용근로자 300~499인)": "300~499인",
    "5규모(상용근로자 500~999인)": "500~999인",
    "6규모(상용근로자 1000인 이상)": "1000인 이상",
    "상용근로자 30인 이상": "30인 이상",
    "상용근로자 30~299인": "30~299인",
}


def clean_item(s):
    return re.sub(r"\(천원\)|\(계\)|\s", "", str(s))


def read_one(path):
    raw = pd.read_excel(path, sheet_name=0, header=None)
    years, items = raw.iloc[0, 2:].tolist(), raw.iloc[1, 2:].tolist()
    body = raw.iloc[2:].copy()
    body[0] = body[0].ffill()
    rows = []
    for _, r in body.iterrows():
        size = SIZE_MAP.get(str(r[0]).strip(), str(r[0]).strip())
        ind = str(r[1]).strip()
        sec = "ALL" if ind == "전체" else (ind.split(".")[0] if "." in ind else ind)
        for j, (y, it) in enumerate(zip(years, items)):
            v = pd.to_numeric(r[j + 2], errors="coerce")
            rows.append({"연도": int(y), "규모구분": size, "대분류": sec, "산업명": ind,
                         "항목": clean_item(it), "월평균_천원": v})
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", nargs="+", required=True)
    ap.add_argument("--out", default="data/kosis_bonus.csv")
    a = ap.parse_args()
    df = pd.concat([read_one(p) for p in a.xlsx], ignore_index=True).drop_duplicates()
    df.to_csv(a.out, index=False, encoding="utf-8-sig")
    b = df[(df["항목"] == "상여금및성과금")]
    print(f"{len(df):,}행 저장 → {a.out}  (연도 {sorted(df['연도'].unique())}, 규모 {b['규모구분'].nunique()}개, 산업 {b['대분류'].nunique()}개)")
    chk = b[(b["연도"] == b["연도"].max()) & (b["대분류"].isin(["ALL", "C", "J"])) & (b["규모구분"].isin(["전체", "10~299인", "300인 이상"]))]
    print("\n[검산] 최신 연도 상여금및성과금(만원/월)")
    print(chk.pivot_table(index="대분류", columns="규모구분", values="월평균_천원").div(10).round(2).to_string())


if __name__ == "__main__":
    main()
