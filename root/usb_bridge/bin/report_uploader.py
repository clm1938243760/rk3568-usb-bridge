#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
import urllib.request
import urllib.error

BASE_DIR = "/root/usb_bridge"
LOG_FILE = BASE_DIR + "/logs/report_uploader.log"

REPORT_PRINT_QUEUE_DIR = BASE_DIR + "/report_print_queue"
REPORT_PDF_QUEUE_DIR = BASE_DIR + "/report_pdf_queue"
REPORT_UPLOADED_DIR = BASE_DIR + "/report_uploaded"
REPORT_ERROR_DIR = BASE_DIR + "/report_error"

GS_BIN = os.environ.get("GS_BIN", "gs")
UPLOAD_URL = os.environ.get("REPORT_UPLOAD_URL", "")
UPLOAD_TIMEOUT = int(os.environ.get("REPORT_UPLOAD_TIMEOUT", "30"))
UPLOAD_FIELD_NAME = os.environ.get("REPORT_UPLOAD_FIELD_NAME", "file")


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def ensure_dirs():
    for path in [
        os.path.dirname(LOG_FILE),
        REPORT_PRINT_QUEUE_DIR,
        REPORT_PDF_QUEUE_DIR,
        REPORT_UPLOADED_DIR,
        REPORT_ERROR_DIR,
    ]:
        os.makedirs(path, exist_ok=True)


def log(msg):
    ensure_dirs()
    line = "[%s] %s" % (now(), msg)
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def next_print_task():
    files = sorted([f for f in os.listdir(REPORT_PRINT_QUEUE_DIR) if f.endswith(".json")])
    if not files:
        return None, None

    name = files[0]
    src = os.path.join(REPORT_PRINT_QUEUE_DIR, name)
    work = os.path.join(REPORT_PRINT_QUEUE_DIR, name + ".work")
    os.rename(src, work)

    with open(work, "r") as f:
        task = json.load(f)

    return work, task


def convert_ps_to_pdf(task):
    ps_file = task.get("ps_file", "")
    if not ps_file or not os.path.exists(ps_file):
        raise RuntimeError("ps file not found: %s" % ps_file)

    base_name = task.get("base_name") or os.path.splitext(os.path.basename(ps_file))[0]
    pdf_tmp = os.path.join(REPORT_PDF_QUEUE_DIR, base_name + ".pdf.tmp")
    pdf_path = os.path.join(REPORT_PDF_QUEUE_DIR, base_name + ".pdf")

    cmd = [
        GS_BIN,
        "-dBATCH",
        "-dNOPAUSE",
        "-dSAFER",
        "-sDEVICE=pdfwrite",
        "-sOutputFile=%s" % pdf_tmp,
        ps_file,
    ]
    log("convert ps to pdf: %s" % " ".join(cmd))
    subprocess.check_call(cmd)
    os.rename(pdf_tmp, pdf_path)
    log("pdf created: %s" % pdf_path)
    return pdf_path


def upload_pdf(pdf_path, task):
    if not UPLOAD_URL:
        raise RuntimeError("REPORT_UPLOAD_URL is not set")

    with open(pdf_path, "rb") as f:
        pdf_data = f.read()

    boundary = "----RK3568USBBridge%s" % uuid.uuid4().hex
    filename = os.path.basename(pdf_path)

    fields = {
        "patient_id": str(task.get("patient_id", "")),
        "report_no": str(task.get("report_no", "")),
        "his_exam_no": str(task.get("his_exam_no", "")),
        "scan_text": str(task.get("scan_text", "")),
    }

    body = bytearray()

    for key, value in fields.items():
        body.extend(("--%s\r\n" % boundary).encode("utf-8"))
        body.extend(('Content-Disposition: form-data; name="%s"\r\n\r\n' % key).encode("utf-8"))
        body.extend(value.encode("utf-8"))
        body.extend(b"\r\n")

    body.extend(("--%s\r\n" % boundary).encode("utf-8"))
    file_header = (
        'Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ) % (UPLOAD_FIELD_NAME, filename)
    body.extend(file_header.encode("utf-8"))
    body.extend(pdf_data)
    body.extend(b"\r\n")
    body.extend(("--%s--\r\n" % boundary).encode("utf-8"))

    req = urllib.request.Request(
        UPLOAD_URL,
        data=bytes(body),
        headers={
            "Content-Type": "multipart/form-data; boundary=%s" % boundary,
            "Content-Length": str(len(body)),
            "X-Patient-Id": str(task.get("patient_id", "")),
            "X-Report-No": str(task.get("report_no", "")),
            "X-His-Exam-No": str(task.get("his_exam_no", "")),
            "User-Agent": "RK3568-USB-Bridge",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=UPLOAD_TIMEOUT) as resp:
            body_text = resp.read().decode("utf-8", errors="ignore")
            log("upload response status=%s body=%s" % (resp.status, body_text[:500]))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError("upload http error status=%s body=%s" % (e.code, body_text[:500]))


def move_to_uploaded(path):
    os.makedirs(REPORT_UPLOADED_DIR, exist_ok=True)
    shutil.move(path, os.path.join(REPORT_UPLOADED_DIR, os.path.basename(path).replace(".work", "")))


def move_to_error(path):
    if path and os.path.exists(path):
        os.makedirs(REPORT_ERROR_DIR, exist_ok=True)
        shutil.move(path, os.path.join(REPORT_ERROR_DIR, os.path.basename(path).replace(".work", "")))


def main():
    ensure_dirs()
    log("report_uploader start upload_url=%s" % (UPLOAD_URL or "<not set>"))

    while True:
        work, task = next_print_task()
        if not task:
            time.sleep(0.2)
            continue

        try:
            pdf_path = convert_ps_to_pdf(task)
            upload_pdf(pdf_path, task)
            move_to_uploaded(work)
            ps_file = task.get("ps_file", "")
            if ps_file and os.path.exists(ps_file):
                move_to_uploaded(ps_file)
            move_to_uploaded(pdf_path)
        except Exception as e:
            log("FATAL report upload failed, exit for supervisor restart: %s" % e)
            move_to_error(work)
            sys.exit(1)


if __name__ == "__main__":
    main()
