"""Chạy kiểm tra ở luồng nền, ghi tiến độ và kết quả ra đĩa.

Nhờ vậy lượt kiểm tra KHÔNG phụ thuộc vào tab trình duyệt: mất kết nối, tải lại trang hay đóng tab
thì việc kiểm tra vẫn chạy tiếp; mở lại đường dẫn có mã lượt (?job=...) là thấy tiến độ và tải kết quả.
"""
import json
import sys
import shutil
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path

JOBS_DIR = Path(tempfile.gettempdir()) / "refcheck_jobs"
KEEP_SECONDS = 24 * 3600            # giữ kết quả 24 giờ
_RUN_LOCK = threading.Lock()        # mỗi lúc chỉ chạy 1 lượt (dùng chung cho mọi người truy cập)
_STATUS_LOCK = threading.Lock()


class _ThreadStdout:
    """Thay sys.stdout: chỉ ghi print() của luồng kiểm tra vào file nhật ký, các luồng khác in như cũ.
    (contextlib.redirect_stdout đổi stdout cho MỌI luồng, làm lẫn nhật ký.)"""
    def __init__(self, original):
        self.original = original
        self.targets = {}

    def write(self, text):
        return self.targets.get(threading.get_ident(), self.original).write(text)

    def flush(self):
        self.targets.get(threading.get_ident(), self.original).flush()

    def __getattr__(self, name):
        return getattr(self.original, name)


if not isinstance(sys.stdout, _ThreadStdout):
    sys.stdout = _ThreadStdout(sys.stdout)


def _job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def read_status(job_id: str):
    p = _job_dir(job_id) / "status.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _write_status(job_id: str, status: dict):
    with _STATUS_LOCK:
        status["updated"] = time.time()
        p = _job_dir(job_id) / "status.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)


def cleanup_old_jobs():
    if not JOBS_DIR.exists():
        return
    now = time.time()
    for d in JOBS_DIR.iterdir():
        try:
            if d.is_dir() and now - d.stat().st_mtime > KEEP_SECONDS:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def create_job(files: list) -> str:
    """files: [(tên file gốc, bytes)]. Lưu file, khởi động luồng nền, trả về mã lượt."""
    cleanup_old_jobs()
    job_id = uuid.uuid4().hex[:12]
    d = _job_dir(job_id)
    (d / "in").mkdir(parents=True)
    (d / "out").mkdir()
    entries = []
    for i, (name, data) in enumerate(files):
        in_path = d / "in" / f"{i:03d}.docx"
        in_path.write_bytes(data)
        entries.append({"name": name, "out_name": Path(name).stem + "_reviewed.docx",
                        "state": "waiting", "done": 0, "total": 0, "current": "", "summary": "", "error": ""})
    _write_status(job_id, {"state": "queued", "created": time.time(), "files": entries})
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    return job_id


def _run_job(job_id: str):
    from main import review_document          # import trong luồng để tránh vòng lặp import
    d = _job_dir(job_id)
    status = read_status(job_id)
    with _RUN_LOCK:
        status["state"] = "running"
        _write_status(job_id, status)
        for i, f in enumerate(status["files"]):
            f["state"] = "running"
            _write_status(job_id, status)
            log_path = d / "out" / f"{i:03d}.log"

            def on_progress(done, total, text, f=f):
                f["done"], f["total"], f["current"] = done, total, text[:80]
                _write_status(job_id, status)

            try:
                with open(log_path, "w", encoding="utf-8") as log:
                    sys.stdout.targets[threading.get_ident()] = log
                    try:
                        res = review_document(d / "in" / f"{i:03d}.docx", d / "out" / f["out_name"],
                                              on_progress=on_progress)
                    finally:
                        sys.stdout.targets.pop(threading.get_ident(), None)
                s = res["stats"]
                f["summary"] = (
                    f"{res['items']} mục · {res['comments']} comment · {res['highlights']} mục tô vàng — "
                    f"Web {s['WEB']['found']}/{s['WEB']['total']}, Sách {s['BOOK']['found']}/{s['BOOK']['total']}, "
                    f"Tạp chí {s['JOURNAL']['found']}/{s['JOURNAL']['total']} tìm thấy"
                    + (f", Văn bản pháp quy {s['LEGAL']['total']}" if s.get("LEGAL", {}).get("total") else ""))
                f["saved"] = Path(res["saved"]).name
                f["state"] = "done"
            except Exception as e:
                f["state"] = "error"
                f["error"] = f"{type(e).__name__}: {e}"
                with open(log_path, "a", encoding="utf-8") as log:
                    log.write("\n" + traceback.format_exc())
            _write_status(job_id, status)
        status["state"] = "done"
        _write_status(job_id, status)


def result_path(job_id: str, file_entry: dict) -> Path:
    return _job_dir(job_id) / "out" / file_entry.get("saved", file_entry["out_name"])


def log_text(job_id: str, index: int) -> str:
    p = _job_dir(job_id) / "out" / f"{index:03d}.log"
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


STALE_SECONDS = 15 * 60


def is_interrupted(status: dict) -> bool:
    """Lượt bị ngắt: chưa xong, không có luồng nào đang chạy, và đã lâu không cập nhật
    (thường do máy chủ khởi động lại giữa chừng)."""
    return (status["state"] in ("queued", "running") and not is_busy()
            and time.time() - status.get("updated", status["created"]) > STALE_SECONDS)


def is_busy() -> bool:
    return _RUN_LOCK.locked()
