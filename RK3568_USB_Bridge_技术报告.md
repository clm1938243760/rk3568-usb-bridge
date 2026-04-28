# RK3568 USB Bridge 扫码录入与报告上传系统技术报告

## 1. 项目概述

本系统基于 RK3568 嵌入式 Linux 开发板，实现一套面向 Windows 主机端检查/报告软件的自动化桥接方案。系统通过 USB Gadget 同时模拟 HID 键盘、HID 鼠标和 USB 打印机，实现以下两个核心业务流程：

1. 扫码枪接入开发板，读取编号后调用业务 API 查询患者/检查信息，并根据接口返回数据生成 JSON 键鼠操作任务，由 HID Gadget 自动录入 Windows 软件界面。
2. Windows 主机打印报告时，将开发板识别为 USB 打印机。开发板接收打印产生的 PS 数据流，按扫码得到的患者编号/报告号命名，转换为 PDF 后上传到云端服务器。

系统设计目标是：扫码之后必须产生待打印报告任务，报告打印、转换、上传均通过队列目录串联；关键模块出错后退出进程，并由开机自启脚本中的守护循环自动重启。

## 2. 系统运行环境

### 2.1 硬件环境

- 主控平台：RK3568 嵌入式 Linux 开发板
- 外设输入：USB 扫码枪
- 主机端：Windows 电脑
- USB Gadget 功能：
  - HID Keyboard
  - HID Mouse
  - USB Printer

### 2.2 软件环境

- 系统路径：`/root/usb_bridge`
- 业务脚本目录：`/root/usb_bridge/bin`
- 开机自启脚本：
  - `/etc/init.d/S50usbdevice`
  - `/etc/init.d/S98usbbridge`
- Python 版本：Python 3
- PS 转 PDF 工具：Ghostscript，命令名默认为 `gs`
- 云端上传接口：
  - `http://8.148.73.190:5000/upload`

## 3. USB Gadget 配置

USB Gadget 由 `/etc/init.d/S50usbdevice` 在开机阶段创建，配置路径为：

```text
/sys/kernel/config/usb_gadget/rockchip
```

该脚本完成以下功能：

- 挂载 configfs
- 创建 gadget 描述符
- 设置 VID/PID
- 创建 HID 键盘 function：`functions/hid.usb0`
- 创建 HID 鼠标 function：`functions/hid.usb1`
- 创建 USB 打印机 function：`functions/printer.usb0`
- 将三个 function 链接到同一个 configuration
- 绑定 UDC，使 Windows 主机识别该复合 USB 设备

系统期望生成的设备节点为：

```text
/dev/hidg0
/dev/hidg1
/dev/g_printer0
```

其中：

- `/dev/hidg0`：HID 键盘输出
- `/dev/hidg1`：HID 鼠标输出
- `/dev/g_printer0`：USB 打印机数据接收

## 4. 软件模块设计

系统业务程序位于：

```text
/root/usb_bridge/bin
```

当前包含以下核心模块。

### 4.1 scan_patient_capture.py

功能：扫码枪监听、编号解析、API 查询、生成表单录入任务和待打印任务。

主要职责：

- 在 `/proc/bus/input/devices` 中查找扫码枪设备
- 监听扫码枪对应的 `/dev/input/event*`
- 将按键事件还原为扫码字符串
- 调用患者信息 API
- 保存 API 原始返回
- 规范化患者字段
- 根据模板生成 HID 表单录入 JSON
- 写入当前患者状态
- 生成“等待打印报告”队列任务

输入：

```text
扫码枪输入编号
```

输出：

```text
/root/usb_bridge/api_raw/api_*.json
/root/usb_bridge/form_queue/form_*.json
/root/usb_bridge/state/current_patient.json
/root/usb_bridge/report_wait_queue/report_*.json
```

### 4.2 hid_executor.py

功能：消费表单录入任务，通过 HID 键盘和鼠标对 Windows 主机执行自动录入。

主要职责：

- 监听 `/root/usb_bridge/form_queue`
- 读取 JSON 中的 `eventClassList`
- 执行鼠标绝对坐标点击
- 执行键盘输入
- 对中文或复杂文本使用 Windows 剪贴板粘贴方案
- 成功后归档任务
- 失败后移动到 error，并退出等待自启脚本重启

输入：

```text
/root/usb_bridge/form_queue/*.json
```

