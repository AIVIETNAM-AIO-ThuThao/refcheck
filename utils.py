import html
import re
import time
import os
import unicodedata
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from docx.enum.text import WD_COLOR_INDEX
from docx.oxml.ns import qn
from docx.text.run import Run

# ============ CẤU HÌNH CHUNG ============
INPUT_FILE = Path(r"C:\Users\ThuThao\Desktop\TÀI LIỆU THAM KHẢO1.docx")   # có thể truyền qua dòng lệnh
OUTPUT_FILE = INPUT_FILE.with_name(INPUT_FILE.stem + "_reviewed.docx")
AUTHOR_NAME = "BTV"
EMAIL = os.environ.get("REFCHECK_EMAIL", "akitokusa@example.com")   # đọc từ Secrets REFCHECK_EMAIL; ĐỔI email mẫu nếu chạy trên máy
GOOGLE_BOOKS_API_KEY = os.environ.get("GOOGLE_BOOKS_API_KEY", "")
TITLE_MATCH_THRESHOLD = 0.85
WEB_TITLE_THRESHOLD = 0.78
REQUEST_DELAY = 0.5               # khoảng cách tối thiểu (giây) giữa 2 request tới CÙNG một máy chủ
CURRENT_YEAR = date.today().year


HIGHLIGHT_UNVERIFIED_VN = False
HIGHLIGHT_UNCHECKED = False
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
}

# ============ REGEX DÙNG CHUNG ============
DOI_RE = re.compile(r'(10\.\d{4,9}/[^\s,;]+)', re.IGNORECASE)
URL_RE = re.compile(r'(https?://[^\s<>"\'\)\]]+)', re.IGNORECASE)
ISBN_RE = re.compile(r'(?:ISBN(?:-1[03])?:?\s*)?((?:97[89][-\s]?)?\d{1,5}[-\s]?\d{1,7}[-\s]?\d{1,6}[-\s]?[\dX])', re.IGNORECASE)
LIST_MARKER_RE = re.compile(r'^\s*(?:[\uf0b7\u2022\u25cf\u25aa\u2013\-*]\s*|\[\d+\]\s*|\d{1,3}[.)]\s+)')
LNCS_TITLE_RE = re.compile(r'^(?P<auth>[^:]{3,400}?\b[A-Z]\.(?:-?[A-Z]\.)*)\s*:\s+(?P<title>.+?)\.(?:\s|$)')  # "Li, Z., Smith, J.S.: Title. In: ..."
PAGE_RE = re.compile(r'([A-Za-z]?\d+)\s*[-–—]\s*([A-Za-z]?\d+)')
JOURNAL_PATTERN_RE = re.compile(r'(\b\d+\s*\(\d+\)\s*[,:]\s*\d+|\bVol\.\s*\d+|\bIssue\s*\d+)', re.IGNORECASE)
JOURNAL_HINT_RE = re.compile(r'tạp\s*chí|\bjournal\b|\bproceedings\b|kỷ\s*yếu|\btransactions\b', re.IGNORECASE)
CHAPTER_RE = re.compile(r'(?:^|[.,;]\s*)(?:In|Trong)\s*:?\s+\S')        # "In J. Smith (Ed.), ..." / "Trong: ..."
EDITOR_RE = re.compile(r'\((?:eds?|chủ\s*biên|cb)\.?\)', re.IGNORECASE)
ETAL_RE = re.compile(r'\bet\s*al\b|cộng\s*sự|\bvà\s*cs\b|\band\s+others\b', re.IGNORECASE)

VIETNAMESE_CHARS_RE = re.compile(
    r'[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]',
    re.IGNORECASE
)

# Họ phổ biến của người Việt (không dấu) — để nhận ra tên viết theo thứ tự Họ + Tên khi API bỏ dấu
VN_SURNAMES = {
    "nguyen", "tran", "le", "pham", "hoang", "huynh", "phan", "vu", "vo", "dang", "bui",
    "do", "ho", "ngo", "duong", "ly", "truong", "dinh", "doan", "lam", "mai", "trinh",
    "luong", "luu", "cao", "ha", "ta", "quach", "thai", "chau", "kieu", "la", "tong", "trieu",
}

PUBLISHER_KEYWORDS = [
    "publishing house", "press", "publishers", "publishing", "publications",
    "nhà xuất bản", "nxb", "edition", "handbook", "textbook"
]

