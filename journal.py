import re
import difflib
from urllib.parse import quote
import html

from utils import (
    DOI_RE,
    EMAIL,
    HIGHLIGHT_UNVERIFIED_VN,
    TITLE_MATCH_THRESHOLD,
    add_review_comment,
    check_authors,
    clean_doi,
    extract_cited_pages,
    extract_cited_title,
    extract_cited_year,
    has_vietnamese_diacritics,
    highlight_whole_para,
    http_get,
    norm_key,
    normalize_text,
    pages_match,
    parse_author_dicts,
    title_extraction_failed,
    word_in,
)

CROSSREF_API = "https://api.crossref.org/works"
DATACITE_API = "https://api.datacite.org/dois/"
CROSSREF_HEADERS = {"User-Agent": f"RefChecker/1.0 (mailto:{EMAIL})"}

NOTICE_RE = re.compile(r'^\s*(correction|corrigendum|erratum|retraction|retracted|expression of concern)\b', re.IGNORECASE)
JOURNAL_STOP_WORDS = {"the", "and", "of", "for", "in", "on", "a", "an", "va", "cua"}


# ---------------------------------------------------------------- tiện ích tiêu đề
def title_similarity(cited_title: str, variants: list, full_text: str = "") -> float:
    """Độ giống cao nhất giữa tiêu đề trích dẫn và các biến thể tiêu đề (có/không phụ đề).
    Nếu tách tiêu đề thất bại nhưng tiêu đề chính thức nằm nguyên trong dòng trích dẫn thì coi là khớp."""
    best = 0.0
    cited_norm = normalize_text(cited_title)
    full_norm = normalize_text(full_text)
    for v in variants:
        v_norm = normalize_text(v)
        if not v_norm:
            continue
        best = max(best, difflib.SequenceMatcher(None, cited_norm, v_norm).ratio())
        if full_norm and len(v_norm) >= 20 and v_norm in full_norm:
            best = max(best, 1.0)
    return best


def _crossref_record(msg: dict) -> dict:
    title = (msg.get("title") or [""])[0]
    subtitle = (msg.get("subtitle") or [""])[0] if msg.get("subtitle") else ""
    variants = [title] + ([f"{title}: {subtitle}"] if subtitle else [])
    issued = (msg.get("issued") or {}).get("date-parts") or [[None]]
    year = str(issued[0][0]) if issued and issued[0] and issued[0][0] else ""
    return {
        "status": "OK",
        "title": variants[-1],                    # hiển thị tiêu đề đầy đủ (có phụ đề)
        "title_variants": variants,
        "container_titles": [html.unescape(t) for t in (msg.get("container-title") or []) + (msg.get("short-container-title") or []) if t],
        "authors": parse_author_dicts(msg.get("author", []), source="crossref"),
        "page": msg.get("page", "") or msg.get("article-number", ""),
        "doi": msg.get("DOI", ""),
        "year": year,
        "type": msg.get("type", ""),
    }


# ---------------------------------------------------------------- tra cứu
def lookup_doi_datacite(doi: str) -> dict:
    url = f"{DATACITE_API}{quote(doi, safe='/')}"
    try:
        resp = http_get(url, headers={"User-Agent": "RefChecker/1.0"})
    except Exception as e:
        return {"status": "ERROR", "message": f"Lỗi kết nối DataCite: {e}"}
    if resp.status_code == 404:
        return {"status": "DOI_NOT_FOUND", "message": f"DOI '{doi}' không tồn tại trên DataCite."}
    if resp.status_code != 200:
        return {"status": "ERROR", "message": f"DataCite HTTP {resp.status_code}"}

    data = resp.json().get("data", {}).get("attributes", {})
    titles = [t.get("title", "") for t in data.get("titles", []) if t.get("title")]
    return {
        "status": "OK",
        "title": titles[0] if titles else "",
        "title_variants": titles,
        "container_titles": [],
        "authors": parse_author_dicts(data.get("creators", []), source="datacite"),
        "page": "",
        "doi": doi,
        "year": str(data.get("publicationYear") or ""),
        "type": "datacite",
    }


def lookup_doi(doi: str) -> dict:
    doi = clean_doi(doi)
    url = f"{CROSSREF_API}/{quote(doi, safe='/')}"
    try:
        resp = http_get(url, headers=CROSSREF_HEADERS)
    except Exception as e:
        return {"status": "ERROR", "message": f"Lỗi kết nối Crossref: {e}"}

    if resp.status_code == 404:
        dc = lookup_doi_datacite(doi)
        if dc["status"] in ("OK", "ERROR"):
            return dc                                   # lỗi mạng ở DataCite KHÔNG được coi là "DOI không tồn tại"
        return {"status": "DOI_NOT_FOUND",
                "message": f"DOI '{doi}' không tồn tại (đã thử Crossref và DataCite)."}
    if resp.status_code != 200:
        return {"status": "ERROR", "message": f"Crossref HTTP {resp.status_code}"}

    return _crossref_record(resp.json().get("message", {}))