输出：

```text
/root/usb_bridge/form_done/*.json
/root/usb_bridge/error/*.json
```

### 4.3 printer_capture.py

功能：消费待打印报告任务，并从 USB printer gadget 接收 Windows 打印数据。

主要职责：

- 监听 `/root/usb_bridge/report_wait_queue`
- 每次取一个待打印任务
- 打开 `/dev/g_printer0`
- 接收 Windows 主机发送的 PS 打印数据
- 根据患者 ID、报告号、时间戳命名文件
- 生成 PS 文件和对应 JSON 元数据
- 将等待任务归档到 `report_wait_done`
- 出错后移动任务到 `report_error`，并退出等待重启

输入：

```text
/root/usb_bridge/report_wait_queue/*.json
/dev/g_printer0
```

输出：

```text
/root/usb_bridge/report_print_queue/*.ps
/root/usb_bridge/report_print_queue/*.json
/root/usb_bridge/report_wait_done/*.json
```

### 4.4 report_uploader.py

功能：将接收到的 PS 报告转换成 PDF，并上传到云端。

主要职责：

- 消费 `/root/usb_bridge/report_print_queue/*.json`
- 根据元数据定位对应 PS 文件
- 调用 Ghostscript 将 PS 转为 PDF
- 将 PDF 上传到云端接口
- 上传成功后归档 `.json`、`.ps`、`.pdf`
- 失败后将任务移动到 `report_error`，并退出等待重启

输入：

```text
/root/usb_bridge/report_print_queue/*.json
/root/usb_bridge/report_print_queue/*.ps
```

输出：

```text
/root/usb_bridge/report_pdf_queue/*.pdf
/root/usb_bridge/report_uploaded/*
/root/usb_bridge/report_error/*
```

上传接口：

```text
http://8.148.73.190:5000/upload
```

## 5. 队列目录设计

系统采用目录队列方式实现模块解耦。每个模块只处理自己的输入队列，并将结果写入下游队列或归档目录。

### 5.1 基础目录

```text
/root/usb_bridge
```

系统所有业务文件、队列、日志均位于该目录下。

### 5.2 表单录入相关目录

```text
/root/usb_bridge/form_queue
```

扫码/API 成功后生成的表单录入任务目录。`hid_executor.py` 从这里读取任务。

```text
/root/usb_bridge/form_done
```

表单录入成功后的任务归档目录。

```text
/root/usb_bridge/error
```

HID 录入失败或表单任务处理失败时的错误任务目录。

### 5.3 患者状态目录

```text
/root/usb_bridge/state/current_patient.json
```

当前扫码成功的患者上下文文件。内容包括：

- `scan_text`
- `patient_id`
- `report_no`
- `his_exam_no`
- `patient`
- `time`

该文件用于记录当前业务状态，方便排查和后续模块扩展。

### 5.4 API 原始数据目录

```text
/root/usb_bridge/api_raw
```

保存每次扫码调用患者信息接口后的原始 JSON 返回数据。用于接口调试和问题追溯。

### 5.5 报告打印相关目录

```text
/root/usb_bridge/report_wait_queue
```

扫码成功后生成的待打印报告任务目录。该目录中存在任务，表示系统要求后续必须收到一份对应报告打印数据。

```text
/root/usb_bridge/report_wait_done
```

待打印任务被 `printer_capture.py` 成功匹配并收到打印数据后，会移动到该目录归档。

```text
/root/usb_bridge/report_print_queue
```

打印数据接收完成后的队列目录。包含：

- `.ps`：Windows 打印产生的 PostScript 数据文件
- `.json`：该报告对应的患者和打印元数据

文件命名格式：

```text
患者ID_报告号_时间戳.ps
患者ID_报告号_时间戳.json
```

```text
/root/usb_bridge/report_pdf_queue
```

PS 转 PDF 后的临时 PDF 队列目录。

```text
/root/usb_bridge/report_uploaded
```

报告上传成功后的归档目录。上传成功后，相关 `.json`、`.ps`、`.pdf` 都会移动到这里。

```text
/root/usb_bridge/report_error
```

打印接收、PS 转 PDF、PDF 上传过程中出错的任务会移动到该目录。

### 5.6 日志目录

```text
/root/usb_bridge/logs
```

主要日志文件：

