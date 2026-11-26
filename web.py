import json
import re
import difflib
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from utils import (
    BROWSER_HEADERS,
    WEB_TITLE_THRESHOLD,
    add_review_comment,
    check_authors,
    extract_cited_title,
    extract_cited_year,
    extract_web_url,
    highlight_whole_para,
    http_get,
    norm_key,
    normalize_text,
    parse_author_dicts,
    strip_urls_dois,
    title_extraction_failed,
    word_in,
)

MAX_HTML_BYTES = 3 * 1024 * 1024          # chỉ đọc tối đa 3 MB đầu trang
GENERIC_SUFFIXES = {"com", "net", "org", "gov", "edu", "ac", "co", "info", "int", "biz",
                    "vn", "uk", "us", "au", "jp", "cn", "kr", "de", "fr", "io", "health", "news"}
ARTICLE_TYPES = {"article", "newsarticle", "blogposting", "scholarlyarticle", "report", "webpage", "techarticle"}


def site_label(url: str) -> str:
    """'https://en.wikipedia.org/...' -> 'wikipedia'; 'moh.gov.vn' -> 'moh'."""
    host = re.sub(r'^www\.', '', urlparse(url).netloc.lower().split(':')[0])
    parts = host.split('.')
    while len(parts) > 1 and parts[-1] in GENERIC_SUFFIXES:
        parts.pop()
    return parts[-1] if parts else host


def _iter_json_ld_items(data):
    if isinstance(data, list):
        for d in data:
            yield from _iter_json_ld_items(d)
    elif isinstance(data, dict):
        if "@graph" in data:
            yield from _iter_json_ld_items(data["@graph"])
        else:
            yield data


def extract_json_ld_metadata(soup: BeautifulSoup) -> dict:
    meta = {"titles": [], "authors": [], "site_name": "", "year": ""}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            raw = script.string or script.get_text()
            if not raw:
                continue
            for item in _iter_json_ld_items(json.loads(raw)):
                types = item.get("@type", "")
                types = {t.lower() for t in (types if isinstance(types, list) else [types]) if isinstance(t, str)}
                if item.get("headline"):
                    meta["titles"].append(str(item["headline"]).strip())
                elif item.get("name") and types & ARTICLE_TYPES:
                    meta["titles"].append(str(item["name"]).strip())
                if not meta["authors"] and item.get("author"):
                    auth = item["author"]
                    for a in (auth if isinstance(auth, list) else [auth]):
                        if isinstance(a, dict) and a.get("name"):
                            meta["authors"].append(str(a["name"]))
                        elif isinstance(a, str):
                            meta["authors"].append(a)
                if not meta["year"] and item.get("datePublished"):
                    m = re.search(r'(19|20)\d{2}', str(item["datePublished"]))
                    if m:
                        meta["year"] = m.group(0)
                if not meta["site_name"] and isinstance(item.get("publisher"), dict):
                    meta["site_name"] = item["publisher"].get("name", "")
        except Exception:
            continue
    return meta


def _meta(soup, **attrs) -> str:
    tag = soup.find("meta", attrs=attrs)
    return tag["content"].strip() if tag and tag.get("content") else ""


def fetch_web_metadata(url: str) -> dict:
    try:
        resp = http_get(url, headers=BROWSER_HEADERS, timeout=20, allow_redirects=True, stream=True)
    except requests.exceptions.ConnectionError as e:
        msg = str(e)
        if re.search(r'NameResolution|Name or service not known|getaddrinfo|nodename nor servname|No address associated', msg):
            return {"status": "NOT_FOUND", "message": f"Tên miền không tồn tại: {url}"}
        return {"status": "ERROR", "message": f"không kết nối được tới {url}"}
    except requests.exceptions.Timeout:
        return {"status": "ERROR", "message": f"trang phản hồi quá chậm: {url}"}
    except Exception as e:
        return {"status": "ERROR", "message": f"không truy cập được {url}: {e}"}

    try:
        if resp.status_code in (404, 410):
            return {"status": "NOT_FOUND", "message": f"Đường dẫn trả về lỗi {resp.status_code} (trang không tồn tại): {url}"}
        if resp.status_code >= 400:
            # 401/403/429/5xx: thường là trang chặn bot hoặc lỗi tạm thời, KHÔNG kết luận là trang không tồn tại
            return {"status": "BLOCKED", "message": f"trang chặn truy cập tự động hoặc lỗi máy chủ, HTTP {resp.status_code}: {url}"}

        ctype = resp.headers.get("Content-Type", "").lower()
        if ctype and "html" not in ctype and "xml" not in ctype:
            return {"status": "NON_HTML", "content_type": ctype, "final_url": resp.url}

        chunks, size = [], 0
        for chunk in resp.iter_content(65536):
            chunks.append(chunk)
            size += len(chunk)
            if size >= MAX_HTML_BYTES:
                break
        body = b"".join(chunks)
    finally:
        resp.close()

    from_enc = resp.encoding if "charset=" in ctype else None
    soup = BeautifulSoup(body, "html.parser", from_encoding=from_enc)
    json_ld = extract_json_ld_metadata(soup)

    # Gom MỌI ứng viên tiêu đề rồi so với cái giống nhất (giảm báo nhầm "tiêu đề không khớp")
    titles = list(json_ld["titles"])
    for t in (_meta(soup, property="og:title"), _meta(soup, name="twitter:title"),
              _meta(soup, name="citation_title"), _meta(soup, name="dc.title")):
        if t:
            titles.append(t)
    h1 = soup.find("h1")
    if h1 and len(h1.get_text(strip=True)) > 10:
        titles.append(h1.get_text(" ", strip=True))
    if soup.title and soup.title.string:
        raw_title = soup.title.string.strip()
        titles.append(raw_title)
        titles.extend(p for p in re.split(r'\s+[\|\-–—]\s+', raw_title) if len(p) > 10)
    titles = [t for i, t in enumerate(titles) if t and t not in titles[:i]]

    site_name = json_ld["site_name"] or _meta(soup, property="og:site_name") or ""

    authors = json_ld["authors"]
    if not authors:
        a = _meta(soup, name=re.compile(r'^(author|citation_author|dc\.creator)$', re.IGNORECASE))
        if a:
            authors = [a]

    year = json_ld["year"]
    if not year:
        for d in (_meta(soup, property="article:published_time"), _meta(soup, name="citation_publication_date"),
                  _meta(soup, name="citation_date"), _meta(soup, name="dc.date")):
            m = re.search(r'(19|20)\d{2}', d)
            if m:
                year = m.group(0)
                break

    return {
        "status": "OK",
        "title": titles[0] if titles else "",
        "title_candidates": titles,
        "site_name": site_name,
        "authors": authors,
        "year": year,
        "final_url": resp.url,
    }


