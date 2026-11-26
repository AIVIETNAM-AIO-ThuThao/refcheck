"""Tra cứu sách trong nước trên trang Xác nhận đăng ký xuất bản của Cục Xuất bản, In và Phát hành (ppdvn.gov.vn).

Mỗi dòng kết quả gồm: ISBN, tên xuất bản phẩm, tác giả, người dịch, số xác nhận ĐKXB (vd. 4689-2026/CXBTV/7-86/ĐHQGTPHCM).
Năm trong số xác nhận (2026) chính là năm xuất bản, vì xác nhận chỉ có hiệu lực đến hết 31/12 năm đó.
"""
import re
import time
from functools import lru_cache

import requests
from bs4 import BeautifulSoup

from utils import BROWSER_HEADERS, norm_key, normalize_text, parse_author_dicts, word_in

PPDVN_URL = "https://ppdvn.gov.vn/web/guest/ke-hoach-xuat-ban"
PPDVN_DELAY = 1.5          # giây giữa 2 lần truy vấn: lịch sự với máy chủ nhà nước

# Danh sách NXB lấy từ ô chọn "Nhà xuất bản" trên trang (id_nxb)
PPDVN_NXB = {
    "Khoa học - Công nghệ - Truyền thông": 170, "Chính trị Quốc gia Sự thật": 1, "Quân đội nhân dân": 2,
    "Văn học": 5, "Văn hóa - thông tin": 6, "Văn hóa dân tộc": 7, "Kim Đồng": 8, "Âm nhạc": 9, "Mỹ thuật": 10,
    "Thanh niên": 11, "Phụ nữ Việt Nam": 12, "Công an nhân dân": 13, "Lao động": 14, "Sân khấu": 15,
    "Hội Nhà văn": 16, "Khoa học và Kỹ thuật": 17, "Nông nghiệp": 18, "Y học": 19, "Thể thao và Du lịch": 20,
    "Xây dựng": 21, "Thống kê": 22, "Giao thông vận tải": 23, "Tài chính": 24,
    "Tài nguyên - Môi trường và Bản đồ Việt Nam": 25, "Giáo dục Việt Nam": 26, "Đại học Quốc gia Hà Nội": 27,
    "Thế giới": 4, "Kinh tế - Tài chính": 190, "Hàng hải": 89, "Đại học Vinh": 68, "Học viện Nông nghiệp": 67,
    "Kinh tế thành phố Hồ Chí Minh": 66, "Đại học Công Nghiệp Thành phố Hồ Chí Minh": 63,
    "Đại học sư phạm Thành phố Hồ Chí Minh": 62, "Đại học Cần Thơ": 61, "Công Thương": 60, "Dân trí": 59,
    "Thông tin và Truyền thông": 41, "Lao động - Xã hội": 42, "Tôn giáo": 43,
    "Đại học Quốc gia Thành phố Hồ Chí Minh": 45, "Thông tấn": 46, "Đại học Sư phạm": 47, "Tư Pháp": 48,
    "Lý luận Chính trị": 49, "Tri thức": 51, "Đại học Kinh tế Quốc dân": 52, "Bách khoa Hà Nội": 53,
    "Đại học Huế": 54, "Khoa học Tự nhiên và Công nghệ": 55, "Hồng Đức": 56, "Đại học Thái Nguyên": 57,
    "Văn hóa- Văn nghệ Thành phố Hồ Chí Minh": 64, "Phương Đông": 38, "Trẻ": 37,
    "Tổng hợp Thành phố Hồ Chí Minh": 35, "Đồng Nai": 34, "Hà Nội": 28, "Thuận Hóa": 32, "Nghệ An": 31,
    "Thanh Hóa": 30, "Hải Phòng": 29, "Đà Nẵng": 33,
}
_NXB_BY_KEY = {norm_key(k): (k, v) for k, v in PPDVN_NXB.items()}
_NXB_ID_TO_NAME = {v: k for k, v in PPDVN_NXB.items()}

