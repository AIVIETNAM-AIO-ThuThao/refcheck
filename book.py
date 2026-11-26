import re
import difflib
import html

from utils import (
    CURRENT_YEAR,
    EMAIL,
    GOOGLE_BOOKS_API_KEY,
    HIGHLIGHT_UNVERIFIED_VN,
    TITLE_MATCH_THRESHOLD,
    add_review_comment,
    check_authors,
    extract_cited_title,
    extract_cited_year,
    extract_isbn,
    extract_year_from_str,
    format_author_apa,
    has_vietnamese_diacritics,
    highlight_whole_para,
    http_get,
    norm_key,
    normalize_text,
    parse_author_dicts,
    word_in,
)

GOOGLE_BOOKS_API = "https://www.googleapis.com/books/v1/volumes"
OPEN_LIBRARY_API = "https://openlibrary.org/api/books"
OPEN_LIBRARY_SEARCH_API = "https://openlibrary.org/search.json"
OPENALEX_API = "https://api.openalex.org/works"
CROSSREF_API = "https://api.crossref.org/works"

EDITION_RE = re.compile(
    r'(\b\d+(?:st|nd|rd|th)\s+ed\.?|\btái\s*bản|\bấn\s*bản|\brev\.\s*ed\.?)',
    re.IGNORECASE
)

ORDINAL_MAP = {
    "first": "1", "1st": "1", "nhất": "1", "đầu": "1",
    "second": "2", "2nd": "2", "hai": "2", "nhì": "2",
    "third": "3", "3rd": "3", "ba": "3",
    "fourth": "4", "4th": "4", "tư": "4", "bốn": "4",
    "fifth": "5", "5th": "5", "năm": "5",
    "sixth": "6", "6th": "6", "sáu": "6",
    "seventh": "7", "7th": "7", "bảy": "7",
    "eighth": "8", "8th": "8", "tám": "8",
    "ninth": "9", "9th": "9", "chín": "9",
    "tenth": "10", "10th": "10", "mười": "10",
}


VN_ORDINALS = "nhất|đầu|hai|nhì|ba|tư|bốn|năm|sáu|bảy|tám|chín|mười"


class _ApiStats:
    """Đếm số lần gọi API thành công/thất bại để phân biệt 'không tìm thấy' với 'không tra được'."""
    ok = 0
    failed = 0

    @classmethod
    def reset(cls):
        cls.ok = cls.failed = 0


def _get_json(url: str, params: dict):
    if url == GOOGLE_BOOKS_API and GOOGLE_BOOKS_API_KEY:
        params = {**params, "key": GOOGLE_BOOKS_API_KEY}
    try:
        resp = http_get(url, params=params, timeout=12)
    except Exception:
        _ApiStats.failed += 1
        return None
    if resp.status_code != 200:
        _ApiStats.failed += 1
        return None
    _ApiStats.ok += 1
    try:
        return resp.json()
    except ValueError:
        return None


def extract_edition_number(text: str) -> str:
    """Trích xuất số thứ tự lần tái bản (vd: '(4th ed.)' -> '4', 'tái bản lần thứ 2' -> '2')."""
    if not text:
        return ""
    text_lower = text.lower()

    m = re.search(
        r'(?:\b(\d+)(?:st|nd|rd|th)\s*ed(?:ition)?\.?|\btái\s*bản\s*(?:lần\s*(?:thứ\s*)?)?(\d+)|\b(?:in|xuất\s*bản)\s*lần\s*(?:thứ\s*)?(\d+))',
        text_lower
    )
    if m:
        return next(g for g in m.groups() if g is not None)

    m_vn = re.search(r'\b(?:tái\s*bản|in|xuất\s*bản)\s*lần\s*(?:thứ\s*)?(' + VN_ORDINALS + r')\b', text_lower)
    if m_vn:
        return ORDINAL_MAP.get(m_vn.group(1), "")

    m_word = re.search(
        r'\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\s+ed(?:ition)?\b',
        text_lower
    )
    if m_word:
        return ORDINAL_MAP.get(m_word.group(1), "")

    return ""


