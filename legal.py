"""Luồng VĂN BẢN QUY PHẠM / VĂN BẢN CỦA ĐẢNG, NHÀ NƯỚC (Nghị quyết, Nghị định, Luật, Thông tư, Quyết định...).

Các văn bản này KHÔNG có trên Crossref, OpenAlex, Google Books..., tra ở đó chỉ tốn thời gian
(câu truy vấn dài tiếng Việt khiến Crossref phản hồi rất chậm → chương trình như bị đứng).
Luồng này không truy cập mạng, chỉ kiểm tra tính nhất quán của chính dòng trích dẫn:
  - có số hiệu và ngày ban hành chưa;
  - năm trong ngoặc khớp năm ban hành không; năm trong số hiệu khớp ngày ban hành không;
  - ký hiệu trong số hiệu khớp loại văn bản không (Nghị định ↔ NĐ, Quyết định ↔ QĐ...).
"""
import re

from utils import add_review_comment, extract_cited_year, extract_web_url, DOI_RE

DOC_TYPES = {                       # loại văn bản -> ký hiệu viết tắt phải có trong số hiệu (None: không kiểm)
    "nghị quyết": "NQ", "nghị định": "NĐ", "quyết định": "QĐ", "thông tư liên tịch": "TTLT",
    "thông tư": "TT", "chỉ thị": "CT", "kết luận": "KL", "quy định": "QĐ", "hướng dẫn": "HD",
    "thông báo": "TB", "công văn": None, "pháp lệnh": None, "bộ luật": None, "luật": None,
    "hiến pháp": None, "lệnh": None, "sắc lệnh": "SL", "văn bản hợp nhất": "VBHN",
}
TYPE_RE = re.compile(r"(?<!\w)(" + "|".join(sorted(DOC_TYPES, key=len, reverse=True)) + r")(?!\w)", re.I)
# Số hiệu: 57-NQ/TW, 15/2020/NĐ-CP, 59/2020/QH14, 749/QĐ-TTg, 04-HĐNN8, 01/VBHN-BTC
SO_HIEU_RE = re.compile(r"(?<![\w/])(\d{1,5})(?:/(\d{4}))?[/-]((?:[A-ZĐ]{1,6}\d{0,2})(?:[/-][A-ZĐ]{1,6}\d{0,2})*)(?![\w])")
DATE_TEXT_RE = re.compile(r"ngày\s+(\d{1,2})\s+tháng\s+(\d{1,2})\s+năm\s+(\d{4})", re.I)
DATE_NUM_RE = re.compile(r"(?<!\d)(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})(?!\d)")
PUBLISHER_HINT_RE = re.compile(r"\b(Nxb|NXB|Nhà xuất bản)\b")


def is_legal_document(text: str) -> bool:
    """Dòng trích dẫn là văn bản pháp quy/văn bản của Đảng (không phải sách, không có link/DOI)."""
    if extract_web_url(text) or DOI_RE.search(text) or PUBLISHER_HINT_RE.search(text):
        return False          # có link → để luồng Web mở link; có NXB (vd. Văn kiện Đại hội) → luồng Sách
    m = TYPE_RE.search(text)
    if not m:
        return False
    if m.group(1).lower() == "hiến pháp":
        return True
    return bool(SO_HIEU_RE.search(text[m.start():]))


def _issue_date(text: str):
    m = DATE_TEXT_RE.search(text) or DATE_NUM_RE.search(text)
    if not m:
        return None
    d, mo, y = (int(x) for x in m.groups())
    return d, mo, y


def process_legal_paragraph(doc, para, text: str) -> tuple[bool, int, int]:
    problems = []
    m_type = TYPE_RE.search(text)
    doc_type = m_type.group(1).lower() if m_type else ""
    after_type = text[m_type.start():] if m_type else text
    so_hieu = SO_HIEU_RE.search(after_type)
    date = _issue_date(text)
    cited_year = extract_cited_year(text)

    if doc_type != "hiến pháp":
        if not so_hieu:
            problems.append("Cần bổ sung số hiệu văn bản.")
        if not date:
            problems.append("Cần bổ sung ngày ban hành văn bản (ví dụ: ngày 22 tháng 12 năm 2024).")

    if date:
        d, mo, y = date
        if not (1 <= d <= 31 and 1 <= mo <= 12):
            problems.append(f"Ngày ban hành không hợp lệ: {d}/{mo}/{y}.")
        if cited_year and cited_year.isdigit() and int(cited_year) != y:
            problems.append(f"Năm trong ngoặc ({cited_year}) khác năm ban hành ghi trong ngày ({y}). Đề nghị kiểm tra.")
        if so_hieu and so_hieu.group(2) and int(so_hieu.group(2)) != y:
            problems.append(f"Năm trong số hiệu ({so_hieu.group(0)}) khác năm ban hành ({y}). Đề nghị kiểm tra.")

    expected = DOC_TYPES.get(doc_type)
    if so_hieu and expected:
        codes = re.split(r"[/-]", so_hieu.group(3).upper())
        codes = [re.sub(r"\d+$", "", c) for c in codes]
        if expected not in codes and not (expected == "TT" and "TTLT" in codes):
            problems.append(f"Số hiệu {so_hieu.group(0)} không khớp loại văn bản \"{m_type.group(1)}\" "
                            f"(thường có ký hiệu {expected}). Đề nghị kiểm tra.")

    if problems:
        add_review_comment(doc, para, " ".join(problems))
        print(f"    [LEGAL] {len(problems)} vấn đề: {' | '.join(problems)}")
        return True, 1, 0          # main._process_one sẽ tô vàng vì đây là comment chỉ ra vấn đề
    print("    [LEGAL] Văn bản pháp quy: số hiệu, ngày ban hành, năm nhất quán → OK (không tra cứu trực tuyến)")
    return True, 0, 0