# Tiền tố/hậu tố hay gắn vào tên xuất bản phẩm khi đăng ký, không thuộc tên sách
TITLE_PREFIX_RE = re.compile(
    r'^\s*(?:sách\s+chuyên\s+khảo|sách\s+tham\s+khảo|giáo\s+trình(?:\s+giảng\s+dạy\s+đại\s+học)?|tài\s+liệu\s+tham\s+khảo)\s*[:\-–]?\s*',
    re.IGNORECASE)
TITLE_TAIL_RE = re.compile(r'\s*\((?:[^()]*(?:tái\s*bản|dùng\s*cho|dành\s*cho|in\s*lần|có\s*sửa|bổ\s*sung)[^()]*)\)\s*$', re.IGNORECASE)
AUTHOR_ROLE_RE = re.compile(r'\((?:đồng\s*)?chủ\s*biên\)|\(cb\)|\(đcb\)|\(editor\)|\(eds?\.?\)|chủ\s*biên\s*:', re.IGNORECASE)
DEGREE_RE = re.compile(r'\b(?:GS|PGS|TS|ThS|BS|CN|KS|NCS|TSKH)\b\.?', re.IGNORECASE)
ORG_WORDS = ("truong", "khoa", "bo mon", "dai hoc", "vien", "trung tam", "cong ty", "nhieu tac gia", "university")


def clean_title(raw: str) -> str:
    t = TITLE_PREFIX_RE.sub("", raw or "")
    while True:
        t2 = TITLE_TAIL_RE.sub("", t)
        if t2 == t:
            break
        t = t2
    return t.strip(" .:-–")


def find_nxb(cited_text: str):
    """Tìm NXB trong dòng trích dẫn -> (tên NXB, id_nxb) hoặc (None, None) nếu không nhận ra / không có trong danh sách."""
    m = re.search(r'(?:nhà\s*xuất\s*bản|\bnxb)\.?\s*([^.]+)', cited_text, re.IGNORECASE)
    if not m:
        return None, None
    phrase = norm_key(m.group(1))
    if phrase in _NXB_BY_KEY:                                   # trùng khớp hoàn toàn
        return _NXB_BY_KEY[phrase]
    hits = [(k, v) for k, v in _NXB_BY_KEY.items() if word_in(k, phrase)]
    if hits:                                                    # tên NXB nằm trọn trong cụm, lấy tên dài nhất
        return max(hits, key=lambda kv: len(kv[0]))[1]
    return None, None


def _person_names(author_text: str) -> list:
    t = DEGREE_RE.sub(" ", AUTHOR_ROLE_RE.sub(" ", author_text or ""))
    names = []
    for part in re.split(r'\s*(?:,|;|\s-\s|\bvà\b)\s*', t):
        part = re.sub(r'\s+', ' ', part).strip(" .")
        key = norm_key(part)
        if not key or any(w in key for w in ORG_WORDS) or not (2 <= len(key.split()) <= 5):
            continue
        names.append(part)
    return names


def cited_authors_match(cited_text: str, author_text: str) -> bool:
    """Mọi tác giả ghi trong trích dẫn (phần trước '(năm)') đều có mặt trong cột Tác giả của Cục Xuất bản."""
    hint = cited_text.split("(")[0]
    hint = re.sub(r'\b(?:và\s+cộng\s+sự|và\s+cs|et\s+al)\.?', ' ', hint, flags=re.IGNORECASE)
    registry_key = norm_key(author_text)
    names = [n for n in re.split(r'\s*(?:,|&|;|\bvà\b)\s*', hint) if norm_key(n)]
    if not names or not registry_key:
        return False
    for n in names:
        words = norm_key(n).split()
        if all(len(w) > 1 for w in words):
            if not word_in(n, registry_key):
                return False
        elif not word_in(max(words, key=len), registry_key):     # dạng có chữ viết tắt: chỉ đòi họ
            return False
    return True


