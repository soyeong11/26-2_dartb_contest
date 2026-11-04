"""
한국은행 「기업경영분석」 책자형 통계편 PDF → 업종 × 규모 × 지표 CSV

왜 필요한가
  추천 시스템의 '동종 비교 기준값'(부가가치 구성, 노동소득분배율, 투자효율 등)을
  업종·규모별로 한 번에 쓰기 위해, PDF에 흩어진 통계표를 긴 형식(long) CSV로 바꾼다.

사용법
  pip install pdfplumber            (또는 poppler의 pdftotext가 있으면 자동 사용)
  python bok_pdf_to_csv.py --pdf "2025년_기업경영분석_책자형_통계편.pdf" --out bok_2025_long.csv

출력 열
  pdf_page, 업종코드, 업종명, 규모, 지표코드, 지표명, 연도, 값
  - 규모: 전체 / 대기업 / 중견기업 / 중소기업 / 중기업 / 소기업
  - 지표코드 예: 904 부가가치율, 905 노동소득분배율, 1001~1005 부가가치 구성
  - 단위는 대부분 %이며, 재무상태표·제조원가명세서의 '금액(백만원)'은 별도 행(연도='금액')으로 저장

주의
  - 책자에는 산식 설명이 없다. 부가가치율·투자효율의 분모는 핸드북으로 별도 확인할 것.
  - 표 레이아웃이 다른 쪽(목차·부록)은 자동으로 건너뛴다.
"""

import argparse
import csv
import re
import shutil
import subprocess
import sys

SIZE_RULES = [  # 영문 라벨 우선, 없으면 한글 라벨
    ("( Large Enterprises )", "대기업"),
    ("( Middle Market Enterprises )", "중견기업"),
    ("( Medium Enterprises )", "중기업"),
    ("( Small Enterprises )", "소기업"),
    ("( All Enterprises )", "전체"),
]
KOR_SIZE = [("중 소 기 업", "중소기업"), ("중 견 기 업", "중견기업"), ("대 기 업", "대기업"),
            ("중 기 업", "중기업"), ("소 기 업", "소기업")]

CODE_RE = re.compile(r"\b((?:ALL|[A-Z]\d{2,5})(?:[-,]\d{1,5})*)\b")
NUM = r"(-?[\d,]*\.?\d+|\*|-)"
# 비율표: '# 904 부 가 가 치 율 28.65 30.33 31.18' 또는 '44 629 이 자 보 상 비 율 191.13 244.09 292.67'
RATIO_RE = re.compile(r"(?:#|\b\d{1,3})\s+(\d{3,4})\s+([가-힣A-Za-z ∙·()]+?)\s+" + NUM + r"\s+" + NUM + r"\s+" + NUM + r"(?=\s|$)")
# 금액표: '110 31304 감 가 상 각 비 145,522,223 4.83'
AMOUNT_RE = re.compile(r"\b\d{1,3}\s+(\d{2,5})\s+([가-힣 ∙·()]+?)\s+(-?[\d,]{5,})\s+(-?\d+\.\d+)(?=\s|$)")
YEAR_RE = re.compile(r"\b(20\d{2})\s+(20\d{2})\s+(20\d{2})\b")
# 손익계산서: '88 252 영 업 외 비 용 5.18' (pdftotext) / '60 24101 급 여 46,806,381 6.25' (pdfplumber)
IS_RE = re.compile(r"\b\d{1,3}\s+(2\d{0,4})\s+([가-힣 ∙·()]+?)\s+(?:(-?[\d,]{5,})\s+)?(-?\d+\.\d+)(?=\s|$)")


def is_trend(code):
    """3개년 값이 있는 표(이익잉여금처분 4xx, 각종 비율 5xx~10xx)의 코드인가"""
    n = int(code)
    return (len(code) == 3 and 400 <= n < 1000) or (len(code) == 4 and (1000 <= n < 1100 or 4000 <= n < 5000))


def resolve(path):
    """파일이 없으면 띄어쓰기·밑줄·한글 표기(NFC/NFD) 차이를 무시하고 같은 폴더에서 찾는다."""
    import os
    import unicodedata
    if os.path.exists(path):
        return path
    key = lambda s: re.sub(r"[\s_]", "", unicodedata.normalize("NFC", s)).lower()  # noqa: E731
    folder = os.path.dirname(path) or "."
    for f in os.listdir(folder):
        if key(f) == key(os.path.basename(path)):
            print(f"[알림] '{path}' 대신 '{f}' 파일을 사용합니다.")
            return os.path.join(folder, f)
    pdfs = [unicodedata.normalize("NFC", f) for f in os.listdir(folder) if f.lower().endswith(".pdf")]
    sys.exit(f"PDF를 찾을 수 없습니다: {path}\n이 폴더의 PDF: {pdfs}")