def lookup_book_by_isbn(isbn: str) -> dict:
    data = _get_json(GOOGLE_BOOKS_API, {"q": f"isbn:{isbn}"})
    items = (data or {}).get("items", [])
    if items:
        info = items[0].get("volumeInfo", {})
        title = info.get("title", "")
        subtitle = info.get("subtitle", "")
        return {
            "status": "OK",
            "title": title,
            "full_title": f"{title}: {subtitle}" if subtitle else title,
            "authors": parse_author_dicts(info.get("authors", []), source="book_api"),
            "year": extract_year_from_str(info.get("publishedDate", "")),
            "publisher": info.get("publisher", ""),
            "description": info.get("description", ""),
            "isbn": isbn,
        }

    bibkey = f"ISBN:{isbn}"
    data = _get_json(OPEN_LIBRARY_API, {"bibkeys": bibkey, "format": "json", "jscmd": "data"})
    rec = (data or {}).get(bibkey)
    if rec:
        title = rec.get("title", "")
        subtitle = rec.get("subtitle", "")
        publishers = rec.get("publishers", [])
        return {
            "status": "OK",
            "title": title,
            "full_title": f"{title}: {subtitle}" if subtitle else title,
            "authors": parse_author_dicts(rec.get("authors", []), source="book_api"),
            "year": extract_year_from_str(rec.get("publish_date", "")),
            "publisher": publishers[0].get("name", "") if publishers else "",
            "description": "",
            "isbn": isbn,
        }
    return {"status": "ISBN_NOT_FOUND"}


def lookup_exact_edition_year(title: str, edition_num: str) -> list:
    """Truy vấn theo Tên sách + Số lần tái bản để lấy chính xác năm của lần tái bản đó."""
    if not edition_num:
        return []

    edition_candidates = []
    norm_target = normalize_text(title)
    suffix = {"1": "1st", "2": "2nd", "3": "3rd"}.get(edition_num, f"{edition_num}th")

    # Google Books. (Đã bỏ nhánh Open Library: các mảng edition_name và publish_date của search.json
    # KHÔNG tương ứng theo thứ tự, nên ghép zip() cho ra năm sai mà lại được đánh dấu "đúng ấn bản".)
    query = f'intitle:"{title}" "{suffix} edition"'
    data = _get_json(GOOGLE_BOOKS_API, {"q": query, "maxResults": 6})
    for item in (data or {}).get("items", []):
        info = item.get("volumeInfo", {})
        api_title = info.get("title", "")
        subtitle = info.get("subtitle", "")
        desc = info.get("description", "")
        combined_meta = f"{api_title} {subtitle} {desc}"

        if extract_edition_number(combined_meta) == edition_num:
            sim = difflib.SequenceMatcher(None, norm_target, normalize_text(api_title)).ratio()
            if sim >= 0.80:
                edition_candidates.append({
                    "status": "FOUND",
                    "similarity": sim,
                    "title": api_title,
                    "full_title": f"{api_title}: {subtitle}" if subtitle else api_title,
                    "authors": parse_author_dicts(info.get("authors", []), source="book_api"),
                    "year": extract_year_from_str(info.get("publishedDate", "")),
                    "publisher": info.get("publisher", ""),
                    "description": desc,
                    "exact_edition_matched": True,
                })

    return edition_candidates