def _fetch(params: dict):
    from web import _open_page_legacy                            # import trễ để tránh vòng lặp import
    time.sleep(PPDVN_DELAY)
    try:
        r = requests.get(PPDVN_URL, params=params, headers=BROWSER_HEADERS, timeout=20)
    except requests.exceptions.SSLError:
        r = _open_page_legacy(requests.Request("GET", PPDVN_URL, params=params).prepare().url)
    r.raise_for_status()
    return r.content


def parse_rows(html: bytes) -> list:
    soup = BeautifulSoup(html, "html.parser")
    box = soup.find(id="list_data_return")
    rows = []
    for tr in (box.select("tbody tr") if box else []):
        tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(tds) < 9:
            continue
        so_xn = tds[8]
        y = re.search(r'-(\d{4})/', so_xn)
        rows.append({"isbn": tds[1], "raw_title": tds[2], "author_text": tds[3], "translator": tds[4],
                     "so_xn": so_xn, "year": y.group(1) if y else "",
                     "nxb_code": so_xn.rsplit("/", 1)[-1] if "/" in so_xn else ""})
    return rows


@lru_cache(maxsize=512)
def _search(query: str, id_nxb: str) -> tuple:
    return tuple(tuple(sorted(r.items())) for r in parse_rows(_fetch({"query": query, "id_nxb": id_nxb,
                                                                      "bat_dau": "", "ket_thuc": ""})))


def search_books(cited_title: str, cited_text: str, threshold: float = 0.85):
    """Trả về (danh sách ứng viên theo khuôn của book.py, ok) — ok=False khi không truy cập được trang."""
    import difflib
    nxb_name, nxb_id = find_nxb(cited_text)
    queries = [cited_title]
    words = cited_title.split()
    if len(words) > 6:
        queries.append(" ".join(words[:6]))                     # tên đăng ký có thể dài hơn/khác đuôi
    attempts = [(q, str(nxb_id)) for q in queries] if nxb_id else []
    attempts += [(q, "") for q in queries]

    target = normalize_text(cited_title)
    try:
        for q, idx in attempts:
            rows = [dict(r) for r in _search(q, idx)]
            matched = []
            for r in rows:
                title = clean_title(r["raw_title"])
                sim = max(difflib.SequenceMatcher(None, target, normalize_text(title)).ratio(),
                          1.0 if len(target) >= 10 and target in normalize_text(r["raw_title"]) else 0.0)
                if sim >= threshold:
                    matched.append((r, title))
            if not matched:
                continue
            years = sorted({r["year"] for r, _ in matched if r["year"]})
            if idx:
                publisher = nxb_name                            # tìm thấy đúng ở NXB ghi trong trích dẫn
            elif nxb_id:
                publisher = None                                # có ở Cục XB nhưng KHÔNG thuộc NXB đã ghi
            else:
                publisher = ""                                  # trích dẫn không ghi/không nhận ra NXB
            cands = []
            for r, title in matched:
                pub = publisher if publisher is not None else f"NXB có mã '{r['nxb_code']}' (theo số XNĐK)"
                cands.append({
                    "status": "FOUND", "source": "ppdvn", "similarity": 1.0,
                    "title": title, "full_title": title, "raw_title": r["raw_title"],
                    "authors": parse_author_dicts(_person_names(r["author_text"]), source="book_api"),
                    "author_text": r["author_text"], "year": r["year"], "all_years": years,
                    "publisher": pub, "description": r["translator"], "isbn": r["isbn"],
                    "so_xn": r["so_xn"], "exact_edition_matched": True,
                })
            return cands, True
        return [], True
    except Exception as e:
        print(f"    [PPDVN] Lỗi truy cập Cục Xuất bản: {type(e).__name__}: {e}")
        return [], False