def pdf_pages(path, engine="auto"):
    """페이지별 텍스트(raw 순서)를 돌려준다."""
    if engine in ("auto", "pdftotext") and shutil.which("pdftotext"):
        out = subprocess.run(["pdftotext", "-raw", path, "-"], capture_output=True, check=True)
        return out.stdout.decode("utf-8", errors="replace").split("\f")
    try:
        import pdfplumber
    except ImportError:
        sys.exit("pdftotext 또는 pdfplumber가 필요합니다: pip install pdfplumber")
    pages = []
    with pdfplumber.open(path) as pdf:
        n = len(pdf.pages)
        for i, p in enumerate(pdf.pages, 1):
            pages.append(p.extract_text() or "")
            if i % 25 == 0 or i == n:
                print(f"  PDF 읽는 중 {i}/{n}쪽 (pdfplumber는 쪽당 1~2초)")
    return pages


def detect_size(text):
    for eng, label in SIZE_RULES:
        if eng in text:
            return label
    for kor, label in KOR_SIZE:
        if kor in text:
            return label
    return None


def detect_industry(text):
    head = text[:1500] + text[-1500:]
    for c in CODE_RE.findall(head):
        if c not in {"ALL"} and not c.isdigit():
            return c
    return "ALL" if "All-industry" in text or "전 산 업" in text else None


def clean_name(s):
    return re.sub(r"\s+", "", s)


def to_num(s):
    if s in ("*", "-"):
        return ""
    return s.replace(",", "")


def parse(pages, name_map):
    rows = []
    for i, text in enumerate(pages, 1):
        if "재무상태표" not in text and "Balance Sheet" not in text:
            continue
        size = detect_size(text)
        ind = detect_industry(text)
        if not size or not ind:
            continue
        ym = YEAR_RE.search(text)
        years = ym.groups() if ym else ("t-2", "t-1", "t")
        base = dict(pdf_page=i, 업종코드=ind, 업종명=name_map.get(ind, ""), 규모=size)
        seen = set()
        for m in IS_RE.finditer(text):
            code, name = m.group(1), clean_name(m.group(2))
            if (code, name) in seen:
                continue
            seen.add((code, name))
            if m.group(3):
                rows.append({**base, "지표코드": code, "지표명": name, "연도": "금액(백만원)", "값": to_num(m.group(3))})
            rows.append({**base, "지표코드": code, "지표명": name, "연도": "구성비(%)", "값": to_num(m.group(4))})
        for m in RATIO_RE.finditer(text):
            code, name = m.group(1), clean_name(m.group(2))
            if not is_trend(code) or (code, name) in seen:
                continue
            seen.add((code, name))
            for y, v in zip(years, m.groups()[2:5]):
                rows.append({**base, "지표코드": code, "지표명": name, "연도": y, "값": to_num(v)})
        for m in AMOUNT_RE.finditer(text):
            code, name = m.group(1), clean_name(m.group(2))
            if (code, name) in seen:
                continue
            seen.add((code, name))
            rows.append({**base, "지표코드": code, "지표명": name, "연도": "금액(백만원)", "값": to_num(m.group(3))})
            rows.append({**base, "지표코드": code, "지표명": name, "연도": "구성비(%)", "값": to_num(m.group(4))})
    return rows


def industry_names(path):
    """목차(앞 3쪽, 레이아웃 보존 텍스트)에서 '코드 업종명 쪽번호'를 모은다. 실패해도 코드만으로 동작."""
    if not shutil.which("pdftotext"):
        return {}
    out = subprocess.run(["pdftotext", "-layout", "-f", "1", "-l", "3", path, "-"], capture_output=True)
    text = out.stdout.decode("utf-8", errors="replace")
    names = {}
    pat = re.compile(r"((?:ALL|[A-Z]\d{2,5})(?:[-,]\d{1,5})*(?:,\s?[A-Z]?\d{1,5})*)\s{2,}([가-힣][가-힣 ,·∙()\-]+?)\s{2,}(\d{1,3})\b")
    for line in text.splitlines():
        for m in pat.finditer(line):
            names.setdefault(m.group(1).replace(" ", ""), re.sub(r"\s+", " ", m.group(2)).strip())
    return names


def industry_names_plumber(pages):
    """pdftotext가 없을 때: 목차 쪽 텍스트에서 '코드 업종명 쪽번호'를 최대한 모은다."""
    names = {}
    pat = re.compile(r"((?:ALL|[A-Z]\d{2,5})(?:[-,]\d{1,5})*)\s+([가-힣][가-힣 ,·∙()\-]*?[가-힣)])\s+(\d{1,3})\b")
    for text in pages[:3]:
        for m in pat.finditer(text):
            names.setdefault(m.group(1), re.sub(r"\s+", " ", m.group(2)).strip())
    return names


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--out", default="bok_long.csv")
    ap.add_argument("--engine", default="auto", choices=["auto", "pdftotext", "pdfplumber"])
    a = ap.parse_args()
    a.pdf = resolve(a.pdf)
    pages = pdf_pages(a.pdf, a.engine)
    names = industry_names(a.pdf) if shutil.which("pdftotext") else industry_names_plumber(pages)
    rows = parse(pages, names)
    if not rows:
        sys.exit("추출된 행이 없습니다. PDF 형식을 확인하세요.")
    with open(a.out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    combos = {(r["업종코드"], r["규모"]) for r in rows}
    print(f"{len(rows):,}행, 업종×규모 {len(combos)}개 → {a.out}")


if __name__ == "__main__":
    main()