def search_book_candidates(title: str, cited_text: str = "") -> list:
    """Thu thập danh sách ứng viên sách từ Google Books, Open Library, OpenAlex và Crossref."""
    if not title or len(title.strip()) < 4:
        return []

    candidates = []
    norm_target = normalize_text(title)
    is_vn = has_vietnamese_diacritics(title) or has_vietnamese_diacritics(cited_text)

    gb_queries = [{"q": f'intitle:"{title}"', "maxResults": 10}]
    if is_vn:
        author_hint = cited_text.split("(")[0].strip() if "(" in cited_text else ""
        if author_hint:
            gb_queries.append({"q": f'"{title}" "{author_hint}"', "maxResults": 8})
        gb_queries.append({"q": title, "langRestrict": "vi", "maxResults": 10})

    seen_ids = set()
    for params in gb_queries:
        try:
            data = _get_json(GOOGLE_BOOKS_API, params)
            if data:
                for item in data.get("items", []):
                    vol_id = item.get("id", "")
                    if vol_id and vol_id in seen_ids:
                        continue
                    seen_ids.add(vol_id)

                    info = item.get("volumeInfo", {})
                    api_title = info.get("title", "")
                    subtitle = info.get("subtitle", "")
                    full_title = f"{api_title}: {subtitle}" if subtitle else api_title
                    sim = max(
                        difflib.SequenceMatcher(None, norm_target, normalize_text(api_title)).ratio(),
                        difflib.SequenceMatcher(None, norm_target, normalize_text(full_title)).ratio()
                    )
                    candidates.append({
                        "status": "FOUND",
                        "similarity": sim,
                        "title": api_title,
                        "full_title": full_title,
                        "authors": parse_author_dicts(info.get("authors", []), source="book_api"),
                        "year": extract_year_from_str(info.get("publishedDate", "")),
                        "publisher": info.get("publisher", ""),
                        "description": info.get("description", ""),
                    })
        except Exception:
            pass

    try:
        data = _get_json(OPEN_LIBRARY_SEARCH_API, {
            "title": title,
            "limit": 8,
            "fields": "title,subtitle,author_name,publisher,first_publish_year,publish_year"
        })
        if data:
            for doc_item in data.get("docs", []):
                api_title = doc_item.get("title", "")
                subtitle = doc_item.get("subtitle", "")
                full_title = f"{api_title}: {subtitle}" if subtitle else api_title
                sim = max(
                    difflib.SequenceMatcher(None, norm_target, normalize_text(api_title)).ratio(),
                    difflib.SequenceMatcher(None, norm_target, normalize_text(full_title)).ratio()
                )
                pubs = doc_item.get("publisher", [])
                pub_years = doc_item.get("publish_year", [])
                year_val = extract_year_from_str(doc_item.get("first_publish_year", ""))
                candidates.append({
                    "status": "FOUND",
                    "similarity": sim,
                    "title": api_title,
                    "full_title": full_title,
                    "authors": parse_author_dicts(doc_item.get("author_name", []), source="book_api"),
                    "year": year_val,
                    "all_years": [str(y) for y in pub_years],
                    "publisher": pubs[0] if pubs else "",
                    "all_publishers": pubs,
                    "description": "",
                })
    except Exception:
        pass

    try:
        data = _get_json(OPENALEX_API, {"search": title, "filter": "type:book", "per-page": 5, "mailto": EMAIL})
        if data:
            for work in data.get("results", []):
                api_title = work.get("title", "") or ""
                if not api_title:
                    continue
                sim = difflib.SequenceMatcher(None, norm_target, normalize_text(api_title)).ratio()
                raw_authors = [
                    a.get("author", {}).get("display_name", "")
                    for a in work.get("authorships", [])
                    if a.get("author", {}).get("display_name")
                ]
                loc = work.get("primary_location") or {}
                src = loc.get("source") or {}
                candidates.append({
                    "status": "FOUND",
                    "similarity": sim,
                    "title": api_title,
                    "full_title": api_title,
                    "authors": parse_author_dicts(raw_authors, source="book_api"),
                    "year": extract_year_from_str(work.get("publication_year", "")),
                    "publisher": src.get("display_name", ""),
                    "description": "",
                })
    except Exception:
        pass

    try:
        data = _get_json(CROSSREF_API, {
            "query.title": title,
            "filter": "type:book,type:monograph,type:edited-book",
            "rows": 5,
            "mailto": EMAIL
        })
        if data:
            for item in data.get("message", {}).get("items", []):
                titles = item.get("title", [])
                if not titles:
                    continue
                api_title = titles[0]
                sim = difflib.SequenceMatcher(None, norm_target, normalize_text(api_title)).ratio()
                pub_date = item.get("published", {}).get("date-parts", [[""]])[0]
                year_val = str(pub_date[0]) if pub_date and pub_date[0] else ""
                candidates.append({
                    "status": "FOUND",
                    "similarity": sim,
                    "title": api_title,
                    "full_title": api_title,
                    "authors": parse_author_dicts(item.get("author", []), source="crossref"),
                    "year": year_val,
                    "publisher": item.get("publisher", ""),
                    "description": "",
                })
    except Exception:
        pass

    return candidates


