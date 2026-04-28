#!/usr/bin/env python3
import os
import time
import json
import shutil
import base64
import sys
import threading

QUEUE_DIR = "/root/usb_bridge/queue"
FORM_QUEUE_DIR = "/root/usb_bridge/form_queue"

DONE_DIR = "/root/usb_bridge/done"
FORM_DONE_DIR = "/root/usb_bridge/form_done"
ERROR_DIR = "/root/usb_bridge/error"

LOG_FILE = "/root/usb_bridge/logs/hid_executor.log"

KEYBOARD_DEV = "/dev/hidg0"
MOUSE_DEV = "/dev/hidg1"

SCREEN_W = 1920
SCREEN_H = 1080
ABS_MAX = 32767

TYPE_DELAY = 0.01

KEY_CAPSLOCK = 0x39

keyboard_led_state = None
keyboard_led_lock = threading.Lock()

HID_CHAR_MAP = {}

# a-z / A-Z
for i, ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
    HID_CHAR_MAP[ch] = (0x04 + i, 0x00)
    HID_CHAR_MAP[ch.upper()] = (0x04 + i, 0x02)

# 0-9
for ch, code in {
    "1": 0x1e, "2": 0x1f, "3": 0x20, "4": 0x21, "5": 0x22,
    "6": 0x23, "7": 0x24, "8": 0x25, "9": 0x26, "0": 0x27,
}.items():
    HID_CHAR_MAP[ch] = (code, 0x00)

# Shift + 数字符号
for ch, code in {
    "!": 0x1e, "@": 0x1f, "#": 0x20, "$": 0x21, "%": 0x22,
    "^": 0x23, "&": 0x24, "*": 0x25, "(": 0x26, ")": 0x27,
}.items():
    HID_CHAR_MAP[ch] = (code, 0x02)

# 常用符号
for ch, code, mod in [
    ("\n", 0x28, 0x00),
    ("\t", 0x2b, 0x00),
    (" ", 0x2c, 0x00),
    ("-", 0x2d, 0x00), ("_", 0x2d, 0x02),
    ("=", 0x2e, 0x00), ("+", 0x2e, 0x02),
    ("[", 0x2f, 0x00), ("{", 0x2f, 0x02),
    ("]", 0x30, 0x00), ("}", 0x30, 0x02),
    ("\\", 0x31, 0x00), ("|", 0x31, 0x02),
    (";", 0x33, 0x00), (":", 0x33, 0x02),
    ("'", 0x34, 0x00), ("\"", 0x34, 0x02),
    ("`", 0x35, 0x00), ("~", 0x35, 0x02),
    (",", 0x36, 0x00), ("<", 0x36, 0x02),
    (".", 0x37, 0x00), (">", 0x37, 0x02),
    ("/", 0x38, 0x00), ("?", 0x38, 0x02),
]:
    HID_CHAR_MAP[ch] = (code, mod)


def log(msg):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def wait_dev(path):
    while not os.path.exists(path):
        log("waiting for %s ..." % path)
        time.sleep(1)


def keyboard_led_reader():
    global keyboard_led_state

    while True:
        wait_dev(KEYBOARD_DEV)

        fd = None
        try:
            fd = os.open(KEYBOARD_DEV, os.O_RDONLY | os.O_NONBLOCK)
            log("keyboard led reader start")

            while True:
                try:
                    data = os.read(fd, 8)
                    if data:
                        with keyboard_led_lock:
                            keyboard_led_state = data[0]
                        log("keyboard led state=0x%02x caps=%s" % (
                            data[0],
                            "on" if data[0] & 0x02 else "off"
                        ))
                    else:
                        time.sleep(0.05)
                except BlockingIOError:
                    time.sleep(0.05)

        except Exception as e:
            log("keyboard led reader error: %s" % e)
            time.sleep(1)

        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass


def get_capslock_state():
    with keyboard_led_lock:
        state = keyboard_led_state

    if state is None:
        return None

    return bool(state & 0x02)


def wait_capslock_state(timeout=0.5):
    end = time.time() + timeout

    while time.time() < end:
        state = get_capslock_state()
        if state is not None:
            return state
        time.sleep(0.05)

    return get_capslock_state()


def send_key(hid, modifier, keycode):
    hid.write(bytes([modifier, 0x00, keycode, 0x00, 0x00, 0x00, 0x00, 0x00]))
    hid.flush()
    time.sleep(TYPE_DELAY)

    hid.write(b"\x00\x00\x00\x00\x00\x00\x00\x00")
    hid.flush()
    time.sleep(TYPE_DELAY)