def search_by_title(title: str, cited_year: str = "", full_text: str = "") -> dict:
    query = full_text or title
    if title_extraction_failed(title, full_text) and full_text:
        query = full_text                        # Crossref query.bibliographic xử lý được cả dòng trích dẫn
    if not query or len(query.strip()) < 10:
        return {"status": "NO_TITLE", "message": "Thiếu tiêu đề bài báo."}

    params = {"query.bibliographic": query, "rows": 10, "mailto": EMAIL}
    try:
        resp = http_get(CROSSREF_API, params=params, headers=CROSSREF_HEADERS)
        if resp.status_code != 200:
            return {"status": "ERROR", "message": f"Lỗi tìm kiếm Crossref (HTTP {resp.status_code})."}
        items = resp.json().get("message", {}).get("items", [])
    except Exception as e:
        return {"status": "ERROR", "message": f"Lỗi tìm kiếm Crossref: {e}"}

    if not items:
        return {"status": "NOT_FOUND", "message": "Không tìm thấy bài viết tương ứng trên Crossref."}

    cited_is_notice = bool(NOTICE_RE.search(title or ""))
    cited_key = norm_key(full_text or title)
    best, best_key = None, None
    for item in items:
        rec = _crossref_record(item)
        if not rec["title_variants"][0]:
            continue
        if NOTICE_RE.search(rec["title_variants"][0]) and not cited_is_notice:
            continue                              # bỏ các bản đính chính/rút bài trùng tiêu đề
        sim = title_similarity(title, rec["title_variants"], full_text)
        year_ok = bool(cited_year and rec["year"] and abs(int(rec["year"]) - int(cited_year)) <= 1)
        author_ok = bool(rec["authors"]) and word_in(rec["authors"][0]["family"], cited_key)
        # Cùng một bài thì phải trùng ít nhất tác giả đầu HOẶC năm; tiêu đề giống thôi chưa đủ
        same_work = sim >= TITLE_MATCH_THRESHOLD and (author_ok or year_ok)
        key = (same_work, author_ok + year_ok, sim)
        if best_key is None or key > best_key:
            best, best_key = rec, key
            best["similarity"] = sim

    if best is None or not best_key[0]:
        return {"status": "LOW_MATCH", "message": "Không tìm thấy bài viết khớp trên Crossref."}

    best["status"] = "FOUND"
    return best


# ---------------------------------------------------------------- đối chiếu từng trường
def check_journal_in_text(cited_text: str, registry_journals, lenient: bool = False) -> dict:
    if isinstance(registry_journals, str):
        registry_journals = [registry_journals]
    registry_journals = [j for j in registry_journals or [] if j]
    if not registry_journals:
        return {"status": "NO_REGISTRY_JOURNAL", "message": ""}

    cited_key = norm_key(html.unescape(cited_text).replace("&", " and "))
    for jt in registry_journals:
        name = html.unescape(jt)
        if lenient:                               # kỷ yếu: bỏ phần trong ngoặc như "(Cat. No.99CH36288C)"
            name = re.sub(r'\([^)]*\)', ' ', name)
        words = [w for w in norm_key(name.replace("&", " and ")).split() if w not in JOURNAL_STOP_WORDS]
        hits = sum(word_in(w, cited_key) for w in words)
        if words and (hits == len(words) or (lenient and hits * 3 >= len(words) * 2)):
            return {"status": "MATCH", "message": ""}


def check_pages(cited_pages: str, registry_pages: str) -> dict:
    if not registry_pages:
        return {"status": "NO_REGISTRY_PAGES", "message": ""}
    if not cited_pages:
        return {"status": "MISSING_PAGES",
                "message": f"Thiếu số trang. Số trang đúng: {registry_pages}."}
    if pages_match(cited_pages, registry_pages):
        return {"status": "MATCH", "message": ""}
    return {"status": "PAGES_MISMATCH",
            "message": f"Số trang không khớp. Số trang đúng: {registry_pages}."}


