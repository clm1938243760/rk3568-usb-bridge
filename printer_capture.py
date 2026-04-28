#!/usr/bin/env python3
import json
import os
import select
import shutil
import sys
import time

BASE_DIR = "/root/usb_bridge"
LOG_FILE = BASE_DIR + "/logs/printer_capture.log"
PRINTER_DEV = os.environ.get("PRINTER_DEV", "/dev/g_printer0")

REPORT_WAIT_DIR = BASE_DIR + "/report_wait_queue"
REPORT_PRINT_QUEUE_DIR = BASE_DIR + "/report_print_queue"
REPORT_WAIT_DONE_DIR = BASE_DIR + "/report_wait_done"
REPORT_ERROR_DIR = BASE_DIR + "/report_error"

CHUNK_SIZE = 64 * 1024
IDLE_TIMEOUT = float(os.environ.get("PRINT_IDLE_TIMEOUT", "2.0"))
MIN_JOB_BYTES = int(os.environ.get("PRINT_MIN_JOB_BYTES", "128"))


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def ensure_dirs():
    for path in [
        os.path.dirname(LOG_FILE),
        REPORT_WAIT_DIR,
        REPORT_PRINT_QUEUE_DIR,
        REPORT_WAIT_DONE_DIR,
        REPORT_ERROR_DIR,
    ]:
        os.makedirs(path, exist_ok=True)


def log(msg):
    ensure_dirs()
    line = "[%s] %s" % (now(), msg)
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def wait_dev(path):
    while not os.path.exists(path):
        log("waiting for printer device %s ..." % path)
        time.sleep(1)


def next_wait_task():
    files = sorted([f for f in os.listdir(REPORT_WAIT_DIR) if f.endswith(".json")])
    if not files:
        return None, None

    name = files[0]
    src = os.path.join(REPORT_WAIT_DIR, name)
    work = os.path.join(REPORT_WAIT_DIR, name + ".work")
    os.rename(src, work)

    with open(work, "r") as f:
        task = json.load(f)

    return work, task


def safe_name(value, fallback):
    text = str(value or "").strip()
    keep = []
    for ch in text:
        if ch.isalnum() or ch in ["-", "_", "."]:
            keep.append(ch)
        else:
            keep.append("_")
    text = "".join(keep).strip("._")
    return text or fallback


def read_print_job(dev):
    wait_dev(dev)
    log("waiting print job on %s" % dev)

    chunks = []
    total = 0
    last_data = None

    fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 0.1)

            if ready:
                try:
                    data = os.read(fd, CHUNK_SIZE)
                except BlockingIOError:
                    data = b""

                if data:
                    chunks.append(data)
                    total += len(data)
                    last_data = time.time()
                    continue

            if total > 0 and last_data and time.time() - last_data >= IDLE_TIMEOUT:
                break
    finally:
        os.close(fd)

    if total < MIN_JOB_BYTES:
        raise RuntimeError("print job too small: %d bytes" % total)

    log("print job received: %d bytes" % total)
    return b"".join(chunks)


def write_print_queue(job_data, wait_task):
    patient = wait_task.get("patient", {})
    patient_id = safe_name(wait_task.get("patient_id") or patient.get("patient_id"), "unknown")
    report_no = safe_name(wait_task.get("report_no") or patient.get("report_no"), patient_id)
    ts = int(time.time() * 1000)
    base_name = "%s_%s_%d" % (patient_id, report_no, ts)

    ps_tmp = os.path.join(REPORT_PRINT_QUEUE_DIR, base_name + ".ps.tmp")
    ps_path = os.path.join(REPORT_PRINT_QUEUE_DIR, base_name + ".ps")
    meta_path = os.path.join(REPORT_PRINT_QUEUE_DIR, base_name + ".json")

    with open(ps_tmp, "wb") as f:
        f.write(job_data)
    os.rename(ps_tmp, ps_path)

    meta = dict(wait_task)
    meta.update({
        "status": "print_received",
        "print_time": now(),
        "ps_file": ps_path,
        "base_name": base_name,
        "bytes": len(job_data)
    })

    tmp_meta = meta_path + ".tmp"
    with open(tmp_meta, "w") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.rename(tmp_meta, meta_path)

    log("report print task saved: %s" % meta_path)


def move_wait_task_done(wait_work):
    os.makedirs(REPORT_WAIT_DONE_DIR, exist_ok=True)
    shutil.move(wait_work, os.path.join(REPORT_WAIT_DONE_DIR, os.path.basename(wait_work).replace(".work", "")))


def move_wait_task_error(wait_work):
    if wait_work and os.path.exists(wait_work):
        os.makedirs(REPORT_ERROR_DIR, exist_ok=True)
        shutil.move(wait_work, os.path.join(REPORT_ERROR_DIR, os.path.basename(wait_work).replace(".work", "")))


def main():
    ensure_dirs()
    log("printer_capture start printer=%s" % PRINTER_DEV)

    while True:
        wait_work, wait_task = next_wait_task()
        if not wait_task:
            time.sleep(0.2)
            continue

        try:
            log("matched waiting report patient_id=%s report_no=%s" % (
                wait_task.get("patient_id", ""),
                wait_task.get("report_no", "")
            ))
            job_data = read_print_job(PRINTER_DEV)
            write_print_queue(job_data, wait_task)
            move_wait_task_done(wait_work)
        except Exception as e:
            log("FATAL printer capture failed, exit for supervisor restart: %s" % e)
            move_wait_task_error(wait_work)
            sys.exit(1)


if __name__ == "__main__":
    main()
