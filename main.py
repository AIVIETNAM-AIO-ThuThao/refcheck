import traceback
from datetime import datetime
from pathlib import Path

from docx import Document

from utils import (
    EMAIL,
    INPUT_FILE,
    OUTPUT_FILE,
    add_review_comment,
    classify_reference,
    is_section_header,
    strip_list_marker,
)
import web
import book
import journal


def _process_one(handler, doc, para, text):
    """Chạy 1 mục; lỗi ở 1 mục không làm hỏng cả lần chạy."""
    try:
        return handler(doc, para, text)
    except Exception as e:
        print(f"    [LỖI] {type(e).__name__}: {e}")
        traceback.print_exc(limit=2)
        try:
            add_review_comment(doc, para, f"Chưa kiểm tra tự động được (lỗi chương trình: {type(e).__name__}). Đề nghị kiểm tra thủ công.")
            return False, 1, 0
        except Exception:
            return False, 0, 0


def _save(doc, out_path):
    try:
        doc.save(str(out_path))
        return out_path
    except PermissionError:
        alt = out_path.with_name(f"{out_path.stem}_{datetime.now():%Y%m%d_%H%M%S}{out_path.suffix}")
        print(f"  File {out_path.name} đang mở trong Word → lưu sang {alt.name}")
        doc.save(str(alt))
        return alt


def review_document(in_path, out_path, on_progress=None) -> dict:
    """Kiểm tra 1 file danh mục TLTK, lưu bản có comment vào out_path, trả về số liệu tổng kết.
    on_progress(done, total, text): hàm tùy chọn để báo tiến độ (web app dùng để vẽ thanh tiến độ)."""
    in_path, out_path = Path(in_path), Path(out_path)
    print(f"Đang đọc: {in_path.name}")
    doc = Document(str(in_path))

    # File chỉ chứa danh mục TLTK: mỗi đoạn (trừ tiêu đề và dòng trống) là một tài liệu
    header_para = None
    queues = {"WEB": [], "BOOK": [], "JOURNAL": []}
    for idx, para in enumerate(doc.paragraphs, start=1):
        if is_section_header(para.text):
            header_para = header_para or para
            continue
        text = strip_list_marker(para.text)
        if len(text) < 20:
            continue
        queues[classify_reference(text)].append((idx, para, text))

    flows = [
        ("WEB", "INTERNET (web.py)", web.process_web_paragraph),
        ("BOOK", "SÁCH (book.py)", book.process_book_paragraph),
        ("JOURNAL", "TẠP CHÍ (journal.py)", journal.process_journal_paragraph),
    ]
    stats = {k: {"found": 0, "clean": 0, "total": len(queues[k])} for k in queues}
    total_items = sum(len(q) for q in queues.values())
    done = 0
    total_commented = 0
    total_highlighted = 0

    for step, (key, label, handler) in enumerate(flows, start=1):
        print(f"\n--- [{step}/3] CHẠY LUỒNG {label}: {len(queues[key])} mục ---")
        for idx, para, text in queues[key]:
            print(f"  (Dòng {idx}) [{key}] {text[:60]}...")
            ok, c_add, h_add = _process_one(handler, doc, para, text)
            if ok:
                stats[key]["found"] += 1
                if c_add == 0:
                    stats[key]["clean"] += 1
            total_commented += c_add
            total_highlighted += h_add
            done += 1
            if on_progress:
                on_progress(done, total_items, text)

    if total_highlighted > 0 and header_para is not None:
        add_review_comment(doc, header_para,
                           "Không tìm thấy các tài liệu được tô vàng. Đề nghị sửa. "
                           "Nếu xóa thì xóa cả ở đây và trong nội dung.")
        total_commented += 1

    saved = _save(doc, out_path)
    print("\n================ HOÀN TẤT ================")
    print(f"File xuất: {saved}")
    names = {"WEB": "Internet (web.py)   ", "BOOK": "Sách (book.py)      ", "JOURNAL": "Tạp chí (journal.py)"}
    for key in ("WEB", "BOOK", "JOURNAL"):
        print(f"  - {names[key]}: tìm thấy {stats[key]['found']}/{len(queues[key])}, không có sai sót {stats[key]['clean']}")
    print(f"  - Tổng comment đã gắn   : {total_commented}")
    print(f"  - Tổng đoạn bị tô vàng  : {total_highlighted}")
    return {"saved": saved, "stats": stats, "items": total_items,
            "comments": total_commented, "highlights": total_highlighted}

def main():
    if not INPUT_FILE.exists():
        print(f"LỖI: Không tìm thấy file {INPUT_FILE}")
        return
    if "example.com" in EMAIL:
        print("  LƯU Ý: EMAIL trong utils.py vẫn là địa chỉ mẫu — nên đổi sang email thật.")
    review_document(INPUT_FILE, OUTPUT_FILE)
    
if __name__ == "__main__":
    main()