def _check_record_fields(doc, para, text: str, rec: dict, cited_title: str, check_title: bool) -> int:
    """Đối chiếu tiêu đề / tác giả / tạp chí / số trang với bản ghi; trả về số comment đã gắn."""
    comments = 0
    if check_title and not title_extraction_failed(cited_title, text) and rec.get("title_variants"):
        if title_similarity(cited_title, rec["title_variants"], text) < TITLE_MATCH_THRESHOLD:
            add_review_comment(doc, para, f"Tiêu đề không khớp với DOI. Tiêu đề chính thức: \"{rec['title']}\".")
            comments += 1

    author_result = check_authors(text, rec.get("authors", []))
    if author_result["status"] not in ("MATCH", "NO_REGISTRY_AUTHORS"):
        add_review_comment(doc, para, author_result["message"])
        comments += 1

    lenient = rec.get("type") in ("proceedings-article", "book-chapter")   # tên kỷ yếu/sách mỗi nơi ghi một kiểu
    journal_result = check_journal_in_text(text, rec.get("container_titles", []), lenient=lenient)
    if journal_result["status"] not in ("MATCH", "NO_REGISTRY_JOURNAL"):
        add_review_comment(doc, para, journal_result["message"])
        comments += 1

    page_result = check_pages(extract_cited_pages(text), rec.get("page", ""))
    if page_result["status"] not in ("MATCH", "NO_REGISTRY_PAGES"):
        add_review_comment(doc, para, page_result["message"])
        comments += 1
    return comments


def _not_found(doc, para, text: str, message: str) -> tuple[bool, int, int]:
    """Không tìm thấy: tài liệu tiếng Việt chỉ ghi chú (không tô vàng) trừ khi bật HIGHLIGHT_UNVERIFIED_VN."""
    if has_vietnamese_diacritics(text) and not HIGHLIGHT_UNVERIFIED_VN:
        add_review_comment(doc, para, message + " Tài liệu trong nước không có trên Crossref — cần kiểm tra thủ công.")
        return False, 1, 0
    add_review_comment(doc, para, message)
    highlight_whole_para(para)
    return False, 1, 1


def _unchecked(doc, para, message: str) -> tuple[bool, int, int]:
    add_review_comment(doc, para, f"Chưa kiểm tra tự động được ({message}). Đề nghị kiểm tra thủ công.")
    return False, 1, 0


# ---------------------------------------------------------------- hàm entry
def process_journal_paragraph(doc, para, text: str) -> tuple[bool, int, int]:
    """Hàm entry của luồng Tạp chí. Trả về: (found, comments_added, highlights_added)."""
    cited_title = extract_cited_title(text)
    cited_year = extract_cited_year(text)
    doi_match = DOI_RE.search(text)

    # Bước 1: Ưu tiên xác minh bằng DOI
    if doi_match:
        cited_doi = clean_doi(doi_match.group(1))
        doi_check = lookup_doi(cited_doi)

        if doi_check["status"] == "OK":
            print(f"    [DOI] {cited_doi} → OK")
            comments = _check_record_fields(doc, para, text, doi_check, cited_title, check_title=True)
            return True, comments, 0

        if doi_check["status"] == "DOI_NOT_FOUND":
            print(f"    [DOI] {cited_doi} → NOT_FOUND, thử tìm bằng tiêu đề")
            sr = search_by_title(cited_title, cited_year, text)
            if sr["status"] == "FOUND":
                add_review_comment(doc, para, f"DOI '{cited_doi}' không tồn tại. DOI đúng: https://doi.org/{sr['doi']}.")
                comments = 1 + _check_record_fields(doc, para, text, sr, cited_title, check_title=False)
                return True, comments, 0
            if sr["status"] == "ERROR":
                return _unchecked(doc, para, f"DOI '{cited_doi}' không tồn tại; tìm theo tiêu đề bị lỗi: {sr['message']}")
            return _not_found(doc, para, text, f"DOI '{cited_doi}' không tồn tại và không tìm thấy bài viết tương ứng.")

        print(f"    [DOI] {cited_doi} → ERROR: {doi_check.get('message')}")
        return _unchecked(doc, para, doi_check.get("message", "lỗi kết nối"))

    # Bước 2: Không có DOI -> tìm bằng tiêu đề
    sr = search_by_title(cited_title, cited_year, text)
    if sr["status"] == "ERROR":
        return _unchecked(doc, para, sr["message"])
    if sr["status"] != "FOUND":
        return _not_found(doc, para, text, sr["message"])

    print(f"    [TITLE] {cited_title[:50]}... → FOUND (sim={sr['similarity']:.2f})")
    comments = _check_record_fields(doc, para, text, sr, cited_title, check_title=False)
    if sr.get("doi"):
        add_review_comment(doc, para, f"Thiếu DOI. DOI đúng: https://doi.org/{sr['doi']}.")
        comments += 1
    return True, comments, 0