def collect_all_valid_years(candidates: list, cited_title: str) -> set:
    """Gom tất cả các năm xuất bản/tái bản từ mọi ấn bản có cùng tiêu đề."""
    valid_years = set()
    norm_target = normalize_text(cited_title)

    for cand in candidates:
        sim = difflib.SequenceMatcher(
            None, norm_target, normalize_text(cand.get("title", ""))
        ).ratio()
        sim_full = difflib.SequenceMatcher(
            None, norm_target, normalize_text(cand.get("full_title", ""))
        ).ratio()

        if max(sim, sim_full) >= TITLE_MATCH_THRESHOLD:
            if cand.get("year"):
                valid_years.add(str(cand["year"]))
            for y in cand.get("all_years", []):
                if y:
                    valid_years.add(str(y))

    return valid_years


PUB_STOP_WORDS = {
    "nha", "xuat", "ban", "nxb", "press", "publishing", "publisher", "publishers", "house", "the", "and",
    "of", "inc", "ltd", "llc", "co", "company", "group", "books", "verlag", "editions", "university", "dai", "hoc",
}


def publisher_in_text(pub: str, text_key: str) -> bool:
    if word_in(pub, text_key):
        return True
    words = norm_key(html.unescape(pub).replace("&", " and ")).split()
    core = [w for w in words if w not in PUB_STOP_WORDS and len(w) > 1]
    if not core:
        return False
    hits = sum(1 for w in core if word_in(w, text_key))
    need = len(core) if len(core) <= 2 else -(-2 * len(core) // 3)     # ≤2 từ: đủ cả; >2 từ: ≥ 2/3
    return hits >= need


def evaluate_book_4_fields(
    text: str,
    cited_title: str,
    cited_year: str,
    book_info: dict,
    pooled_years: set
) -> dict:
    """Chấm điểm khớp trên 4 thành phần: Tên tác giả, Tiêu đề, Nhà xuất bản, Năm xuất bản."""
    matched_fields = []
    mismatched_details = []

    # 1. Tên tác giả
    api_authors = book_info.get("authors", [])
    desc_norm = normalize_text(book_info.get("description", ""))
    author_hint = text.split("(")[0].strip() if "(" in text else ""
    author_hint_norm = normalize_text(author_hint)

    author_res = check_authors(text, api_authors)
    if author_res["status"] == "MATCH":
        matched_fields.append("Tên tác giả")
    elif author_hint_norm and len(author_hint_norm) > 4 and author_hint_norm in desc_norm:
        matched_fields.append("Tên tác giả/dịch giả")
    elif api_authors:
        all_formatted = [format_author_apa(a.get("family", ""), a.get("given", ""))
                         for a in api_authors if a.get("family")]
        mismatched_details.append(f"Tác giả theo dữ liệu: {', '.join(all_formatted)}")

    # 2. Tiêu đề sách
    official_title = book_info.get("title", "")
    official_full_title = book_info.get("full_title", official_title)
    sim_main = difflib.SequenceMatcher(None, normalize_text(cited_title), normalize_text(official_title)).ratio()
    sim_full = difflib.SequenceMatcher(None, normalize_text(cited_title), normalize_text(official_full_title)).ratio()
    best_title_sim = max(sim_main, sim_full)

    if best_title_sim >= TITLE_MATCH_THRESHOLD:
        if sim_main >= TITLE_MATCH_THRESHOLD and sim_full < 0.80 and ":" in official_full_title:
            mismatched_details.append(f"Tiêu đề đầy đủ (gồm phụ đề): \"{official_full_title}\"")
        else:
            matched_fields.append("Tiêu đề")
    elif official_full_title:
        mismatched_details.append(f"Tiêu đề đúng: \"{official_full_title}\"")

    # 3. Nhà xuất bản — so khớp không dấu, theo TỪ TRỌN VẸN, và phải trùng đa số từ cốt lõi
    #    (bản cũ chỉ cần 1 từ bất kỳ, nên "Oxford University Press" khớp với "Cambridge University Press",
    #    và stop-word tiếng Việt viết không dấu nên "nhà/xuất/bản" chưa bao giờ bị loại).
    official_pub = book_info.get("publisher", "")
    all_pubs = book_info.get("all_publishers", [official_pub] if official_pub else [])
    text_key = norm_key(text)

    pub_matched = False
    for pub in all_pubs:
        if pub and publisher_in_text(pub, text_key):
            pub_matched = True
            break

    if pub_matched:
        matched_fields.append("Nhà xuất bản")
    elif official_pub:
        mismatched_details.append(f"Nhà xuất bản theo dữ liệu: \"{official_pub}\"")

    # 4. Năm xuất bản
    api_year = book_info.get("year", "")
    has_edition_tag = bool(EDITION_RE.search(text))
    core_3_matched = len(matched_fields) == 3

    year_matched = False
    if cited_year:
        if book_info.get("exact_edition_matched"):
            year_matched = (cited_year == api_year)
        elif cited_year == api_year or cited_year in pooled_years:
            year_matched = True
        elif core_3_matched and pooled_years:
            valid_ints = [int(y) for y in pooled_years if y.isdigit()]
            if valid_ints and min(valid_ints) <= int(cited_year) <= CURRENT_YEAR + 1:
                year_matched = True
        elif has_edition_tag and ("Tên tác giả" in matched_fields or "Tên tác giả/dịch giả" in matched_fields) and "Tiêu đề" in matched_fields:
            year_matched = True

    if year_matched:
        matched_fields.append("Năm xuất bản")
    elif api_year:
        if book_info.get("exact_edition_matched"):
            mismatched_details.append(f"Năm xuất bản đúng của lần tái bản này: {api_year}")
        else:
            years_hint = ", ".join(sorted(pooled_years)) if pooled_years else api_year
            mismatched_details.append(f"Năm xuất bản ghi nhận trên hệ thống: {years_hint}")

    return {
        "score": len(matched_fields),
        "title_sim": best_title_sim,
        "exact_ed": 1 if book_info.get("exact_edition_matched") else 0,
        "matched_fields": matched_fields,
        "mismatched_details": mismatched_details,
        "book_info": book_info,
    }


def _not_found(doc, para, text: str, message: str) -> tuple[bool, int, int]:
    if has_vietnamese_diacritics(text) and not HIGHLIGHT_UNVERIFIED_VN:
        add_review_comment(doc, para, message + " Sách trong nước thường không có trên các CSDL quốc tế — cần kiểm tra thủ công.")
        return False, 1, 0
    add_review_comment(doc, para, message)
    highlight_whole_para(para)
    return False, 1, 1


def process_book_paragraph(doc, para, text: str) -> tuple[bool, int, int]:
    """Hàm entry chính của luồng Sách."""
    _ApiStats.reset()
    cited_title = extract_cited_title(text)
    cited_year = extract_cited_year(text)
    cited_isbn = extract_isbn(text)

    candidates = []

    if cited_isbn:
        isbn_res = lookup_book_by_isbn(cited_isbn)
        if isbn_res.get("status") == "OK":
            candidates.append(isbn_res)

    edition_num = extract_edition_number(text)
    if edition_num:
        candidates.extend(lookup_exact_edition_year(cited_title, edition_num))

    candidates.extend(search_book_candidates(cited_title, cited_text=text))

    if not candidates:
        if _ApiStats.failed and not _ApiStats.ok:
            add_review_comment(doc, para, "Chưa kiểm tra tự động được (các API sách đều lỗi hoặc giới hạn truy cập). Đề nghị kiểm tra thủ công.")
            print("    [BOOK] Tất cả API lỗi → chỉ comment, không tô vàng")
            return False, 1, 0
        return _not_found(doc, para, text, "Không tìm thấy sách tương ứng trên cơ sở dữ liệu.")

    pooled_years = collect_all_valid_years(candidates, cited_title)

    def _rank(ev):
        # Ưu tiên: khớp nhiều trường > đúng lần tái bản > có dữ liệu NXB > năm gần năm trích dẫn > tiêu đề giống
        info_ = ev["book_info"]
        y = info_.get("year", "")
        year_gap = abs(int(y) - int(cited_year)) if y.isdigit() and cited_year.isdigit() else 999
        return (ev["score"], ev["exact_ed"], bool(info_.get("publisher")), -year_gap, ev["title_sim"])

    best_eval = None
    for cand in candidates:
        ev = evaluate_book_4_fields(text, cited_title, cited_year, cand, pooled_years)
        if best_eval is None or _rank(ev) > _rank(best_eval):
            best_eval = ev

    score = best_eval["score"]
    title_sim = best_eval["title_sim"]
    info = best_eval["book_info"]

    if score == 4:
        print("    [BOOK] Khớp hoàn hảo 4/4 thành phần → OK")
        return True, 0, 0
    if score == 3 and not best_eval["mismatched_details"]:
        # 3 trường khớp, trường còn lại KHÔNG có dữ liệu để so (thường là NXB) -> không có gì sai để báo
        print("    [BOOK] Khớp 3/4, trường còn lại thiếu dữ liệu đối chiếu → OK")
        return True, 0, 0

    if score in (2, 3) or title_sim >= 0.90:
        authors_str = ", ".join([
            format_author_apa(a.get("family", ""), a.get("given", ""))
            for a in info.get("authors", []) if a.get("family")
        ]) or "Chưa rõ tác giả"
        year_str = info.get("year") or "n.d."
        title_str = info.get("full_title") or info.get("title") or ""
        pub_str = info.get("publisher") or ""

        suggested = f"{authors_str} ({year_str}). {title_str}."
        if pub_str:
            suggested += f" {pub_str}."

        diff_notes = "; ".join(best_eval["mismatched_details"]) if best_eval["mismatched_details"] else "Thiếu dữ liệu đối chiếu từ API"
        matched_str = ", ".join(best_eval["matched_fields"]) if best_eval["matched_fields"] else "không có thành phần nào"

        comment_text = (
            f"Sách khớp {score}/4 thành phần ({matched_str}). "
            f"Chi tiết khác biệt: {diff_notes}. "
            f"Ấn bản gần khớp nhất trên hệ thống: {suggested}"
        )
        add_review_comment(doc, para, comment_text)
        print(f"    [BOOK] Khớp {score}/4 (title_sim={title_sim:.2f}) → Đã comment đề xuất")
        return True, 1, 0

    print(f"    [BOOK] Chỉ khớp {score}/4 → Không tìm thấy")
    return _not_found(doc, para, text, "Không tìm thấy sách tương ứng (dữ liệu đối chiếu chỉ trùng khớp ≤ 1/4 thành phần).")
