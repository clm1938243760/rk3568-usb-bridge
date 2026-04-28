#!/usr/bin/env python3
import os
import re
import time
import json
import base64
import struct
import urllib.parse
import urllib.request
import sys

DEVICE_NAME = "USBKey Chip USBKey Module"
DEVICES_FILE = "/proc/bus/input/devices"

API_URL = "http://192.168.112.139:9061/api/client/getTJPatientInfo"
API_PARAM = "inputText"

BASE_DIR = "/root/usb_bridge"
TEMPLATE_FILE = BASE_DIR + "/config/MarkInfo_SearchTitle_Config_100.json"
FORM_QUEUE_DIR = BASE_DIR + "/form_queue"
LOG_FILE = BASE_DIR + "/logs/scan_patient_capture.log"
SCAN_LOG = "/root/scan.log"
API_RAW_DIR = BASE_DIR + "/api_raw"
STATE_DIR = BASE_DIR + "/state"
CURRENT_PATIENT_FILE = STATE_DIR + "/current_patient.json"
REPORT_WAIT_DIR = BASE_DIR + "/report_wait_queue"

EV_KEY = 0x01
KEY_DOWN = 1
KEY_UP = 0

KEY_ENTER = 28
KEY_KPENTER = 96
KEY_CAPSLOCK = 58
KEY_LEFTSHIFT = 42
KEY_RIGHTSHIFT = 54

ENTER_CODES = {KEY_ENTER, KEY_KPENTER}
SHIFT_CODES = {KEY_LEFTSHIFT, KEY_RIGHTSHIFT}
IGNORE_CODES = {KEY_CAPSLOCK}

KEY_MAP = {
    2: "1", 3: "2", 4: "3", 5: "4", 6: "5",
    7: "6", 8: "7", 9: "8", 10: "9", 11: "0",

    79: "1", 80: "2", 81: "3",
    75: "4", 76: "5", 77: "6",
    71: "7", 72: "8", 73: "9",
    82: "0",

    16: "q", 17: "w", 18: "e", 19: "r", 20: "t",
    21: "y", 22: "u", 23: "i", 24: "o", 25: "p",
    30: "a", 31: "s", 32: "d", 33: "f", 34: "g",
    35: "h", 36: "j", 37: "k", 38: "l",
    44: "z", 45: "x", 46: "c", 47: "v", 48: "b",
    49: "n", 50: "m",

    12: "-", 13: "=",
    26: "[", 27: "]",
    39: ";", 40: "'",
    41: "`", 43: "\\",
    51: ",", 52: ".", 53: "/",
    57: " ",
}

SHIFT_MAP = {
    "1": "!", "2": "@", "3": "#", "4": "$", "5": "%",
    "6": "^", "7": "&", "8": "*", "9": "(", "0": ")",
    "-": "_", "=": "+",
    "[": "{", "]": "}",
    ";": ":", "'": "\"",
    "`": "~", "\\": "|",
    ",": "<", ".": ">", "/": "?",
}

FMT = "llHHI"
EVENT_SIZE = struct.calcsize(FMT)

def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")