def press_key(modifier, keycode):
    wait_dev(KEYBOARD_DEV)
    with open(KEYBOARD_DEV, "wb", buffering=0) as hid:
        send_key(hid, modifier, keycode)


def press_capslock():
    press_key(0x00, KEY_CAPSLOCK)
    time.sleep(0.15)


def ensure_capslock(target_on):
    current = wait_capslock_state()

    if current is None:
        log("WARN capslock state unknown, fallback toggle")
        press_capslock()
        time.sleep(0.2)
        current = wait_capslock_state()

    if current is None:
        return None

    if bool(current) != bool(target_on):
        press_capslock()
        time.sleep(0.2)
        current = wait_capslock_state()

    if current is not None and bool(current) != bool(target_on):
        log("WARN capslock ensure failed target=%s actual=%s" % (target_on, current))

    return current


def type_text(text, enter=False):
    wait_dev(KEYBOARD_DEV)

    with open(KEYBOARD_DEV, "wb", buffering=0) as hid:
        for ch in str(text):
            item = HID_CHAR_MAP.get(ch)
            if item is None:
                log("WARN unsupported char skipped: %r" % ch)
                continue

            keycode, modifier = item
            send_key(hid, modifier, keycode)

        if enter:
            send_key(hid, 0x00, 0x28)


def select_all():
    # Ctrl + A
    press_key(0x01, 0x04)
    time.sleep(0.1)


def paste_clipboard():
    # Ctrl + V
    press_key(0x01, 0x19)
    time.sleep(0.3)


def open_run_dialog():
    # Win + R
    press_key(0x08, 0x15)
    time.sleep(0.6)


def press_enter():
    press_key(0x00, 0x28)
    time.sleep(0.3)


def has_non_ascii(text):
    try:
        str(text).encode("ascii")
        return False
    except Exception:
        return True


def should_use_capslock_type(text, field=""):
    text = str(text)
    field = str(field or "").lower()

    if not text or has_non_ascii(text):
        return False

    if field in ["patient_id", "report_no", "his_exam_no"]:
        return any(ch.isalpha() for ch in text)

    return len(text) >= 2 and text[0].isalpha() and any(ch.isdigit() for ch in text[1:])


def type_ascii_direct(text, field=""):
    text = str(text)

    if should_use_capslock_type(text, field):
        old_caps = wait_capslock_state()
        log("capslock direct type field=%s old_caps=%s text=%s" % (field, old_caps, text))
        ensure_capslock(True)
        type_text(text.lower(), enter=False)

        if old_caps is not None:
            ensure_capslock(old_caps)
        else:
            ensure_capslock(False)

        return

    type_text(text, enter=False)


def pixel_to_abs_x(x):
    x = max(0, min(SCREEN_W - 1, int(x)))
    return int(x * ABS_MAX / SCREEN_W)


def pixel_to_abs_y(y):
    y = max(0, min(SCREEN_H - 1, int(y)))
    return int(y * ABS_MAX / SCREEN_H)


def mouse_abs_report(button, x, y):
    wait_dev(MOUSE_DEV)

    ax = pixel_to_abs_x(x)
    ay = pixel_to_abs_y(y)
    button = int(button) & 0xff

    data = bytes([
        button,
        ax & 0xff,
        (ax >> 8) & 0xff,
        ay & 0xff,
        (ay >> 8) & 0xff
    ])

    with open(MOUSE_DEV, "wb", buffering=0) as m:
        m.write(data)
        m.flush()


def mouse_click_abs(x, y, button="left"):
    if button == "left":
        b = 0x01
    elif button == "right":
        b = 0x02
    elif button == "middle":
        b = 0x04
    else:
        b = 0x01

    log("mouse_click_abs x=%s y=%s button=%s" % (x, y, button))

    mouse_abs_report(0x00, x, y)
    time.sleep(0.05)

    mouse_abs_report(b, x, y)
    time.sleep(0.08)

    mouse_abs_report(0x00, x, y)
    time.sleep(0.1)


def click_abs(x, y):
    mouse_click_abs(int(x), int(y), "left")


def paste_text_windows(text, target_x, target_y):
    """
    中文输入方案：
    1. 把中文转成 PowerShell [char] Unicode 码点表达式
    2. Win+R 运行 PowerShell
    3. PowerShell 写入剪贴板
    4. 点击目标输入框
    5. Ctrl+A，Ctrl+V 粘贴中文
    """
    text = str(text)

    parts = []
    for ch in text:
        parts.append("[char]%d" % ord(ch))

    char_expr = "+".join(parts)

    run_cmd = (
        'powershell -sta -nop -w hidden -c '
        '"Set-Clipboard -Value (%s)"'
    ) % char_expr

    log("paste_text_windows: %s" % text)
    log("powershell cmd length: %d" % len(run_cmd))

    # 打开 Windows 运行框
    open_run_dialog()

    # 清空运行框并输入命令
    select_all()
    type_text(run_cmd, enter=True)

    # 等 PowerShell 写入剪贴板
    time.sleep(1.5)

    # 回到目标输入框
    click_abs(target_x, target_y)
    time.sleep(0.2)

    # 清空原内容并粘贴中文
    select_all()
    paste_clipboard()