def _best_title_sim(cited_title: str, candidates: list) -> float:
    c = normalize_text(cited_title)
    best = 0.0
    for t in candidates:
        n = normalize_text(t)
        if not n:
            continue
        if len(c) >= 15 and (c in n or n in c) and min(len(c), len(n)) / max(len(c), len(n)) >= 0.5:
            return 1.0
        best = max(best, difflib.SequenceMatcher(None, c, n).ratio())
    return best


def process_web_paragraph(doc, para, text: str) -> tuple[bool, int, int]:
    """Hàm entry của luồng Web. Trả về: (found, comments_added, highlights_added)."""
    comments = 0
    url = extract_web_url(text)
    cited_title = extract_cited_title(text)
    web_info = fetch_web_metadata(url)

    if web_info["status"] == "NOT_FOUND":
        add_review_comment(doc, para, web_info["message"])
        highlight_whole_para(para)
        return False, 1, 1
    if web_info["status"] in ("BLOCKED", "ERROR"):
        add_review_comment(doc, para, f"Chưa kiểm tra tự động được ({web_info['message']}). Đề nghị mở đường dẫn để kiểm tra thủ công.")
        print(f"    [WEB] {web_info['status']}: {web_info['message']}")
        return False, 1, 0
    if web_info["status"] == "NON_HTML":
        print(f"    [WEB] Tài liệu không phải HTML ({web_info['content_type']}) → chỉ xác nhận đường dẫn tồn tại")
        return True, 0, 0

    text_wo_url = strip_urls_dois(text)
    text_key = norm_key(text_wo_url)
    label = site_label(web_info.get("final_url") or url)

    # 1. Tiêu đề
    candidates = web_info.get("title_candidates", [])
    if candidates and not title_extraction_failed(cited_title, text):
        sim = _best_title_sim(cited_title, candidates)
        if sim < WEB_TITLE_THRESHOLD:
            add_review_comment(doc, para, f"Tiêu đề bài viết không khớp với nội dung trên trang web. Tiêu đề trên web: \"{web_info['title']}\".")
            comments += 1
            print(f"    [WEB-TITLE] MISMATCH (sim={sim:.2f})")

    # 2. Tên trang web — so với phần chữ ĐÃ BỎ URL (bản cũ so cả URL nên kiểm tra này không bao giờ báo lỗi)
    site_name = web_info.get("site_name", "")
    words = text_key.split()
    site_words = [w for w in norm_key(site_name).split() if len(w) > 2]
    site_ok = (
        word_in(label, text_key)
        or (len(label) >= 4 and label in "".join(words))                      # ourworldindata ~ "Our World in Data"
        or (len(label) >= 2 and label in "".join(w[0] for w in words))        # who ~ "World Health Organization"
        or (site_name and word_in(site_name, text_key))
        or (site_words and sum(word_in(w, text_key) for w in site_words) * 3 >= len(site_words) * 2)
    )
    if not site_ok:
        shown = site_name or label
        add_review_comment(doc, para, f"Thiếu hoặc sai tên trang web. Tên trang web chính thức: \"{shown}\".")
        comments += 1
        print(f"    [WEB-SITE] MISMATCH ({shown})")

    # 3. Tác giả (bỏ qua khi "tác giả" thực ra là tên tờ báo/trang web)
    web_authors = [a for a in web_info.get("authors", [])
                   if a and norm_key(a) != norm_key(site_name) and not word_in(label, norm_key(a))]
    if web_authors:
        parsed_authors = parse_author_dicts(web_authors, source="web")
        author_result = check_authors(text_wo_url, parsed_authors)
        if author_result["status"] not in ("MATCH", "NO_REGISTRY_AUTHORS"):
            add_review_comment(doc, para, author_result["message"])
            comments += 1
            print(f"    [WEB-AUTHOR] {author_result['status']}")

    # 4. Năm
    web_year = web_info.get("year", "")
    cited_year = extract_cited_year(text)
    if web_year and cited_year and cited_year != web_year:
        add_review_comment(doc, para, f"Năm xuất bản không khớp với bài viết trên web (Năm trên web: {web_year}).")
        comments += 1
        print(f"    [WEB-YEAR] MISMATCH ({cited_year} vs {web_year})")

    return True, comments, 0