def ensure_dirs():
    os.makedirs(FORM_QUEUE_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    os.makedirs(API_RAW_DIR, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(REPORT_WAIT_DIR, exist_ok=True)

def log(msg):
    ensure_dirs()
    line = "[%s] %s" % (now(), msg)
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def find_event_dev():
    try:
        with open(DEVICES_FILE, "r") as f:
            content = f.read()
    except Exception as e:
        log("read devices file failed: %s" % e)
        return None

    blocks = content.strip().split("\n\n")

    for block in blocks:
        if ('N: Name="%s"' % DEVICE_NAME) not in block:
            continue

        m = re.search(r"Handlers=.*?(event\d+)", block)
        if m:
            return "/dev/input/" + m.group(1)

    return None

def wait_scanner():
    while True:
        dev = find_event_dev()
        if dev and os.path.exists(dev):
            log("scanner found: %s" % dev)
            return dev

        log("scanner not found, retry in 2s")
        time.sleep(2)

def save_scan_log(text):
    with open(SCAN_LOG, "a") as f:
        f.write("[%s] %s\n" % (now(), text))

def build_sql(scan_text):
    raw = str(scan_text).strip().replace("'", "''")
    kw = raw.upper()

    sql = """
select
  t.exam_item,
  t.his_exam_no,
  z.report_no,
  t.patient_id,
  t.patient_name,
  q.name_phonetic,
  substr(t.patient_name, 0, 2) as xing,
  substr(t.patient_name, 2, 8) as ming,
  t.sex,
  t.age,
  to_char(t.birthday,'yyyy') as nian,
  to_char(t.birthday,'mm') as yue,
  to_char(t.birthday,'dd') as ri,
  t.birthday
from exam_master t
left join exam_item z on t.his_exam_no=z.his_exam_no
left join patient_info q on t.patient_id=q.patient_id
where
  (
    upper(z.report_no) like '%{kw}%'
    or upper(t.patient_id) like '%{kw}%'
    or upper(t.patient_name) like '%{kw}%'
    or upper(t.his_exam_no) like '%{kw}%'
  )
order by t.req_date desc
limit 5
""".format(kw=kw)

    return sql.strip()

def http_get_patient(scan_text):
    sql = build_sql(scan_text)

    # 关键：接口要求 sqlStr 是 base64 编码后的 SQL
    sql_b64 = base64.b64encode(sql.encode("utf-8")).decode("ascii")

    payload_obj = {
        "sqlStr": sql_b64
    }

    payload = json.dumps(payload_obj, ensure_ascii=False).encode("utf-8")

    log("request api POST json: %s scan=%s" % (API_URL, scan_text))
    log("sql raw first chars: %s" % sql[:80])
    log("sql base64 first chars: %s" % sql_b64[:80])

    req = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json",
            "User-Agent": "RK3568-USB-Bridge"
        },
        method="POST"
    )

    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read().decode("utf-8", errors="ignore")

    ts = int(time.time() * 1000)
    raw_path = "%s/api_%d.json" % (API_RAW_DIR, ts)

    with open(raw_path, "w") as f:
        f.write(body)

    log("api raw saved: %s" % raw_path)

    return json.loads(body)

def pick_first_record(api_json):
    if isinstance(api_json, list):
        return api_json[0] if api_json else None

    if isinstance(api_json, dict):
        for key in ["data", "result", "rows", "list"]:
            value = api_json.get(key)

            if isinstance(value, list):
                return value[0] if value else None

            if isinstance(value, dict):
                return value

        if "patient_id" in api_json or "patient_name" in api_json:
            return api_json

    return None

def norm(v):
    if v is None:
        return ""
    return str(v)

def normalize_patient(row):
    sex_raw = norm(row.get("sex")).strip()

    if sex_raw in ["1", "男", "M", "m", "male", "Male"]:
        sex = "男"
    elif sex_raw in ["2", "女", "F", "f", "female", "Female"]:
        sex = "女"
    else:
        sex = sex_raw

    return {
        "exam_item": norm(row.get("exam_item")),
        "his_exam_no": norm(row.get("his_exam_no")),
        "report_no": norm(row.get("report_no")),
        "patient_id": norm(row.get("patient_id")),
        "patient_name": norm(row.get("patient_name")),
        "name_phonetic": norm(row.get("name_phonetic")),
        "xing": norm(row.get("xing")),
        "ming": norm(row.get("ming")),
        "sex": sex,
        "age": norm(row.get("age")),
        "nian": norm(row.get("nian")),
        "yue": norm(row.get("yue")),
        "ri": norm(row.get("ri")),
        "birthday": norm(row.get("birthday")),
    }

def load_template():
    if not os.path.exists(TEMPLATE_FILE):
        raise RuntimeError("template not found: %s" % TEMPLATE_FILE)

    with open(TEMPLATE_FILE, "r") as f:
        return json.load(f)

def build_form_task(scan_text, patient):
    template = load_template()

    task = {
        "id": "form_%d" % int(time.time() * 1000),
        "source": "scanner_api",
        "action": "form_fill",
        "scan_text": scan_text,
        "time": now(),
        "patient": patient,
        "title": template.get("title", ""),
        "windowTitleLocation": template.get("windowTitleLocation", ""),
        "eventClassList": []
    }

    for ev in template.get("eventClassList", []):
        new_ev = dict(ev)
        click_type = new_ev.get("clickType")
        text = new_ev.get("text")

        if click_type == 1 and text:
            field_name = text
            new_ev["field"] = field_name
            new_ev["text"] = patient.get(field_name, "")

        if click_type == 7 and text:
            new_ev["condition"] = {
                "field": "sex",
                "equals": text.replace("性别:", "")
            }

        task["eventClassList"].append(new_ev)

    return task

