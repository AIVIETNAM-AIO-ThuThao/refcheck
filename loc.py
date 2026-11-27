"""Tra cứu danh mục Thư viện Quốc hội Mỹ (Library of Congress) qua cổng SRU, trả bản ghi MODS.

Mỗi lần tái bản là một bản ghi riêng (tên sách, phụ đề, tác giả, lần xuất bản, NXB, năm, ISBN),
nên rất hợp để kiểm tra năm của ĐÚNG lần tái bản mà trích dẫn ghi.
"""
import re
import time
import xml.etree.ElementTree as ET
from functools import lru_cache

import requests

from utils import BROWSER_HEADERS, extract_year_from_str, parse_author_dicts

LOC_SRU = "https://lx2.loc.gov/sru/lcdb"
LOC_DELAY = 1.0            # giây giữa 2 lần truy vấn
NON_AUTHOR_ROLES = ("publisher", "printer", "distributor", "issuing body", "sponsor", "host institution")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _children(el, name):
    return [c for c in el if _local(c.tag) == name]


def _text(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip() if el is not None else ""


def _clean_name(s: str) -> str:
    s = re.sub(r"\([^)]*\)", " ", s)                     # (Stuart Jonathan)
    s = re.sub(r",?\s*\d{4}-(\d{4})?\.?$", "", s.strip())  # năm sinh-mất
    return re.sub(r"\s+", " ", s).strip(" ,.;:")


def _roles(name_el) -> list:
    return [_text(rt).lower().strip(" .") for r in _children(name_el, "role") for rt in _children(r, "roleTerm")]


def parse_mods(xml_text: str) -> list:
    root = ET.fromstring(xml_text)
    records = []
    for mods in root.iter():
        if _local(mods.tag) != "mods":
            continue
        # tên sách: titleInfo trực tiếp của bản ghi (bỏ tên tùng thư trong relatedItem), ưu tiên titleInfo không có 'type'
        tinfos = _children(mods, "titleInfo")
        tinfos.sort(key=lambda t: 0 if not t.get("type") else 1)
        title = subtitle = ""
        if tinfos:
            t = tinfos[0]
            title = " ".join(x for x in (_text(next(iter(_children(t, "nonSort")), None)),
                                         _text(next(iter(_children(t, "title")), None))) if x).strip(" /:;.")
            subtitle = _text(next(iter(_children(t, "subTitle")), None)).strip(" /:;.")

        authors = []
        for n in _children(mods, "name"):
            roles = _roles(n)
            if any(any(nr in r for nr in NON_AUTHOR_ROLES) for r in roles):
                continue
            if n.get("type") not in (None, "personal"):
                continue
            parts = [p for p in _children(n, "namePart") if p.get("type") not in ("date", "termsOfAddress")]
            name = _clean_name(" ".join(_text(p) for p in parts))
            if name:
                authors.append(name)

        edition = publisher = year = ""
        for oi in _children(mods, "originInfo"):
            edition = edition or _text(next(iter(_children(oi, "edition")), None)).strip(" .")
            publisher = publisher or _text(next(iter(_children(oi, "publisher")), None))
            if not publisher:                                   # MODS mới: <agent><namePart>Pearson</namePart>
                for ag in _children(oi, "agent"):
                    publisher = _clean_name(_text(next(iter(_children(ag, "namePart")), None)))
                    if publisher:
                        break
            for d in _children(oi, "dateIssued"):
                year = year or extract_year_from_str(_text(d))
        if not publisher:                                       # NXB ghi ở <name> có vai trò publisher
            for n in _children(mods, "name"):
                if any("publisher" in r for r in _roles(n)):
                    publisher = _clean_name(_text(next(iter(_children(n, "namePart")), None)))
                    break
        publisher = _clean_name(publisher)

        isbns = [re.sub(r"[^0-9Xx]", "", _text(i).split()[0]) for i in _children(mods, "identifier")
                 if i.get("type") == "isbn" and _text(i)]
        if title:
            records.append({"title": title, "subtitle": subtitle, "authors": authors, "edition": edition,
                            "publisher": publisher, "year": year, "isbns": isbns})
    return records


def _cql_quote(s: str) -> str:
    return '"' + re.sub(r'["\\]', " ", s).strip() + '"'


@lru_cache(maxsize=512)
def _query(cql: str) -> tuple:
    time.sleep(LOC_DELAY)
    r = requests.get(LOC_SRU, params={"version": "1.1", "operation": "searchRetrieve", "query": cql,
                                      "maximumRecords": "10", "recordSchema": "mods"},
                     headers=BROWSER_HEADERS, timeout=30)
    r.raise_for_status()
    return tuple(tuple(sorted((k, tuple(v) if isinstance(v, list) else v) for k, v in rec.items()))
                 for rec in parse_mods(r.text))


def search(cited_title: str, first_author_family: str = "", isbn: str = ""):
    """Trả về (danh sách bản ghi, ok). ok=False khi không truy cập được LoC."""
    queries = []
    if isbn:
        queries.append(f"bath.isbn={isbn}")
    if cited_title:
        main_title = re.split(r"\s*:\s*", cited_title)[0]       # tên chính, bỏ phụ đề cho dễ khớp
        q = f"dc.title={_cql_quote(main_title)}"
        if first_author_family:
            q += f" and dc.creator={_cql_quote(first_author_family)}"
        queries.append(q)
    try:
        for q in queries:
            recs = [{k: (list(v) if isinstance(v, tuple) else v) for k, v in rec} for rec in _query(q)]
            if recs:
                return recs, True
        return [], True
    except Exception as e:
        print(f"    [LOC] Lỗi truy cập Library of Congress: {type(e).__name__}: {e}")
        return [], False


def to_candidates(records: list, edition_num_fn=None, cited_edition: str = "") -> list:
    """Đổi bản ghi LoC sang khuôn ứng viên của book.py."""
    cands = []
    for r in records:
        full_title = f"{r['title']}: {r['subtitle']}" if r["subtitle"] else r["title"]
        ed_num = edition_num_fn(r["edition"]) if (edition_num_fn and r["edition"]) else ""
        cands.append({
            "status": "FOUND", "source": "loc", "similarity": 1.0,
            "title": r["title"], "full_title": full_title,
            "authors": parse_author_dicts(r["authors"], source="book_api"),
            "year": r["year"], "publisher": r["publisher"], "description": "",
            "isbn": (r["isbns"] or [""])[0], "edition": r["edition"],
            # trích dẫn có ghi lần tái bản và bản ghi LoC đúng lần đó -> năm phải khớp chặt
            "exact_edition_matched": bool(cited_edition and ed_num == cited_edition),
        })
    return cands