def process_simple_task(path):
    with open(path, "r") as f:
        task = json.load(f)

    action = task.get("action")

    if action == "type_text":
        text = task.get("text", "")
        enter = bool(task.get("enter", True))
        log("type_text: %s" % text)
        type_text(text, enter)

    elif action == "mouse_click_abs":
        x = task.get("x", 0)
        y = task.get("y", 0)
        button = task.get("button", "left")
        mouse_click_abs(x, y, button)

    elif action == "paste_text_abs":
        text = task.get("text", "")
        x = task.get("x", 0)
        y = task.get("y", 0)
        paste_text_windows(text, x, y)

    else:
        log("WARN unsupported simple action: %s" % action)


def process_form_task(path):
    with open(path, "r") as f:
        task = json.load(f)

    patient = task.get("patient", {})
    events = task.get("eventClassList", [])

    log("form_fill start patient_id=%s patient_name=%s sex=%s age=%s" % (
        patient.get("patient_id", ""),
        patient.get("patient_name", ""),
        patient.get("sex", ""),
        patient.get("age", "")
    ))

    for ev in events:
        click_type = ev.get("clickType")
        x = ev.get("x")
        y = ev.get("y")

        if click_type == 0:
            click_abs(x, y)
            time.sleep(0.15)

        elif click_type == 1:
            text = ev.get("text", "")
            field = ev.get("field", "")

            log("input field=%s text=%s" % (field, text))

            if has_non_ascii(text):
                paste_text_windows(text, x, y)
            else:
                click_abs(x, y)
                time.sleep(0.05)
                select_all()
                type_ascii_direct(text, field)

            time.sleep(0.08)

        elif click_type == 7:
            cond = ev.get("condition", {})
            field = cond.get("field", "")
            equals = cond.get("equals", "")
            actual = patient.get(field, "")

            if str(actual) == str(equals):
                log("radio matched %s=%s click x=%s y=%s" % (field, equals, x, y))
                click_abs(x, y)
                time.sleep(0.15)
            else:
                log("radio skip %s need=%s actual=%s" % (field, equals, actual))

        else:
            log("WARN unknown clickType=%s index=%s" % (click_type, ev.get("index")))

    log("form_fill done")


def move_file(src, dst_dir, name):
    os.makedirs(dst_dir, exist_ok=True)
    shutil.move(src, os.path.join(dst_dir, name))


def consume_dir(queue_dir, done_dir, processor):
    os.makedirs(queue_dir, exist_ok=True)
    files = sorted([f for f in os.listdir(queue_dir) if f.endswith(".json")])

    for name in files:
        src = os.path.join(queue_dir, name)
        work = os.path.join(queue_dir, name + ".work")

        try:
            os.rename(src, work)
            processor(work)
            move_file(work, done_dir, name)
            log("done: %s" % name)
        except Exception as e:
            log("ERROR %s: %s" % (name, e))
            try:
                move_file(work, ERROR_DIR, name)
            except Exception:
                pass
            log("FATAL task failed, exit for supervisor restart")
            sys.exit(1)


def main():
    os.makedirs(QUEUE_DIR, exist_ok=True)
    os.makedirs(FORM_QUEUE_DIR, exist_ok=True)
    os.makedirs(DONE_DIR, exist_ok=True)
    os.makedirs(FORM_DONE_DIR, exist_ok=True)
    os.makedirs(ERROR_DIR, exist_ok=True)

    log("hid_executor start")
    log("keyboard=%s mouse=%s" % (KEYBOARD_DEV, MOUSE_DEV))
    log("simple queue=%s" % QUEUE_DIR)
    log("form queue=%s" % FORM_QUEUE_DIR)

    wait_dev(KEYBOARD_DEV)
    wait_dev(MOUSE_DEV)

    t = threading.Thread(target=keyboard_led_reader, daemon=True)
    t.start()

    while True:
        consume_dir(FORM_QUEUE_DIR, FORM_DONE_DIR, process_form_task)
        # 正式流程只处理 form_queue，避免旧 scan_capture.py 生成的 queue 任务干扰
        # consume_dir(QUEUE_DIR, DONE_DIR, process_simple_task)
        time.sleep(0.1)


if __name__ == "__main__":
    main()
