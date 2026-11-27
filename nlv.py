"""Tra cứu mục lục Thư viện Quốc gia Việt Nam (opac.nlv.gov.vn) — nguồn lưu chiểu sách trong nước, cả sách cũ và luận án.

Trang tìm kiếm nhận truy vấn qua tham số 'data' (JSON) trên đường dẫn /tim-kiem, trả về danh sách tài liệu dạng HTML:
tên tài liệu, tác giả, "Thông tin xuất bản: H.: Công Thương, 2026", loại tài liệu.
"""
import json
import re
import time
from functools import lru_cache

import requests
from bs4 import BeautifulSoup

from utils import BROWSER_HEADERS, parse_author_dicts

NLV_SEARCH = "https://opac.nlv.gov.vn/tim-kiem"
NLV_DELAY = 1.5            # giây giữa 2 lần truy vấn: lịch sự với máy chủ nhà nước


def _build_params(keyword: str, field: str = "ti", page_size: int = 50) -> dict:
    data = {"type": "quick", "page": 1, "pageSize": page_size,
            "request": {"searchBy": [["option", field], ["keyword", keyword]],
                        "sortBy": [["year_pub", "desc"]], "filterBy": []}}
    return {"data": json.dumps(data, ensure_ascii=False, separators=(",", ":"))}


def _parse_pubinfo(s: str):
    """'H.: Công Thương, 2026' -> ('Công Thương', '2026'); 'H., 2025' -> ('', '2025')."""
    s = (s or "").strip()
    m = re.search(r'(1[5-9]\d{2}|20\d{2})\D*$', s)
    year = m.group(1) if m else ""
    head = s[:m.start()].rstrip(" ,.;[]") if m else s
    publisher = head.split(":", 1)[1].strip(" ,.;") if ":" in head else ""
    return publisher, year


def parse_results(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for box in soup.select("div.result-block__main__bookContainer"):
        a = box.select_one("a.result-block__main__bookContainer__info_title")
        if not a:
            continue
        authors = [x.get_text(" ", strip=True) for x in box.select("p.authors a")]
        pubinfo = ""
        for p in box.select("div.result-block__main__bookContainer__info > p"):
            label = p.get_text(" ", strip=True)
            if label.startswith("Thông tin xuất bản"):
                span = p.find("span")
                pubinfo = span.get_text(" ", strip=True) if span else label.split(":", 1)[-1]
                break
        publisher, year = _parse_pubinfo(pubinfo)
        typ = box.select_one(".result-block__main__bookContainer__type")
        rows.append({"title": a.get_text(" ", strip=True), "authors": authors, "publisher": publisher,
                     "year": year, "pubinfo": pubinfo, "type": typ.get_text(strip=True) if typ else "",
                     "url": a.get("href", "")})
    return rows


@lru_cache(maxsize=512)
def _search(keyword: str, field: str) -> tuple:
    time.sleep(NLV_DELAY)
    r = requests.get(NLV_SEARCH, params=_build_params(keyword, field), headers=BROWSER_HEADERS, timeout=30)
    r.raise_for_status()
    return tuple(tuple(sorted((k, tuple(v) if isinstance(v, list) else v) for k, v in row.items()))
                 for row in parse_results(r.text))


def search_books(cited_title: str, cited_text: str, threshold: float = 0.85):
    """Trả về (danh sách ứng viên theo khuôn của book.py, ok) — ok=False khi không truy cập được trang."""
    import difflib
    import ppdvn                                   # dùng chung cách làm sạch tên sách và so tác giả
    from utils import normalize_text
    target = normalize_text(ppdvn.clean_title(cited_title))     # bỏ "Giáo trình", "(Tái bản…)" ở cả hai phía
    # Tìm theo TÁC GIẢ ĐẦU trước (ít kết quả, trúng đích); tên sách phổ biến như "Cấu trúc dữ liệu và giải thuật"
    # có hàng chục giáo trình trùng tên. Không thấy thì tìm theo tên sách.
    first_author = re.split(r"\s*(?:,|&|;|\bvà\b)\s*", cited_text.split("(")[0])[0].strip(" .")
    queries = []
    if len(first_author.split()) >= 2:
        queries.append((first_author, "au"))
    queries.append((cited_title, "ti"))

    matched = []
    try:
        for kw, field in queries:
            rows = [{k: (list(v) if isinstance(v, tuple) else v) for k, v in r} for r in _search(kw, field)]
            for r in rows:
                title = ppdvn.clean_title(r["title"])
                sim = difflib.SequenceMatcher(None, target, normalize_text(title)).ratio()
                if sim >= threshold:
                    matched.append((r, title))
            if matched:
                break
    except Exception as e:
        print(f"    [NLV] Lỗi truy cập Thư viện Quốc gia: {type(e).__name__}: {e}")
        return [], False
    # chỉ giữ các bản có tác giả khớp trích dẫn (cùng tên sách có rất nhiều giáo trình của các trường khác nhau)
    same_author = [(r, t) for r, t in matched if ppdvn.cited_authors_match(cited_text, "; ".join(r["authors"]))]
    pool = same_author or matched
    years = sorted({r["year"] for r, _ in pool if r["year"]})
    cands = []
    for r, title in pool:
        cands.append({
            "status": "FOUND", "source": "nlv", "similarity": 1.0,
            "title": title, "full_title": title, "raw_title": r["title"],
            "authors": parse_author_dicts(r["authors"], source="book_api"),
            "author_text": "; ".join(r["authors"]), "year": r["year"], "all_years": years,
            "publisher": r["publisher"], "description": "", "url": r["url"],
            "exact_edition_matched": True,     # mỗi lần in/tái bản là một bản ghi riêng -> năm so chặt từng bản
        })
    return cands, True