```text
boot.log
scan_patient_capture.log
scan_patient_capture_stdout.log
hid_executor.log
hid_executor_stdout.log
printer_capture.log
printer_capture_stdout.log
report_uploader.log
report_uploader_stdout.log
```

其中：

- `boot.log`：开机脚本启动、重启记录
- `*_stdout.log`：程序标准输出和异常信息
- 模块专用 `.log`：模块自身业务日志

## 6. 业务流程

### 6.1 扫码录入流程

1. 用户使用扫码枪扫描编号。
2. `scan_patient_capture.py` 从扫码枪 event 设备读取按键事件。
3. 程序拼接出完整编号。
4. 程序调用患者信息 API。
5. API 返回患者/检查信息。
6. 程序保存原始 API 返回到 `api_raw`。
7. 程序生成表单录入任务到 `form_queue`。
8. 程序写入当前患者状态 `state/current_patient.json`。
9. 程序生成待打印报告任务到 `report_wait_queue`。
10. `hid_executor.py` 消费 `form_queue` 中的任务。
11. 开发板通过 `/dev/hidg0` 和 `/dev/hidg1` 对 Windows 主机执行键鼠操作。
12. 表单录入成功后任务移动到 `form_done`。

### 6.2 报告打印上传流程

1. 扫码成功后，`report_wait_queue` 中存在待打印报告任务。
2. Windows 主机端报告软件执行打印。
3. Windows 将开发板识别为 USB 打印机并发送 PS 数据。
4. `printer_capture.py` 从 `/dev/g_printer0` 接收打印数据。
5. 接收完成后，程序在 `report_print_queue` 中生成 `.ps` 和 `.json`。
6. 对应待打印任务移动到 `report_wait_done`。
7. `report_uploader.py` 消费 `report_print_queue` 中的任务。
8. 程序调用 Ghostscript 将 PS 转换为 PDF。
9. 生成 PDF 到 `report_pdf_queue`。
10. 程序将 PDF 上传到云端接口。
11. 上传成功后，`.json`、`.ps`、`.pdf` 移动到 `report_uploaded`。

## 7. 开机启动与进程守护

系统采用传统 init.d 开机自启方式。

### 7.1 S50usbdevice

启动阶段：

```text
/etc/init.d/S50usbdevice
```

负责创建 USB Gadget 设备，包括 HID 键盘、HID 鼠标和 USB 打印机。

### 7.2 S98usbbridge

启动阶段：

```text
/etc/init.d/S98usbbridge
```

负责启动业务程序。

启动前会等待以下设备节点就绪：

```text
/dev/hidg0
/dev/hidg1
/dev/g_printer0
```

随后启动以下程序：

```text
hid_executor.py
scan_patient_capture.py
printer_capture.py
report_uploader.py
```

每个程序由 shell `while true` 循环守护。程序异常退出后，启动脚本会记录退出码，并在 2 秒后重新启动该程序。

## 8. 异常处理机制

系统异常处理遵循以下原则：

1. 单个任务失败时，尽量先移动到错误目录，避免反复处理同一个坏任务。
2. 程序遇到关键错误后直接退出。
3. 开机自启脚本检测到程序退出后自动重启。
4. 队列目录不随重启清空，避免断电或程序重启导致任务丢失。

### 8.1 扫码/API 异常

如果扫码后 API 查询失败，`scan_patient_capture.py` 会记录错误并退出，等待守护脚本重启。

### 8.2 HID 录入异常

如果 HID 执行任务失败，任务会移动到：

```text
/root/usb_bridge/error
```

随后 `hid_executor.py` 退出，等待重启。

### 8.3 打印接收异常

如果等待打印任务已取出，但打印数据接收失败，任务会移动到：

```text
/root/usb_bridge/report_error
```

随后 `printer_capture.py` 退出，等待重启。

### 8.4 报告转换/上传异常

如果 PS 转 PDF 或 PDF 上传失败，任务会移动到：

```text
/root/usb_bridge/report_error
```

随后 `report_uploader.py` 退出，等待重启。

## 9. 数据命名规则

报告相关文件采用以下命名格式：

```text
患者ID_报告号_时间戳.扩展名
```

示例：

```text
P000123_RPT456_1777350000000.ps
P000123_RPT456_1777350000000.json
P000123_RPT456_1777350000000.pdf
```

命名字段来源：

