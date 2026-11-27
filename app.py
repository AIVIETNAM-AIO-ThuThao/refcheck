"""Web app kiểm tra danh mục Tài liệu tham khảo — chạy: streamlit run app.py"""
import contextlib
import io
import tempfile
import threading
import zipfile
from pathlib import Path

import streamlit as st

from main import review_document

st.set_page_config(page_title="Kiểm tra Tài liệu tham khảo", page_icon="📚", layout="centered")


@st.cache_resource
def job_lock() -> threading.Lock:
    """Chỉ cho 1 lượt kiểm tra chạy tại một thời điểm (dùng chung cho mọi người truy cập):
    tránh vượt giới hạn truy cập của Crossref/Google Books và tránh trộn lẫn bộ đếm giữa các lượt."""
    return threading.Lock()


st.title("📚 Kiểm tra Tài liệu tham khảo")
st.write(
    "Tải lên một hoặc nhiều file Word (.docx) **chỉ chứa Danh mục Tài liệu tham khảo**. "
    "Chương trình đối chiếu với Crossref, DataCite, Google Books, Open Library, OpenAlex "
    "và trang web gốc, rồi trả về file có **comment** (BTV)."
)

files = st.file_uploader("Chọn file .docx (có thể chọn nhiều file)", type=["docx"], accept_multiple_files=True)

if "results" not in st.session_state:
    st.session_state.results = []          # [(tên file kết quả, bytes, tóm tắt)]

if st.button("Bắt đầu kiểm tra", type="primary", disabled=not files):
    st.session_state.results = []
    lock = job_lock()
    if not lock.acquire(blocking=False):
        with st.spinner("Đang có người khác chạy kiểm tra, vui lòng chờ..."):
            lock.acquire()
    try:
        for i, up in enumerate(files, start=1):
            st.subheader(f"{i}/{len(files)}. {up.name}")
            bar = st.progress(0.0, text="Đang đọc file...")

            def on_progress(done, total, text, bar=bar):
                bar.progress(done / max(total, 1), text=f"Mục {done}/{total}: {text[:70]}...")

            log = io.StringIO()
            with tempfile.TemporaryDirectory() as tmp:
                in_path = Path(tmp) / "input.docx"
                in_path.write_bytes(up.getvalue())
                out_name = Path(up.name).stem + "_reviewed.docx"
                out_path = Path(tmp) / out_name
                try:
                    with contextlib.redirect_stdout(log):
                        res = review_document(in_path, out_path, on_progress=on_progress)
                    data = Path(res["saved"]).read_bytes()
                    s = res["stats"]
                    summary = (
                        f"{res['items']} mục · {res['comments']} comment · {res['highlights']} mục tô vàng — "
                        f"Web {s['WEB']['found']}/{s['WEB']['total']}, "
                        f"Sách {s['BOOK']['found']}/{s['BOOK']['total']}, "
                        f"Tạp chí {s['JOURNAL']['found']}/{s['JOURNAL']['total']} tìm thấy"
                    )
                    st.session_state.results.append((out_name, data, summary))
                    bar.progress(1.0, text="Xong")
                    st.success(summary)
                except Exception as e:
                    bar.empty()
                    st.error(f"Không xử lý được file này: {type(e).__name__}: {e}")
            with st.expander("Nhật ký chi tiết"):
                st.code(log.getvalue() or "(trống)", language=None)
    finally:
        lock.release()

if st.session_state.results:
    st.divider()
    st.subheader("Tải kết quả")
    for name, data, summary in st.session_state.results:
        st.download_button(f"⬇️ {name}", data, file_name=name, key=f"dl_{name}",
                           mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        st.caption(summary)
    if len(st.session_state.results) > 1:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data, _ in st.session_state.results:
                z.writestr(name, data)
        st.download_button("⬇️ Tải tất cả (.zip)", buf.getvalue(), file_name="ket_qua_kiem_tra_TLTK.zip",
                           mime="application/zip", key="dl_zip")

st.caption("Lưu ý: Mục được tô vàng là mục có vấn đề: không tìm thấy, sai hoặc thiếu thông tin. "
           )
try:
    import resource
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    st.sidebar.caption(f"RAM cao nhất đã dùng: {peak_mb:.0f} MB / 1024 MB")
except ImportError:
    pass