# Đã bỏ "giáo dục", "trẻ", "tri thức": các từ này rất hay xuất hiện trong tên bài báo/tạp chí tiếng Việt
FAMOUS_PUBLISHERS = [
    "springer", "elsevier", "wiley", "john wiley", "pearson", "mcgraw-hill", "mcgraw hill",
    "oxford university press", "cambridge university press", "mit press", "harvard university press",
    "routledge", "sage", "taylor & francis", "crc press", "academic press", "o'reilly", "packt",
    "apress", "addison-wesley", "prentice hall", "cengage", "macmillan", "palgrave", "world scientific",
    "khoa học và kỹ thuật", "đại học quốc gia", "chính trị quốc gia",
]


# ============ HTTP DÙNG CHUNG (retry + giãn cách theo máy chủ) ============
_SESSION = None
_LAST_CALL: dict = {}


def _session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        s = requests.Session()
        retry = Retry(
            total=3, backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",), respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=10)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _SESSION = s
    return _SESSION


def http_get(url: str, **kwargs) -> requests.Response:
    """requests.get qua một Session dùng chung: tự thử lại khi 429/5xx và giãn cách request theo máy chủ."""
    host = urlparse(url).netloc
    wait = REQUEST_DELAY - (time.monotonic() - _LAST_CALL.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)
    kwargs.setdefault("timeout", 15)
    try:
        return _session().get(url, **kwargs)
    finally:
        _LAST_CALL[host] = time.monotonic()


# ============ CHUẨN HÓA VĂN BẢN ============
def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFC", html.unescape(text or ""))   # gộp Unicode tổ hợp (NFD) về dựng sẵn (NFC)
    text = re.sub(r'[\u00ad\u200b-\u200d\u2060\ufeff]', '', text)   # soft hyphen, ký tự rộng 0 (hay lẫn khi copy từ PDF/Word)
    text = re.sub(r'<[^>]+>', ' ', text)          # Crossref hay chèn <i>, <sub>, <scp> vào tiêu đề
    text = text.lower()
    text = re.sub(r'[^\w\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def strip_accents(text: str) -> str:
    text = (text or "").replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def norm_key(text: str) -> str:
    """Chuẩn hóa để SO KHỚP: bỏ dấu, bỏ HTML, bỏ dấu câu, chữ thường."""
    return normalize_text(strip_accents(text))


def word_in(needle: str, haystack_key: str) -> bool:
    """needle có xuất hiện như một (cụm) từ trọn vẹn trong haystack_key (đã qua norm_key) không."""
    n = norm_key(needle)
    if not n:
        return False
    return re.search(r'(?<!\w)' + re.escape(n) + r'(?!\w)', haystack_key) is not None


def strip_urls_dois(text: str) -> str:
    text = URL_RE.sub(" ", text)
    text = re.sub(r'\bdoi:\s*', ' ', text, flags=re.IGNORECASE)
    return DOI_RE.sub(" ", text)


def has_vietnamese_diacritics(text: str) -> bool:
    return bool(VIETNAMESE_CHARS_RE.search(text or ""))


def clean_doi(doi: str) -> str:
    """Bỏ tiền tố doi.org/doi: và dấu câu/ngoặc thừa ở cuối (giữ ngoặc cân bằng như 10.1016/S0140-6736(20)30183-5)."""
    doi = re.sub(r'^(https?://(dx\.)?doi\.org/|doi:\s*)', '', doi.strip(), flags=re.IGNORECASE)
    pairs = {')': '(', ']': '[', '}': '{', '>': '<'}
    doi = doi.rstrip('.,;:')
    while doi:
        c = doi[-1]
        if c in '"\'' or (c in pairs and doi.count(c) > doi.count(pairs[c])):
            doi = doi[:-1].rstrip('.,;:')
        else:
            break
    return doi


# ============ THAO TÁC WORD ============
def get_para_runs(para) -> list:
    """Mọi run của đoạn, KỂ CẢ run nằm trong hyperlink (para.runs bỏ sót các run này)."""
    runs = []
    try:
        for item in para.iter_inner_content():
            if isinstance(item, Run):
                runs.append(item)
            else:                       # Hyperlink
                runs.extend(item.runs)
    except AttributeError:
        runs = list(para.runs)
    return runs


def highlight_whole_para(para):
    """Tô vàng toàn bộ đoạn văn bản (cả phần là hyperlink)."""
    for run in get_para_runs(para):
        if run.text.strip():
            run.font.highlight_color = WD_COLOR_INDEX.YELLOW
            rpr = run._r.rPr
            change = rpr.find(qn('w:rPrChange')) if rpr is not None else None
            if change is not None:
                rpr.remove(change)
                rpr.append(change)

def _lift_comment_markers(para, comment_id: int):
    """Nếu dấu mốc comment rơi vào trong <w:hyperlink>, đưa chúng ra ngoài ở cấp đoạn văn."""
    p = para._p
    sid = str(comment_id)

    def top_level(el):
        while el.getparent() is not None and el.getparent() is not p:
            el = el.getparent()
        return el if el.getparent() is p else None

    for el in list(p.iter(qn('w:commentRangeStart'))):
        if el.get(qn('w:id')) == sid and el.getparent() is not p:
            anc = top_level(el)
            if anc is not None:
                el.getparent().remove(el)
                anc.addprevious(el)
    end_el = None
    for el in list(p.iter(qn('w:commentRangeEnd'))):
        if el.get(qn('w:id')) == sid:
            end_el = el
            if el.getparent() is not p:
                anc = top_level(el)
                if anc is not None:
                    el.getparent().remove(el)
                    anc.addnext(el)
    for ref in list(p.iter(qn('w:commentReference'))):
        if ref.get(qn('w:id')) == sid:
            r = ref.getparent()
            if r.getparent() is not p:
                anc = top_level(r)
                if anc is not None:
                    r.getparent().remove(r)
                    (end_el if end_el is not None and end_el.getparent() is p else anc).addnext(r)


def add_review_comment(doc, para, text: str):
    """Gắn comment phủ cả đoạn; không lỗi khi đoạn chỉ gồm hyperlink hoặc không có run."""
    runs = get_para_runs(para)
    if not runs:
        runs = [para.add_run("")]
    comment = doc.add_comment(runs=[runs[0], runs[-1]], text=text, author=AUTHOR_NAME, initials="RV")
    _lift_comment_markers(para, comment.comment_id)
    return comment


# ============ TRÍCH XUẤT THÔNG TIN TRÍCH DẪN ============
def extract_cited_year(ref_text: str) -> str:
    m = re.search(r'\((\d{4})[a-z]?(?:,[^)]*)?\)', ref_text)
    return m.group(1) if m else ""


def extract_year_from_str(date_str) -> str:
    if not date_str:
        return ""
    m = re.search(r'(19|20)\d{2}', str(date_str))
    return m.group(0) if m else ""


def strip_list_marker(text: str) -> str:
    """Bỏ ký hiệu đầu dòng: bullet (kể cả ký tự Symbol \uf0b7 của Word), '[12]', '10.'."""
    return LIST_MARKER_RE.sub('', text or '', count=1).strip()


def extract_cited_title(ref_text: str) -> str:
    m = LNCS_TITLE_RE.match(ref_text)          # kiểu Springer/LNCS: tác giả, rồi dấu ':' rồi tiêu đề
    if m:
        return m.group("title").strip()
    m = re.search(
        r'\(\d{4}[a-z]?(?:,\s*[^)]+)?\)[.,]\s*(.+?)(?:(?<!\b[A-Z])(?<!\bvs)(?<!\be\.g)(?<!\bi\.e)(?<!\bal)(?<!\bed)(?<!\bjr)(?<!\bdr)(?<!\bst)\.\s|,\s*http|,\s*truy\s*cập|,\s*retrieved|$)',
        ref_text,
        re.IGNORECASE
    )
    if m:
        title = m.group(1).strip()
        title = re.sub(r'\(\d+(?:st|nd|rd|th)\s+ed\.?\)', '', title, flags=re.IGNORECASE).strip()
        # Kiểu web "Tiêu đề, Tên trang, https://..." : chỉ tách khi tiêu đề kéo dài sát tới URL (kết thúc bằng dấu phẩy)
        # và dòng không có DOI; khi đó bỏ phần SAU dấu phẩy CUỐI (tên trang), không cắt ở dấu phẩy đầu tiên.
        ended_at_comma = ref_text[m.end(1):m.end(1) + 1] == ","
        if ended_at_comma and extract_web_url(ref_text) and not DOI_RE.search(ref_text) and "," in title:
            head, tail = title.rsplit(",", 1)
            if len(head.strip()) > 15 and 0 < len(tail.strip()) <= 60:
                return head.strip()
        return title
    m = re.search(r'["“](.+?)["”]', ref_text)
    if m:
        return m.group(1).strip()
    parts = [p.strip() for p in ref_text.split('.') if len(p.strip()) > 15 and "http" not in p.lower()]
    return max(parts, key=len) if parts else ref_text


def title_extraction_failed(cited_title: str, ref_text: str) -> bool:
    return not cited_title or cited_title.strip() == ref_text.strip()


def _is_year_range(a: str, b: str) -> bool:
    """'2019-2020' là khoảng năm, không phải số trang (còn '1735-1780' vẫn là số trang)."""
    if a.isdigit() and b.isdigit() and len(a) == 4 and len(b) == 4:
        ia, ib = int(a), int(b)
        return 1900 <= ia <= 2099 and 1900 <= ib <= 2099 and 0 <= ib - ia <= 10
    return False


def extract_cited_pages(ref_text: str) -> str:
    """Lấy số trang/mã bài từ trích dẫn; bỏ qua số nằm trong URL/DOI, khoảng năm và ISSN."""
    t = strip_urls_dois(ref_text)
    t = re.sub(r'\bISSN\s*:?\s*\d{4}-\d{3}[\dX]', ' ', t, flags=re.IGNORECASE)
    patterns = [
        r'\d+\s*\([^()]{1,15}\)\s*[,:]\s*([A-Za-z]?\d+)(?:\s*[-–—]\s*([A-Za-z]?\d+))?',   # 12(3), 45-67
        r'\b(?:pp?|tr|trang)\.?\s*([A-Za-z]?\d+)(?:\s*[-–—]\s*([A-Za-z]?\d+))?',            # pp. 45-67 / tr. 12-20
        r'\b\d+\s*,\s*([A-Za-z]?\d+)\s*[-–—]\s*([A-Za-z]?\d+)',                              # 12, 45-67
    ]
    for pat in patterns:
        m = re.search(pat, t, re.IGNORECASE)
        if m:
            a, b = m.group(1), m.group(2)
            if b and _is_year_range(a, b):
                continue
            return f"{a}-{b}" if b else a
    for m in PAGE_RE.finditer(t):
        a, b = m.groups()
        if not _is_year_range(a, b):
            return f"{a}-{b}"
    return ""


def _page_range(s: str):
    s = (s or "").replace('–', '-').replace('—', '-').replace(' ', '')
    parts = [p for p in s.split('-') if p]
    if not parts:
        return None, None
    start = parts[0]
    end = parts[1] if len(parts) > 1 else start
    if start.isdigit() and end.isdigit() and len(end) < len(start):      # 1234-45 -> 1234-1245
        end = start[:len(start) - len(end)] + end
    return start.lower(), end.lower()


def pages_match(cited_pages: str, registry_pages: str) -> bool:
    cs, ce = _page_range(cited_pages)
    rs, re_ = _page_range(registry_pages)
    if cs is None or rs is None:
        return False
    if cs == rs and ce == re_:
        return True
    return rs == re_ and cs == rs              # registry chỉ có trang đầu/mã bài


def isbn_checksum_ok(digits: str) -> bool:
    d = digits.upper()
    if len(d) == 10 and re.fullmatch(r'\d{9}[\dX]', d):
        total = sum((10 - i) * (10 if c == 'X' else int(c)) for i, c in enumerate(d))
        return total % 11 == 0
    if len(d) == 13 and d.isdigit():
        total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(d))
        return total % 10 == 0
    return False


def extract_isbn(ref_text: str) -> str:
    text = strip_urls_dois(ref_text)
    for m in ISBN_RE.finditer(text):
        raw_digits = re.sub(r'[-\s]', '', m.group(1))
        if len(raw_digits) in (10, 13) and isbn_checksum_ok(raw_digits):
            if "isbn" in m.group(0).lower() or len(raw_digits) == 13:
                return raw_digits
    return ""


def extract_web_url(ref_text: str) -> str:
    for m in URL_RE.finditer(ref_text):
        url = m.group(1).rstrip('.,;:')
        if "doi.org/" not in url.lower():
            return url
    return ""


def is_section_header(text: str) -> bool:
    normalized = re.sub(r'[^a-zA-ZÀ-ỹ\s]', ' ', text)
    normalized = re.sub(r'\s+', ' ', normalized).strip().lower()
    exact_candidates = [
        "tài liệu tham khảo", "danh mục tài liệu tham khảo",
        "reference", "references", "reference list", "bibliography",
    ]
    if normalized in exact_candidates:
        return True
    if len(normalized) <= 60:
        return any(c in normalized for c in exact_candidates)
    return False


# ============ TÁC GIẢ ============
def is_vietnamese_name(family: str, given: str = "") -> bool:
    full = f"{family} {given}".strip()
    if has_vietnamese_diacritics(full):
        return True
    return norm_key(family) in VN_SURNAMES and bool(given)


def split_person_name(name: str):
    """Tách chuỗi tên thành (họ, tên). Hỗ trợ 'Họ, Tên', tên Việt (Họ đứng đầu) và tên phương Tây (Họ đứng cuối)."""
    name = re.sub(r'\s+', ' ', (name or "")).strip()
    if not name:
        return "", ""
    if "," in name:
        family, given = [p.strip() for p in name.split(",", 1)]
        return family, given
    parts = name.split()
    if len(parts) == 1:
        return parts[0], ""
    if has_vietnamese_diacritics(name) or norm_key(parts[0]) in VN_SURNAMES:
        return parts[0], " ".join(parts[1:])
    return parts[-1], " ".join(parts[:-1])


def format_author_apa(family: str, given: str) -> str:
    """
    Định dạng tên tác giả khi gợi ý bổ sung:
    - Quốc tế: 'Mirza M.', 'Goodfellow I. J.'
    - Việt Nam: giữ nguyên 'Nguyễn Văn A' (Họ + Tên)
    """
    if not family:
        return ""
    if not given:
        return family
    if is_vietnamese_name(family, given):
        return f"{family} {given}".strip()
    parts = [p for p in re.split(r'[\s\-]+', given.strip()) if p]
    initials = " ".join([f"{part[0].upper()}." for part in parts])
    return f"{family} {initials}"


def parse_author_dicts(raw_authors: list, source: str = "crossref") -> list:
    result = []
    for a in raw_authors or []:
        if source == "crossref":
            family = a.get("family", "")
            given = a.get("given", "")
            if not family and a.get("name"):          # tác giả là tổ chức
                family, given = a["name"], ""
        elif source == "datacite":
            family = a.get("familyName", "")
            given = a.get("givenName", "")
            if not family and a.get("name"):
                if a.get("nameType") == "Organizational":
                    family, given = a["name"], ""
                else:
                    family, given = split_person_name(a["name"])
        else:  # book_api / web
            name_str = a if isinstance(a, str) else a.get("name", "")
            family, given = split_person_name(name_str)
        if family:
            result.append({"family": family, "given": given})
    return result


def _author_present(author: dict, cited_key: str) -> bool:
    family = author.get("family", "")
    given = author.get("given", "")
    if not word_in(family, cited_key):
        return False
    if is_vietnamese_name(family, given) and given:
        # tên Việt: họ "Nguyễn" quá phổ biến, cần khớp cả tên riêng (từ cuối)
        return word_in(given.split()[-1], cited_key)
    return True


def _format_author_list(authors: list) -> str:
    names = [format_author_apa(a.get("family", ""), a.get("given", "")) for a in authors if a.get("family")]
    if len(names) > 20:
        names = names[:19] + ["...", names[-1]]
    return ", ".join(names)


def check_authors(cited_text: str, registry_authors: list) -> dict:
    authors = [a for a in (registry_authors or []) if a.get("family")]
    if not authors:
        return {"status": "NO_REGISTRY_AUTHORS", "message": ""}

    cited_key = norm_key(strip_urls_dois(cited_text))
    if ETAL_RE.search(cited_text):
        to_check = authors[:1]                                  # "et al." / "và cộng sự": chỉ cần tác giả đầu
    elif '…' in cited_text or '...' in cited_text or '. . .' in cited_text:
        to_check = authors[:3] + authors[-1:]                   # có "...": kiểm tra 3 tác giả đầu và tác giả cuối
    else:
        to_check = authors

    missing = [a for a in to_check if not _author_present(a, cited_key)]
    if missing:
        return {
            "status": "MISSING_AUTHORS",
            "message": f"Thiếu/sai tác giả. Danh sách đúng: {_format_author_list(authors).rstrip('.')}."
        }
    return {"status": "MATCH", "message": ""}


# ============ PHÂN LOẠI ============
def classify_reference(ref_text: str) -> str:
    """Phân loại tài liệu thành 1 trong 3 luồng: 'WEB', 'BOOK', 'JOURNAL'."""
    key = norm_key(ref_text)
    web_url = extract_web_url(ref_text)
    has_doi = bool(DOI_RE.search(ref_text))
    has_journal_pat = bool(JOURNAL_PATTERN_RE.search(ref_text))
    is_chapter = bool(CHAPTER_RE.search(ref_text))

    if web_url and not has_journal_pat and not has_doi:
        return "WEB"
    if extract_isbn(ref_text):
        return "BOOK"
    if has_doi:
        return "JOURNAL"          # Crossref tra được cả bài báo, kỷ yếu lẫn chương sách có DOI
    if (has_journal_pat or JOURNAL_HINT_RE.search(ref_text)) and not is_chapter:
        return "JOURNAL"
    if EDITOR_RE.search(ref_text):
        return "BOOK"
    for kw in PUBLISHER_KEYWORDS + FAMOUS_PUBLISHERS:
        if word_in(kw, key):
            return "BOOK"
    return "JOURNAL"