def write_form_task(task):
    os.makedirs(FORM_QUEUE_DIR, exist_ok=True)

    ts = int(time.time() * 1000)
    tmp_path = "%s/form_%d.tmp" % (FORM_QUEUE_DIR, ts)
    json_path = "%s/form_%d.json" % (FORM_QUEUE_DIR, ts)

    with open(tmp_path, "w") as f:
        json.dump(task, f, ensure_ascii=False, indent=2)

    os.rename(tmp_path, json_path)

    log("form task saved: %s" % json_path)

def atomic_write_json(path, obj):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.rename(tmp_path, path)

def write_patient_state(scan_text, patient):
    state = {
        "scan_text": scan_text,
        "time": now(),
        "patient": patient,
        "patient_id": patient.get("patient_id", ""),
        "report_no": patient.get("report_no", ""),
        "his_exam_no": patient.get("his_exam_no", "")
    }
    atomic_write_json(CURRENT_PATIENT_FILE, state)
    log("current patient state saved: %s" % CURRENT_PATIENT_FILE)

def write_report_wait_task(scan_text, patient):
    os.makedirs(REPORT_WAIT_DIR, exist_ok=True)
    ts = int(time.time() * 1000)
    task = {
        "id": "report_%d" % ts,
        "source": "scanner_api",
        "status": "waiting_print",
        "scan_text": scan_text,
        "time": now(),
        "patient": patient,
        "patient_id": patient.get("patient_id", ""),
        "report_no": patient.get("report_no", ""),
        "his_exam_no": patient.get("his_exam_no", "")
    }
    path = "%s/report_%d.json" % (REPORT_WAIT_DIR, ts)
    atomic_write_json(path, task)
    log("report wait task saved: %s" % path)

def handle_scan(scan_text):
    log("SCAN: %s" % scan_text)
    save_scan_log(scan_text)

    try:
        api_json = http_get_patient(scan_text)
        row = pick_first_record(api_json)

        if not row:
            raise RuntimeError("api returned no patient record")

        patient = normalize_patient(row)

        log("patient_id=%s patient_name=%s sex=%s age=%s" %
            (patient["patient_id"], patient["patient_name"], patient["sex"], patient["age"]))

        task = build_form_task(scan_text, patient)
        write_form_task(task)
        write_patient_state(scan_text, patient)
        write_report_wait_task(scan_text, patient)

    except Exception as e:
        log("ERROR handle scan failed: %s" % e)
        raise

def read_scanner(dev):
    log("listening on %s" % dev)

    buf = []
    shift = False

    with open(dev, "rb") as f:
        while True:
            data = f.read(EVENT_SIZE)

            if len(data) == 0:
                raise RuntimeError("scanner disconnected")

            if len(data) != EVENT_SIZE:
                continue

            sec, usec, ev_type, code, value = struct.unpack(FMT, data)

            if ev_type != EV_KEY:
                continue

            if code in IGNORE_CODES:
                continue

            if code in SHIFT_CODES:
                shift = (value != KEY_UP)
                continue

            if value != KEY_DOWN:
                continue

            if code in ENTER_CODES:
                if buf:
                    text = "".join(buf)
                    buf = []
                    handle_scan(text)
                continue

            ch = KEY_MAP.get(code)

            if ch is None:
                log("unknown key code: %s" % code)
                continue

            if shift:
                if ch.isalpha():
                    ch = ch.upper()
                else:
                    ch = SHIFT_MAP.get(ch, ch)

            buf.append(ch)

def main():
    ensure_dirs()
    log("scan_patient_capture start")
    log("api=%s" % API_URL)

    while True:
        dev = wait_scanner()

        try:
            read_scanner(dev)
        except Exception as e:
            log("FATAL scanner error, exit for supervisor restart: %s" % e)
            sys.exit(1)

if __name__ == "__main__":
    main()