- 患者 ID：`patient_id`
- 报告号：`report_no`
- 时间戳：当前毫秒时间戳

## 10. 云端上传设计

PDF 上传地址配置在 `/etc/init.d/S98usbbridge`：

```sh
REPORT_UPLOAD_URL=http://8.148.73.190:5000/upload
```

上传模块使用 HTTP POST 上传 PDF 文件。

请求头包含：

```text
Content-Type: application/pdf
X-Patient-Id: patient_id
X-Report-No: report_no
X-His-Exam-No: his_exam_no
User-Agent: RK3568-USB-Bridge
```

云端接口需要接收请求体中的 PDF 二进制数据，并可根据请求头完成报告归档。

## 11. 部署说明

### 11.1 文件部署

业务脚本应位于：

```text
/root/usb_bridge/bin
```

包括：

```text
scan_patient_capture.py
hid_executor.py
printer_capture.py
report_uploader.py
```

开机脚本应位于：

```text
/etc/init.d/S50usbdevice
/etc/init.d/S98usbbridge
```

### 11.2 权限设置

建议确保脚本具有可执行权限：

```sh
chmod +x /etc/init.d/S50usbdevice
chmod +x /etc/init.d/S98usbbridge
chmod +x /root/usb_bridge/bin/*.py
```

### 11.3 依赖检查

检查设备节点：

```sh
ls -l /dev/hidg0 /dev/hidg1 /dev/g_printer0
```

检查 Ghostscript：

```sh
which gs
gs --version
```

检查进程：

```sh
ps w | grep -E "scan_patient_capture|hid_executor|printer_capture|report_uploader" | grep -v grep
```

查看启动日志：

```sh
tail -f /root/usb_bridge/logs/boot.log
```

## 12. 运维排查

### 12.1 查看扫码是否成功

```sh
tail -f /root/usb_bridge/logs/scan_patient_capture.log
ls -l /root/usb_bridge/api_raw
ls -l /root/usb_bridge/form_queue
ls -l /root/usb_bridge/report_wait_queue
```

### 12.2 查看 HID 录入是否成功

```sh
tail -f /root/usb_bridge/logs/hid_executor.log
ls -l /root/usb_bridge/form_done
ls -l /root/usb_bridge/error
```

### 12.3 查看打印数据是否收到

```sh
tail -f /root/usb_bridge/logs/printer_capture.log
ls -l /root/usb_bridge/report_print_queue
ls -l /root/usb_bridge/report_wait_done
```

### 12.4 查看 PDF 是否上传成功

```sh
tail -f /root/usb_bridge/logs/report_uploader.log
ls -l /root/usb_bridge/report_pdf_queue
ls -l /root/usb_bridge/report_uploaded
ls -l /root/usb_bridge/report_error
```

## 13. 当前方案特点

1. 业务链路清晰：扫码、录入、打印接收、PDF 上传分模块实现。
2. 采用目录队列：模块之间通过文件交接，便于调试和恢复。
3. 支持断点恢复：重启后队列文件仍保留。
4. 支持异常重启：程序出错退出后由启动脚本自动拉起。
5. 支持报告强制关联：扫码成功后必须产生待打印任务，打印模块只处理待打印队列中的任务。
6. 适合嵌入式部署：不依赖复杂服务框架，使用 init.d 和 Python 脚本即可运行。

## 14. 后续优化建议

1. 增加待打印任务超时机制。如果扫码后长时间未打印，可将任务标记为超时并报警。
2. 增加上传重试次数和重试间隔。目前上传失败后进入错误目录并重启，后续可支持自动重试 N 次。
3. 增加报告文件校验。上传前可检查 PDF 页数、文件大小和转换结果。
4. 增加本地 Web 状态页。显示当前队列数量、最近扫码、最近上传、错误任务。
5. 增加配置文件。将 API 地址、上传地址、设备名、屏幕分辨率等移到统一配置文件。
6. 优化 HID 坐标录入。当前依赖固定屏幕坐标，后续可加入窗口检测或模板版本管理。

## 15. 总结

本系统通过 RK3568 USB Gadget 能力，在不改造 Windows 主机业务软件的前提下，实现了扫码查询、自动录入、模拟打印、报告转换和云端上传的完整闭环。系统以目录队列为核心，保证各模块职责清晰、状态可追踪、异常可恢复，适合在嵌入式 Linux 环境中长期运行。
