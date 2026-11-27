"""Web app kiểm tra danh mục Tài liệu tham khảo — chạy: streamlit run app.py

Việc kiểm tra chạy ở LUỒNG NỀN (jobs.py), tiến độ và kết quả ghi ra đĩa theo mã lượt (?job=...).
Mất kết nối, tải lại trang hay đóng tab đều không làm mất lượt đang chạy:
mở lại đường dẫn có ?job=... để xem tiến độ và tải kết quả.
"""
import io
import zipfile

import streamlit as st

import jobs

st.set_page_config(page_title="Kiểm tra Tài liệu tham khảo", page_icon="📚", layout="centered")
st.title("📚 Kiểm tra Tài liệu tham khảo")

job_id = st.query_params.get("job")


# ---------------- MÀN HÌNH 1: CHỌN FILE ----------------
def show_upload_screen():
    st.write(
        "Tải lên một hoặc nhiều file Word (.docx) **chỉ chứa Danh mục Tài liệu tham khảo**. "
        "Chương trình đối chiếu với Crossref, DataCite, OpenAlex, Google Books, Open Library, "
        "Library of Congress, Cục Xuất bản, Thư viện Quốc gia và trang web gốc, rồi trả về file có "
        "**comment** (BTV) và **tô vàng** các mục có vấn đề."
    )
    files = st.file_uploader("Chọn file .docx (có thể chọn nhiều file)", type=["docx"],
                             accept_multiple_files=True)
    if jobs.is_busy():
        st.info("Đang có một lượt kiểm tra khác chạy. Lượt của bạn sẽ được xếp hàng và tự chạy sau.")
    if st.button("Bắt đầu kiểm tra", type="primary", disabled=not files):
        new_id = jobs.create_job([(f.name, f.getvalue()) for f in files])
        st.query_params["job"] = new_id
        st.rerun()


# ---------------- MÀN HÌNH 2: TIẾN ĐỘ & KẾT QUẢ ----------------
def render_job(status):
    if status is None:
        st.error("Không tìm thấy lượt kiểm tra này (có thể máy chủ đã khởi động lại hoặc kết quả đã quá 24 giờ). "
                 "Vui lòng tải file lên lại.")
        return
    files = status["files"]
    interrupted = jobs.is_interrupted(status)

    if status["state"] == "queued":
        st.info("⏳ Đang xếp hàng chờ lượt kiểm tra khác chạy xong...")
    elif status["state"] == "running" and not interrupted:
        st.info("⏳ Đang kiểm tra. Bạn có thể đóng tab này; mở lại **đường dẫn hiện tại** để xem tiến độ.")

    for i, f in enumerate(files):
        st.markdown(f"**{i + 1}/{len(files)}. {f['name']}**")
        if f["state"] == "waiting":
            st.caption("Chờ đến lượt...")
        elif f["state"] == "running":
            total = max(f["total"], 1)
            st.progress(f["done"] / total, text=f"Mục {f['done']}/{f['total']}: {f['current']}...")
        elif f["state"] == "done":
            path = jobs.result_path(job_id, f)
            if path.exists():
                st.download_button(f"⬇️ {f['out_name']}", path.read_bytes(), file_name=f["out_name"],
                                   key=f"dl_{i}",
                                   mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            st.caption(f["summary"])
        else:
            st.error(f"Không xử lý được file này: {f['error']}")
        log = jobs.log_text(job_id, i)
        if log and f["state"] in ("done", "error"):
            with st.expander("Nhật ký chi tiết"):
                st.code(log[-20000:], language=None)

    if interrupted:
        st.error("Lượt kiểm tra đã bị ngắt (máy chủ khởi động lại). Vui lòng chạy lại.")

    done_files = [f for f in files if f["state"] == "done" and jobs.result_path(job_id, f).exists()]
    if status["state"] == "done" and len(done_files) > 1:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in done_files:
                z.writestr(f["out_name"], jobs.result_path(job_id, f).read_bytes())
        st.download_button("⬇️ Tải tất cả (.zip)", buf.getvalue(), file_name="ket_qua_kiem_tra_TLTK.zip",
                           mime="application/zip", key="dl_zip")
    return interrupted


@st.fragment(run_every=3)
def live_progress():
    """Tự làm mới mỗi 3 giây khi lượt đang chạy; chạy xong thì vẽ lại cả trang một lần rồi dừng làm mới."""
    status = jobs.read_status(job_id)
    if status is None or status["state"] == "done":
        st.rerun(scope="app")
    if render_job(status):
        st.rerun(scope="app")


if job_id:
    status = jobs.read_status(job_id)
    finished = status is None or status["state"] == "done" or jobs.is_interrupted(status)
    if finished:
        render_job(status)
        if status is not None and status["state"] == "done":
            st.success("Hoàn tất.")
        if st.button("Kiểm tra file khác"):
            del st.query_params["job"]
            st.rerun()
    else:
        live_progress()
else:
    show_upload_screen()

st.caption("Lưu ý: Mục được tô vàng là mục có vấn đề: không tìm thấy, sai hoặc thiếu thông tin.")
