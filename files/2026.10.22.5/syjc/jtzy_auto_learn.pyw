# -*- coding: utf-8 -*-
"""交通职业资格继续教育自动学习助手（syjc.jtzyzg.org.cn / 试验检测）

真实 Chrome/Edge 浏览器 + Playwright 驱动：
  1. 自动登录（识别码直接从页面读取，失败则提示手动输入）
  2. 查询“我的课程”与学习进度
  3. 逐门课程、逐个课件真实播放；断点续播；播完点“安全退出”保存学时
  4. 全程日志 + 预计剩余时间

说明：平台按真实播放时长在服务端累计学时（播放中每分钟一次心跳上报），
     无法通过接口直接“秒完成”，本软件只做自动播放与流程托管。
"""

from __future__ import annotations

import base64
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

BASE_DIR = Path(__file__).resolve().parent
PORTABLE_ROOT = BASE_DIR.parent
APP_VERSION = "2026.10.22.5"
UPDATE_EXE = "启动SYJC学习.bat"
CONFIG_PATH = BASE_DIR / "config.json"
LOG_DIR = BASE_DIR / "logs"
PROFILE_DIR = BASE_DIR / "browser_profile"

SITE_BASE = "https://syjc.jtzyzg.org.cn/SYJC/LEAP/syjcEdu/html"
LOGIN_URL = "https://syjc.jtzyzg.org.cn/SYJC/LEAP/syjc/html/index.html"
MY_COURSES_URL = f"{SITE_BASE}/myCourse.html"
MY_COURSE_INIT_URL = "https://syjc.jtzyzg.org.cn/SYJC/LEAP/syjcEdu/webpkg/init/myCourse-c7305ea5a4.init.js"

# 实测：2.00 学时的课程视频约 86 分钟，用于估算总耗时
MINUTES_PER_HOUR_CREDIT = 43.0


def now_text() -> str:
    return datetime.now().strftime("%H:%M:%S")


def mmss(seconds) -> str:
    if seconds is None:
        return "--:--"
    seconds = int(seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def parse_progress(raw) -> tuple[int, bool]:
    """把平台的进度字段统一解析成 0-100 的整数。

    返回 (百分比, 是否真的读到了数字)。平台在不同接口里可能给
    "98" / "98%" / 0.98 / 98 这几种写法，也可能是空值。
    """
    if raw is None or isinstance(raw, bool):
        return 0, False
    if isinstance(raw, (int, float)):
        value = float(raw)
        if 0.0 <= value <= 1.0:
            return int(round(value * 100)), True
        return int(round(value)), True
    text = str(raw).strip()
    if not text:
        return 0, False
    has_percent = "%" in text
    cleaned = re.sub(r"[^0-9.]", "", text)
    if not cleaned:
        return 0, False
    try:
        value = float(cleaned)
    except ValueError:
        return 0, False
    if not has_percent and "." in cleaned and 0.0 <= value <= 1.0:
        return int(round(value * 100)), True
    return int(round(value)), True


class Engine(threading.Thread):
    """浏览器自动化引擎，所有 Playwright 调用都在这个线程里。"""

    def __init__(self, config: dict, out: "queue.Queue[tuple]") -> None:
        super().__init__(daemon=True)
        self.config = config
        self.out = out
        self.stop_flag = threading.Event()
        self.dialogs: list[str] = []
        self.log_file: Path | None = None
        self.known_hours: dict[str, float] = {}
        self.watched_seconds = 0.0
        self.current_rows: list = []
        self.tests_only = bool(config.get("tests_only"))
        self.answer_mode = bool(config.get("answer_mode"))
        self.answer_round = 1
        self.use_ai = bool(config.get("use_ai", False))
        self.list_only = bool(config.get("list_only"))
        try:
            self.playback_rate = float(config.get("playback_rate") or 2.0)
        except Exception:
            self.playback_rate = 2.0
        self.headless = bool(config.get("headless", True))
        self.keep_open = bool(config.get("keep_open", True))
        self.current_course: dict | None = None
        self.current_ware_total = 1
        self._progress_dump_done = False

    # ---------- 与界面通信 ----------
    def log(self, text: str) -> None:
        line = f"[{now_text()}] {text}"
        self.out.put(("log", line))
        if self.log_file is not None:
            try:
                with self.log_file.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except Exception:
                pass

    def status(self, text: str) -> None:
        self.out.put(("status", text))

    def courses(self, rows: list) -> None:
        self.current_rows = rows
        self.out.put(("courses", rows))

    def alert(self, text: str) -> None:
        """需要用户配合的事情（防挂机验证、课堂小测等）。"""
        self.out.put(("alert", text))

    def stop(self) -> None:
        self.stop_flag.set()

    # ---------- 浏览器 ----------
    def _launch(self, pw):
        channel = str(self.config.get("browser_channel") or "chrome").strip().lower()
        PROFILE_DIR.mkdir(exist_ok=True)
        headless = self.headless or os.environ.get("JTZY_HEADLESS") == "1"
        candidates: list[str] = []
        for name in (channel, "chrome", "msedge", "chromium"):
            if name and name not in candidates:
                candidates.append(name)
        # 便携包里只带了后台版 chromium（headless shell），需要可见窗口时不能用它
        if not headless and "chromium" in candidates:
            candidates.remove("chromium")
        last_error = None
        for candidate in candidates:
            try:
                ctx = pw.chromium.launch_persistent_context(
                    user_data_dir=str(PROFILE_DIR),
                    channel=None if candidate == "chromium" else candidate,
                    headless=headless,
                    args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
                    no_viewport=True,
                    locale="zh-CN",
                )
                self.log(
                    f"浏览器已启动：{candidate}（{'后台隐藏' if headless else '可见窗口'}）"
                )
                return ctx
            except Exception as exc:
                last_error = exc
                self.log(f"启动 {candidate} 失败：{exc}")
        if not headless:
            self.log("没有可用的可见浏览器：请先安装 Chrome 或 Edge")
        raise RuntimeError(f"无法启动浏览器：{last_error}")

    def _attach(self, page) -> None:
        def on_dialog(dialog) -> None:
            message = dialog.message
            if message not in self.dialogs:
                self.dialogs.append(message)
                self.log(f"平台弹窗（{dialog.type}）：{message}")
            try:
                dialog.accept()
            except Exception:
                pass

        try:
            page.on("dialog", on_dialog)
        except Exception:
            pass

    def _wait_idle(self, page, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end and not self.stop_flag.is_set():
            try:
                page.wait_for_timeout(500)
            except Exception:
                time.sleep(0.5)

    # ---------- 登录 ----------
    def _already_logged_in(self, page) -> bool:
        if "forgotPassword" in page.url:
            return False
        form_visible = False
        try:
            form_visible = (
                page.locator("#logname").is_visible()
                or page.locator("#username").is_visible()
                or page.locator("#pwd").is_visible()
            )
        except Exception:
            pass
        if form_visible:
            return False
        logout_visible = False
        for text in ("退出登录",):
            try:
                loc = page.get_by_text(text, exact=True)
                if loc.count() and loc.first.is_visible():
                    logout_visible = True
                    break
            except Exception:
                continue
        if logout_visible:
            self.log("检测到“退出登录”，确认已登录")
            return True
        url = page.url.lower()
        if "index.html" in url:
            return False
        if any(key in url for key in ("personalcenter", "basicinfo", "course")):
            self.log("页面已进入课程/个人中心，确认已登录")
            return True
        self.log(f"登录状态检查：表单={form_visible} 退出={logout_visible} 地址={page.url}")
        return False

    def _ocr_image(self, image_path: Path) -> str:
        """Use the built-in Windows OCR engine for a captcha image."""
        script = r"""
$ErrorActionPreference='Stop'
$OutputEncoding=[Console]::OutputEncoding=[Text.Encoding]::UTF8
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($WinRtTask, $ResultType) {
  $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
  $netTask = $asTask.Invoke($null, @($WinRtTask))
  $netTask.Wait(-1) | Out-Null
  $netTask.Result
}
[Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
$path = __IMAGE_PATH__
$big = __BIG_PATH__
# 验证码原图只有 100x40，放大 4 倍并做双三次插值，OCR 准确率会高很多
$src = [System.Drawing.Image]::FromFile($path)
try {
  $bmp = New-Object System.Drawing.Bitmap(($src.Width * 4), ($src.Height * 4))
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
  $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
  $g.DrawImage($src, 0, 0, $bmp.Width, $bmp.Height)
  $g.Dispose()
  $bmp.Save($big, [System.Drawing.Imaging.ImageFormat]::Png)
  $bmp.Dispose()
  $path = $big
} finally { $src.Dispose() }
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if (-not $engine) { exit 3 }
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
Write-Output $result.Text
"""
        quoted = "'" + str(image_path).replace("'", "''") + "'"
        script = script.replace("__IMAGE_PATH__", quoted)
        big_path = image_path.with_name(image_path.stem + "_big.png")
        script = script.replace(
            "__BIG_PATH__", "'" + str(big_path).replace("'", "''") + "'"
        )
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        powershell = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.run(
                [str(powershell), "-NoProfile", "-EncodedCommand", encoded],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                creationflags=creationflags,
            )
        except Exception:
            return ""
        text = (proc.stdout or "").strip()
        if text.startswith("#< CLIXML"):
            text = ""
        return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", text)

    def _read_captcha(self, page, selector: str = "#CCode") -> str:
        """截图 + 放大后 OCR。识别不理想就点验证码换一张再认。"""

        def capture() -> str:
            try:
                page.wait_for_function(
                    """(selector) => {
                         const image = document.querySelector(selector);
                         return image && image.complete && image.naturalWidth > 0;
                       }""",
                    selector,
                    timeout=5000,
                )
            except Exception:
                pass
            path = Path(tempfile.gettempdir()) / "jtzy_captcha.png"
            try:
                image.screenshot(path=str(path))
            except Exception:
                return ""
            return re.sub(r"[^0-9A-Za-z]", "", self._ocr_image(path))

        try:
            image = page.locator(selector)
            if not image.count() or not image.is_visible():
                return ""
            best = ""
            best_len = 0
            for round_no in range(1, 4):
                page.wait_for_timeout(500)
                code = capture()
                # 平台识别码固定 4 位，4 位直接采信
                if len(code) == 4:
                    self.log("识别码自动识别成功")
                    return code
                if len(code) >= best_len:
                    best, best_len = code, len(code)
                if round_no < 3:
                    try:
                        image.click()
                    except Exception:
                        pass
                    page.wait_for_timeout(1000)
            if best:
                self.log(f"验证码识别不确定（{len(best)} 位），按最长结果尝试")
            else:
                self.log("验证码识别为空")
            return best
        except Exception as exc:
            self.log(f"识别码自动识别失败：{exc}")
            return ""

    def _login(self, page) -> bool:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        if self._already_logged_in(page):
            self.log("浏览器里已有登录状态，跳过登录")
            return True

        username = self.config.get("username", "").strip()
        password = self.config.get("password", "")
        settle_end = time.time() + 20
        while time.time() < settle_end and not self.stop_flag.is_set():
            if self._already_logged_in(page):
                self.log("检测到已有登录状态，跳过登录")
                return True
            if page.locator("#username").is_visible() or page.locator("#logname").is_visible():
                break
            page.wait_for_timeout(1000)
        for attempt in range(1, 4):
            if self.stop_flag.is_set():
                return False
            try:
                modal = page.locator("#logname")
                main = page.locator("#username")
                if not modal.is_visible() and not main.is_visible():
                    for text in ("登录", "登 录"):
                        try:
                            trigger = page.get_by_text(text, exact=True).first
                            if trigger.is_visible():
                                trigger.click()
                                page.wait_for_timeout(2000)
                                break
                        except Exception:
                            continue
                modal_visible = page.locator("#logname").is_visible()
                # 个人资料页也有 #username，必须同时有密码框才算登录表单
                main_visible = (
                    page.locator("#username").is_visible()
                    and page.locator("#pwd").is_visible()
                )
                if not modal_visible and not main_visible:
                    if self._already_logged_in(page):
                        self.log("检测到已有登录状态，跳过登录")
                        return True
                    self.log("登录框尚未出现，等待后重试")
                    page.wait_for_timeout(2500)
                    continue
                if main_visible:
                    if attempt > 1:
                        # 上一次提交后原验证码已作废，先换一张新图再识别
                        try:
                            page.locator("#CCode").click()
                            page.wait_for_timeout(900)
                        except Exception:
                            pass
                    captcha = self._read_captcha(page, "#CCode")
                    if not captcha:
                        if self._already_logged_in(page):
                            self.log("验证码框已消失，检测到已登录，继续")
                            return True
                        self.log(f"第 {attempt} 次登录：验证码没识别出来，换一张重试")
                        continue
                else:
                    captcha = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", page.locator("#checkCode").inner_text().strip())
                self.log(
                    f"第 {attempt} 次登录：识别码{'已识别' if captcha else '为空'}"
                    f"（弹窗={modal_visible}，主表单={main_visible}）"
                )
                clicked = False
                if modal_visible:
                    page.fill("#logname", username)
                    page.fill("#logpwd", password)
                    page.fill("#logvaricode", captcha)
                    links = page.locator("a.login_btn")
                    for index in range(links.count()):
                        element = links.nth(index)
                        if element.is_visible():
                            element.click()
                            clicked = True
                            break
                else:
                    page.fill("#username", username)
                    page.fill("#pwd", password)
                    page.fill("#code", captcha)
                    controls = page.locator("button, a, input[type=submit]")
                    for index in range(controls.count()):
                        element = controls.nth(index)
                        text = ""
                        try:
                            text = element.inner_text().strip()
                        except Exception:
                            pass
                        value = element.get_attribute("value") or ""
                        if "".join((text or value).split()) == "登录" and element.is_visible():
                            element.click()
                            clicked = True
                            break
                if not clicked:
                    self.log("没有找到可见的“登录”按钮，转为手动登录")
                    break
                # 平台跳转有时较慢，轮询等待，避免把“已登录但还没跳转”误判为失败
                for _ in range(10):
                    page.wait_for_timeout(1000)
                    if self._already_logged_in(page):
                        self.log(f"登录成功：{page.url}")
                        return True
                self.log("登录未通过，重试")
            except Exception as exc:
                self.log(f"登录异常：{exc}")
                page.wait_for_timeout(1500)

        self.log("自动登录失败。请在浏览器窗口里手动输入识别码登录，软件会等你 5 分钟。")
        self.status("等待手动登录…")
        end = time.time() + 300
        while time.time() < end and not self.stop_flag.is_set():
            page.wait_for_timeout(2000)
            if self._already_logged_in(page):
                self.log("检测到已登录，继续")
                return True
        return False

    # ---------- 课程列表 ----------
    def _read_bought_courses_api(self, page) -> list:
        """Read the platform's real “我的课程” list (including learnrate)."""
        try:
            page.add_script_tag(url=MY_COURSE_INIT_URL)
            page.wait_for_timeout(2500)
        except Exception:
            pass
        try:
            data = page.evaluate(
                """() => new Promise(resolve => {
                     try {
                       require(['../data/commonTool'], function(ct) {
                         ct.getUserBean(function(u) {
                           if (!u) return resolve(null);
                           const bean = {
                             userid: u.id,
                             pageSize: '50',
                             pageNum: 1,
                             cardno: u.idcardno,
                             cardtype: u.idcardtype,
                             studentno: u.studentno,
                             coursename: '',
                             coursetype: '',
                             coursestate: '3'
                           };
                           ct.request('getBoughtCourseList', {bean: bean}, function(res) {
                             resolve(res || null);
                           });
                         });
                       }, function() { resolve(null); });
                     } catch (e) { resolve(null); }
                   })"""
            )
        except Exception:
            return []
        if not isinstance(data, dict):
            return []
        result = data.get("result")
        if isinstance(result, dict) and isinstance(result.get("result"), dict):
            result = result.get("result")
        items = result.get("result") if isinstance(result, dict) else result
        if not isinstance(items, list):
            return []
        rows = []
        for item in items:
            if not isinstance(item, dict):
                continue
            cid = item.get("id") or item.get("classid") or ""
            title = item.get("coursename") or item.get("classname") or ""
            if not (cid or title):
                continue
            raw_progress = item.get("learnrateII")
            if raw_progress is None or raw_progress == "":
                raw_progress = item.get("learnrate")
            pct, has_value = parse_progress(raw_progress)
            if not has_value:
                # 平台没给进度数字时才用课程状态兜底，避免“状态已完成”盖掉真实进度
                pct = 100 if str(item.get("coursestate") or "") == "1" else 0
            progress = f"{pct}%"
            if not self._progress_dump_done:
                self.log(
                    f"进度字段：{title} learnrateII={item.get('learnrateII')!r} "
                    f"learnrate={item.get('learnrate')!r} coursestate={item.get('coursestate')!r} "
                    f"coursestatename={item.get('coursestatename')!r} -> {pct}%"
                )
            rows.append({
                "title": title,
                "hours": item.get("coursetimelength") or "",
                "progress": progress,
                "progress_value": pct,
                "courseid": cid,
                "href": item.get("href") or (f"courseDetail.html?courseid={cid}" if cid else ""),
                "status": item.get("coursestatename") or "",
                "category": item.get("coursetypename") or "",
            })
        if rows:
            self._progress_dump_done = True
            self.log(f"我的课程接口读取到 {len(rows)} 门已购课程")
        return rows

    def _read_courses(self, page) -> list:
        api_rows = self._read_bought_courses_api(page)
        if api_rows:
            return api_rows
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        opened_by_click = False
        try:
            link = page.locator('a[href*="myCourse.html"]').first
            if link.count() and link.is_visible():
                link.click()
                opened_by_click = True
        except Exception:
            opened_by_click = False
        if not opened_by_click:
            page.goto(MY_COURSES_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4500)
        try:
            page.wait_for_function(
                """() => {
                     const vm = window.avalon && avalon.vmodels && avalon.vmodels.myCollection;
                     return vm && Array.isArray(vm.boughtcourseList) && vm.boughtcourseList.length > 0;
                   }""",
                timeout=20000,
            )
        except Exception:
            pass
        rows: list = []
        seen = set()
        try:
            page_count = page.evaluate(
                """() => {
                     const vm = window.avalon && avalon.vmodels;
                     if (!vm) return 1;
                     for (const key of Object.keys(vm)) {
                       const model = vm[key];
                       if (model && Array.isArray(model.pagelist) && model.pagelist.length) {
                         return model.pagelist.length;
                       }
                     }
                     return 1;
                   }"""
            ) or 1
        except Exception:
            page_count = 1
        page_count = int(page_count)
        for page_no in range(1, page_count + 1):
            if page_no > 1:
                before_first = page.evaluate(
                    """() => {
                         const vm = window.avalon && avalon.vmodels && avalon.vmodels.myCollection;
                         const first = vm && vm.boughtcourseList && vm.boughtcourseList[0];
                         return first ? (first.classid || first.id || '') : '';
                       }"""
                )
                clicked = False
                controls = page.locator('a[avalon-events*="jump"]')
                for index in range(controls.count()):
                    element = controls.nth(index)
                    text = ""
                    try:
                        text = element.inner_text().strip()
                    except Exception:
                        pass
                    if text == str(page_no) and element.is_visible():
                        element.click()
                        clicked = True
                        break
                if not clicked:
                    self.log(f"未找到第 {page_no} 页的分页按钮，停止翻页")
                    break
                for _ in range(20):
                    page.wait_for_timeout(500)
                    after_first = page.evaluate(
                        """() => {
                             const vm = window.avalon && avalon.vmodels && avalon.vmodels.myCollection;
                             const first = vm && vm.boughtcourseList && vm.boughtcourseList[0];
                             return first ? (first.classid || first.id || '') : '';
                           }"""
                    )
                    if after_first and after_first != before_first:
                        break
            found = page.evaluate(
                """
                () => {
                  const vm = window.avalon && avalon.vmodels && avalon.vmodels.myCollection;
                  const list = vm && vm.boughtcourseList;
                  if (!list) return [];
                  const readPct = (c) => {
                    let v = (c.learnrateII !== undefined && c.learnrateII !== null && c.learnrateII !== '')
                      ? c.learnrateII : c.learnrate;
                    if (v === undefined || v === null || v === '') return 0;
                    if (typeof v === 'number') return v <= 1 ? Math.round(v * 100) : Math.round(v);
                    const s = String(v).trim();
                    if (!s) return 0;
                    const hasPct = s.indexOf('%') >= 0;
                    const num = parseFloat(s.replace(/[^0-9.]/g, ''));
                    if (!isFinite(num)) return 0;
                    if (!hasPct && s.indexOf('.') >= 0 && num <= 1) return Math.round(num * 100);
                    return Math.round(num);
                  };
                  return list.map(c => ({
                    title: c.classname || c.coursename || '',
                    hours: c.classtime || c.coursetimelength || '',
                    progress: (c.learnrateII !== undefined && c.learnrateII !== null && c.learnrateII !== '')
                      ? c.learnrateII : (c.learnrate || ''),
                    progress_value: readPct(c),
                    courseid: c.classid || c.id || '',
                    href: c.href || '',
                    status: c.coursestatename || '',
                    category: c.coursetypename || ''
                  }));
                }
                """
            )
            if not found:
                found = page.evaluate(
                    """
                    () => {
                      const vm = window.avalon && avalon.vmodels && avalon.vmodels.index;
                      const list = vm && vm.courselist;
                      if (!list) return [];
                      return list.map(c => ({
                        title: c.coursename || '',
                        hours: c.coursetimelength || '',
                        progress: '',
                        progress_value: 0,
                        courseid: c.id || '',
                        href: c.jumplink || ('courseDetail.html?courseid=' + (c.id || '')),
                        status: '',
                        category: c.coursetypename || ''
                      }));
                    }
                    """
                )
            for row in found:
                key = row.get("courseid") or row.get("title", "")
                if not key or key in seen:
                    continue
                seen.add(key)
                rows.append(row)
        if rows:
            self.log(f"已读取 {len(rows)} 门课程（共 {page_count} 页）")
        else:
            self.log("未读取到课程列表，正在确认登录状态")
            try:
                if page.locator("#username").is_visible() or page.locator("#pwd").is_visible():
                    self.log("当前页面仍是登录页，登录状态已失效")
            except Exception:
                pass
        return rows

    def _course_progress(self, home, course: dict) -> int:
        """读取单门课程在“我的课程”里的最新进度（0-100）。"""
        title = course.get("title") or ""
        if not title:
            return 0
        try:
            home.goto(MY_COURSES_URL, wait_until="domcontentloaded", timeout=60000)
            home.wait_for_timeout(3000)
            home.fill("#boughtcoursename", title)
            home.evaluate(
                """() => {
                     const vm = window.avalon && avalon.vmodels && avalon.vmodels.myCollection;
                     if (vm && vm.search) vm.search('buyCourse');
                   }"""
            )
            home.wait_for_timeout(2500)
            value = home.evaluate(
                """(title) => {
                     const vm = window.avalon && avalon.vmodels && avalon.vmodels.myCollection;
                     const list = vm && vm.boughtcourseList ? vm.boughtcourseList : [];
                     const row = list.find(c => (c.classname || c.coursename) === title);
                     return row ? (row.learnrateII || row.learnrate || '') : '';
                   }""",
                title,
            )
            pct, has_value = parse_progress(value)
            return pct if has_value else 0
        except Exception as exc:
            self.log(f"读取《{title}》最新进度失败：{exc}")
            return 0

    def _course_progress_now(self, home, course: dict) -> int:
        """优先从“我的课程”接口读取最新进度，失败再回退到页面搜索。"""
        try:
            rows = self._read_bought_courses_api(home)
            wanted = str(course.get("courseid") or "")
            title = course.get("title") or ""
            for row in rows:
                if wanted and str(row.get("courseid") or "") == wanted:
                    return int(row.get("progress_value") or 0)
                if title and row.get("title") == title:
                    return int(row.get("progress_value") or 0)
        except Exception:
            pass
        return self._course_progress(home, course)

    # ---------- 播放器 ----------
    def _video_state(self, page) -> dict:
        try:
            return page.evaluate(
                """
                () => {
                  const v = document.querySelector('video');
                  const tip = document.querySelector('.main_tip');
                  const tipOn = tip ? (getComputedStyle(tip).display !== 'none' && tip.offsetHeight > 0) : false;
                  const q = document.querySelector('#question, .question_box, .ans_list, #reflashQuestion');
                  return {
                    hasVideo: !!v,
                    t: v ? v.currentTime : null,
                    d: v && isFinite(v.duration) ? v.duration : null,
                    ended: v ? v.ended : false,
                    paused: v ? v.paused : null,
                    rate: v ? v.playbackRate : null,
                    ready: v ? v.readyState : null,
                    idleTip: tipOn,
                    question: q ? (q.innerText || '').slice(0, 160) : '',
                    msg: (document.querySelector('#showMsg') || {}).innerText || ''
                  };
                }
                """
            )
        except Exception as exc:
            return {"hasVideo": False, "error": str(exc)}

    def _play(self, page) -> None:
        try:
            page.evaluate(
                """(rate) => {
                  const v = document.querySelector('video');
                  if (!v) return;
                  v.muted = true;
                  v.defaultPlaybackRate = rate;
                  v.playbackRate = rate;
                  const p = v.play();
                  if (p && p.catch) p.catch(() => {});
                }""",
                self.playback_rate,
            )
        except Exception:
            pass

    # ---------- 单个课件 ----------
    def _study_ware(self, home, ware_index: int, ware_name: str) -> str:
        ctx = home.context
        for attempt in range(1, 5):
            if self.stop_flag.is_set():
                return "stopped"
            before = set(ctx.pages)
            before_url = home.url
            self.dialogs.clear()
            try:
                home.locator("ol.img-list > li > a").nth(ware_index).click()
            except Exception as exc:
                self.log(f"点击课件失败：{exc}")
                return "error"

            player = None
            new_pages = []
            end = time.time() + 30
            while time.time() < end and not self.stop_flag.is_set():
                home.wait_for_timeout(1000)
                for pg in ctx.pages:
                    if pg in before:
                        continue
                    if pg not in new_pages:
                        new_pages.append(pg)
                        try:
                            self.log(f"检测到新页面：{pg.url}")
                        except Exception:
                            pass
                    try:
                        if pg.is_closed():
                            continue
                        url = pg.url or ""
                        if "videonew.html" in url or "video" in url.lower():
                            player = pg
                            break
                        if pg.locator("video").count() > 0:
                            player = pg
                            break
                    except Exception:
                        continue
                try:
                    if not player and home.url != before_url:
                        if "videonew.html" in home.url or "video" in home.url.lower():
                            player = home
                        elif home.locator("video").count() > 0:
                            player = home
                except Exception:
                    pass
                if player:
                    break

            if not player and new_pages:
                for pg in new_pages:
                    try:
                        if not pg.is_closed():
                            player = pg
                            self.log(f"使用新页面作为播放器：{pg.url}")
                            break
                    except Exception:
                        continue

            if not player:
                if any("已经存在打开的页面" in d for d in self.dialogs):
                    self.log(f"平台提示已有打开的学习页面，等待 100 秒后重试（第 {attempt} 次）")
                    self.status("等待学习锁释放…")
                    self._wait_idle(home, 100)
                    continue
                self.log(f"未能打开播放器窗口，弹窗记录：{self.dialogs}")
                return "error"

            self._attach(player)
            failed = False
            try:
                player.wait_for_timeout(3000)
                resumed = False
                for resume_text in ("继续播放", "继续上次", "继续观看", "继续学习"):
                    continue_btn = player.get_by_text(resume_text, exact=True)
                    if continue_btn.count() and continue_btn.first.is_visible():
                        try:
                            tip = player.locator("#msg_tip").inner_text()
                        except Exception:
                            tip = "检测到上次学习断点"
                        self.log(f"断点续播（{resume_text}）：{tip}")
                        continue_btn.first.click()
                        player.wait_for_timeout(1500)
                        resumed = True
                        break
                if resumed:
                    state0 = self._video_state(player)
                    if state0.get("t") is not None:
                        self.log(f"从上次位置继续：{mmss(state0.get('t'))}")

                state = {}
                for _ in range(30):
                    if self.stop_flag.is_set():
                        break
                    state = self._video_state(player)
                    if state.get("hasVideo") and state.get("d"):
                        break
                    player.wait_for_timeout(2000)
                if self.stop_flag.is_set():
                    self.log("收到停止指令，本课件未开始播放")
                else:
                    self.log(f"开始播放：{ware_name}（时长 {mmss(state.get('d'))}）")
                    self.log(f"播放倍速：{self.playback_rate:g}x（按平台真实播放进度累计）")
                    try:
                        src = player.evaluate(
                            """() => {
                                 const v = document.querySelector('video');
                                 if (!v) return '';
                                 return v.currentSrc || v.src ||
                                        (v.querySelector('source') ? v.querySelector('source').src : '');
                               }"""
                        )
                        if src:
                            self.log(f"视频地址：{str(src)[:200]}")
                    except Exception:
                        pass
                    self._play(player)

                warned_idle = False
                last_log = 0.0
                no_video_since = None
                idle_alert_at = 0.0
                no_duration_since = None
                failed = False
                last_t = None
                last_progress_at = time.time()
                stall_count = 0
                while not self.stop_flag.is_set():
                    state = self._video_state(player)
                    if state.get("error"):
                        self.log(f"播放器状态读取失败：{state['error']}")
                        failed = True
                        break
                    if "未获取到播放资源" in (state.get("msg") or ""):
                        self.log("播放器提示“未获取到播放资源”，本课件加载失败")
                        failed = True
                        break
                    if not state.get("hasVideo"):
                        if no_video_since is None:
                            no_video_since = time.time()
                        elif time.time() - no_video_since > 300:
                            self.log("播放器超过 5 分钟没有视频元素，判定异常，退出本课件")
                            failed = True
                            break
                    else:
                        no_video_since = None
                    if state.get("hasVideo") and not state.get("d"):
                        if no_duration_since is None:
                            no_duration_since = time.time()
                        elif time.time() - no_duration_since > 180:
                            self.log("视频超过 3 分钟仍拿不到时长（资源没加载出来），判定失败")
                            failed = True
                            break
                    else:
                        no_duration_since = None
                    t = state.get("t")
                    d = state.get("d")
                    if state.get("ended") or (d and t is not None and t >= d - 1.5):
                        self.log(f"课件播放完成：{ware_name}")
                        break
                    if d and t is not None and last_t is not None and last_t > d - 60 and t < last_t - 10:
                        self.log(
                            f"检测到视频回到开头（{mmss(last_t)} → {mmss(t)}），判定课件完成：{ware_name}"
                        )
                        break
                    now = time.time()
                    if t is not None:
                        if last_t is None or t > last_t + 0.5:
                            last_progress_at = now
                            stall_count = 0
                        elif d and now - last_progress_at > 90:
                            stall_count += 1
                            self.log(
                                f"播放进度 {mmss(t)} 已超过 90 秒未推进，"
                                f"尝试恢复（第 {stall_count}/2 次）"
                            )
                            try:
                                player.evaluate(
                                    """() => {
                                         const v = document.querySelector('video');
                                         if (!v) return;
                                         try {
                                           v.currentTime = Math.min(
                                             v.currentTime + 0.05,
                                             Math.max(v.duration - 0.2, 0)
                                           );
                                         } catch (e) {}
                                         const p = v.play();
                                         if (p && p.catch) p.catch(() => {});
                                       }"""
                                )
                            except Exception:
                                pass
                            self._play(player)
                            last_progress_at = now
                            if stall_count >= 2:
                                self.log("播放进度多次无法恢复，退出本课件；下一次运行会自动续播。")
                                failed = True
                                break
                        last_t = t
                    if state.get("idleTip"):
                        if not warned_idle:
                            self.log("平台提示‘亲，还在吗?’，视频已暂停：请在浏览器窗口里移动鼠标或按任意键，软件会自动继续。")
                            self.alert("请动一下鼠标或按任意键：平台弹出‘亲，还在吗?’，视频已暂停")
                            warned_idle = True
                            idle_alert_at = time.time()
                        elif time.time() - idle_alert_at > 120:
                            idle_alert_at = time.time()
                            self.alert("视频仍处于暂停：请到浏览器窗口动一下鼠标或按任意键")
                    elif warned_idle:
                        warned_idle = False
                        self.log("检测到操作，继续播放")
                    if state.get("question"):
                        self.log(f"平台弹出课堂小测，请手动作答：{state['question'][:80]}")
                        self.alert("请手动作答：平台弹出课堂小测")
                    elif state.get("paused") and (state.get("ready") or 0) >= 2:
                        self._play(player)
                    elif state.get("rate") is not None and abs(float(state["rate"]) - self.playback_rate) > 0.05:
                        self._play(player)

                    now = time.time()
                    if now - last_log >= 60:
                        last_log = now
                        pct = (state.get("t") or 0) / state["d"] * 100 if state.get("d") else 0
                        self.log(f"播放中 {mmss(state.get('t'))} / {mmss(state.get('d'))}（{pct:.1f}%）")
                        self.status(f"正在学习：{ware_name} {pct:.1f}%")
                        try:
                            course = self.current_course or {}
                            cid = course.get("courseid") or course.get("id") or ""
                            total = max(int(self.current_ware_total or 1), 1)
                            overall = (
                                (ware_index + min(max(pct, 0.0), 100.0) / 100.0) / total
                            ) * 100.0
                            self.out.put(
                                ("progress", {"courseid": cid, "pct": overall, "ware": ware_name})
                            )
                        except Exception:
                            pass
                    player.wait_for_timeout(1000)
            except Exception as exc:
                self.log(f"播放异常：{exc}")
                # 页面被平台关掉、播放器丢失等都属于本课件没播成，必须让上层重试，
                # 否则会被当成“已处理”直接跳到下一节。
                if not self.stop_flag.is_set():
                    failed = True

            # 学完必须点“安全退出”，平台才会落库学时
            try:
                self.log("点击“安全退出”保存学习记录")
                try:
                    player.get_by_text("安全退出", exact=True).click(timeout=8000, force=True)
                except Exception:
                    player.evaluate(
                        """() => {
                             const nodes = Array.from(document.querySelectorAll('a, button'));
                             const target = nodes.find(
                               e => (e.innerText || '').trim() === '安全退出'
                             );
                             if (target) target.click();
                           }"""
                    )
                try:
                    player.wait_for_timeout(2500)
                except Exception:
                    pass  # 页面已关闭，说明退出流程已完成
            except Exception as exc:
                self.log(f"安全退出按钮点击异常：{exc}")
            try:
                if not player.is_closed():
                    player.close()
            except Exception:
                pass
            if failed and not self.stop_flag.is_set():
                return "error"
            return "stopped" if self.stop_flag.is_set() else "ok"
        return "error"

    # ---------- 单门课程 ----------
    @staticmethod
    def _norm_question(text: str) -> str:
        text = re.sub(r"<[^>]+>", " ", text or "")
        return re.sub(r"[\s（）()。，,、：:；;\"'“”‘’_]+", "", text).strip()

    def _load_answer_key(self) -> dict:
        path = BASE_DIR / "答案库.json"
        try:
            key = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            if not isinstance(exc, FileNotFoundError):
                self.log(f"答案库读取失败：{exc}")
            key = {}
        # 随包附带的公共答案库只补本地缺的题，不覆盖本机已经确认的答案
        for shared_path in (PORTABLE_ROOT / "公共答案库.json", BASE_DIR / "公共答案库.json"):
            try:
                if not shared_path.exists():
                    continue
                shared = json.loads(shared_path.read_text(encoding="utf-8"))
            except Exception as exc:
                self.log(f"公共答案库读取失败：{exc}")
                continue
            added = 0
            for question, answer in (shared or {}).items():
                if answer and question not in key:
                    key[question] = answer
                    added += 1
            if added:
                self.log(f"已从公共答案库补充 {added} 题标准答案")
                self._save_answer_key(key)
            break
        return key

    def _save_answer_key(self, key: dict) -> None:
        path = BASE_DIR / "答案库.json"
        try:
            path.write_text(json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            self.log(f"答案库写入失败：{exc}")

    @staticmethod
    def _read_exam_script() -> str:
        return """() => {
             const vm = window.avalon && avalon.vmodels && avalon.vmodels.exercise;
             if (!vm) return [];
             const norm = s => (s || '').replace(/<[^>]+>/g, ' ');
             const out = [];
             ['single', 'multi', 'judge'].forEach(kind => {
               (vm[kind + 'List'] || []).forEach((q, i) => out.push({
                 kind: kind, idx: i, question: norm(q.questionname),
                  options: (q.option || []).map((o, j) => ({
                    i: j,
                    text: norm(o.content || o.optionname || o.name || o.text || ''),
                    value: o.value || o.optionvalue || ''
                  })),
               }));
             });
             return out;
           }"""

    def _read_exam_items(self, page, timeout_s: float = 15.0) -> list:
        """试卷页的 avalon 试题模型是异步渲染的，这里轮询等它出现。"""
        script = self._read_exam_script()
        end = time.time() + timeout_s
        items: list = []
        while time.time() < end and not self.stop_flag.is_set():
            try:
                items = page.evaluate(script) or []
            except Exception as exc:
                self.log(f"读取试题失败：{exc}")
                items = []
            if items:
                return items
            try:
                page.wait_for_timeout(1000)
            except Exception:
                time.sleep(1)
        return items

    def _fill_exam(self, page) -> tuple[bool, bool]:
        """答案库优先；缺答案时先用占位答案触发判卷，下一轮再用标准答案重答。

        返回 (是否已交卷, 是否读到过试题)。试卷页偶发渲染不出题目，
        调用方据此决定要不要刷新页面重试。
        """
        key = self._load_answer_key()
        items = self._read_exam_items(page)
        if not items:
            self.log("答题页还没加载出题目，稍后再试")
            return False, False

        filled = 0
        missing: list[dict] = []
        for item in items:
            answer = key.get(self._norm_question(item.get("question", "")))
            if not answer:
                missing.append(item)
                continue
            ok = False
            for letter in answer:
                ok = self._click_letter(page, item, letter)
                if not ok:
                    break
            if ok:
                filled += 1
            else:
                missing.append(item)

        if missing:
            if self.use_ai:
                for uid, item in enumerate(missing, 1):
                    item["_uid"] = uid
                ai_answers = self._ai_answer(missing)
                for item in missing:
                    answer = ai_answers.get(int(item.get("_uid", -1)))
                    if not answer:
                        continue
                    key[self._norm_question(item.get("question", ""))] = answer
                    ok = False
                    for letter in answer:
                        ok = self._click_letter(page, item, letter)
                        if not ok:
                            break
                    if ok:
                        filled += 1
                self._save_answer_key(key)
            else:
                self.log(f"答案库缺少 {len(missing)} 题，先用占位答案触发平台判卷")
                for item in missing:
                    if self._click_letter(page, item, "A"):
                        filled += 1

        self.log(f"已自动填写 {filled}/{len(items)} 题")
        if filled < len(items):
            self.log(f"仍有 {len(items) - filled} 题未填，不自动提交，请手动处理")
            return False, True
        return self._submit_exam(page), True

    def _answer_value(self, item: dict, letter: str) -> str:
        option_index = ord(letter.upper()) - ord("A")
        options = item.get("options") or []
        if 0 <= option_index < len(options):
            value = str(options[option_index].get("value") or "").strip()
            if value:
                return value
        return letter.upper()

    def _click_letter(self, page, item: dict, letter: str) -> bool:
        option_index = ord(letter.upper()) - ord("A")
        options = item.get("options") or []
        option = options[option_index] if 0 <= option_index < len(options) else {}
        value = self._answer_value(item, letter)
        text = str(option.get("text") or "").strip()
        try:
            return bool(page.evaluate(
                """([kind, idx, value, text, optionIndex]) => {
                     const suffix = kind === 'single' ? 'select' : kind === 'multi' ? 'multis' : 'jud';
                     const clickInput = input => {
                       if (!input) return false;
                       try { input.scrollIntoView({block: 'center'}); } catch (e) {}
                       try { input.click(); } catch (e) {}
                       if (!input.checked) {
                         input.checked = true;
                         input.dispatchEvent(new Event('input', {bubbles: true}));
                         input.dispatchEvent(new Event('change', {bubbles: true}));
                       }
                       if (input.id) {
                         const label = document.querySelector('label[for="' + input.id + '"]');
                         if (label) label.click();
                       }
                       const holder = input.closest('li') || input.parentElement;
                       if (holder && !input.checked) holder.click();
                       return !!input.checked;
                     };
                     const exact = document.querySelector(
                       'input[name="' + idx + suffix + '"][value="' + value + '"]');
                     if (clickInput(exact)) return true;
                     const candidates = Array.from(document.querySelectorAll('input[name$="' + suffix + '"]'));
                     if (text) {
                       const byText = candidates.find(input => {
                         const holder = input.closest('li') || input.parentElement || input;
                         return (holder.innerText || '').replace(/\\s+/g, '').includes(
                           text.replace(/\\s+/g, ''));
                       });
                       if (clickInput(byText)) return true;
                       const label = Array.from(document.querySelectorAll('label,li,span'))
                         .find(el => (el.innerText || '').replace(/\\s+/g, '').includes(
                           text.replace(/\\s+/g, '')));
                       if (label) { label.click(); return true; }
                     }
                     const sameQuestion = candidates.filter(input =>
                       (input.name || '').startsWith(String(idx)));
                     if (sameQuestion[optionIndex]) return clickInput(sameQuestion[optionIndex]);
                     return false;
                   }""",
                [item.get("kind"), item.get("idx"), value, text, option_index],
            ))
        except Exception:
            return False

    def _ai_answer(self, items: list[dict]) -> dict[int, str]:
        cfg = self.config
        base_url = str(cfg.get("ai_base_url") or "http://127.0.0.1:15721/v1").rstrip("/")
        model = str(cfg.get("ai_model") or "deepseek-v4-flash")
        if not base_url.startswith(("http://", "https://")):
            self.log("AI 答题未配置有效接口，跳过")
            return {}
        payload_items = []
        for item in items:
            options = []
            for option in item.get("options") or []:
                letter = chr(ord("A") + int(option.get("i", 0)))
                options.append({"letter": letter, "text": option.get("text", "")})
            payload_items.append({
                "index": int(item.get("_uid", -1)),
                "type": item.get("kind", ""),
                "question": item.get("question", ""),
                "options": options,
            })
        result: dict[int, str] = {}
        for start in range(0, len(payload_items), 8):
            batch = payload_items[start:start + 8]
            prompt = (
                "你是交通职业资格继续教育考试答题助手。根据题干和选项作答，"
                "只返回 JSON 数组，不要解释。每项格式："
                '{"index":题目序号,"answers":["A"]}。'
                "单选返回一个字母，多选返回按字母排序的数组，判断题为 A/B。\n"
                + json.dumps(batch, ensure_ascii=False)
            )
            body = json.dumps({
                "model": model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": "你只输出可解析的 JSON。"},
                    {"role": "user", "content": prompt},
                ],
            }, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                f"{base_url}/chat/completions",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=80) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
                content = data["choices"][0]["message"]["content"]
                match = re.search(r"\[[\s\S]*\]", content)
                if not match:
                    raise ValueError("AI 未返回 JSON 数组")
                rows = json.loads(match.group(0))
                for row in rows:
                    letters = "".join(re.findall(r"[A-Z]", str(row.get("answers", "")))).upper()
                    if letters:
                        result[int(row.get("index", -1))] = "".join(sorted(set(letters)))
            except Exception as exc:
                self.log(f"AI 答题批次失败：{exc}")
        self.log(f"AI 已返回 {len(result)} 题答案")
        return result

    def _submit_exam(self, page) -> bool:
        """点击平台交卷按钮，并处理确认框。"""
        try:
            button = page.get_by_text("交卷", exact=True).first
            if not button.count() or not button.is_visible():
                self.log("未找到交卷按钮，未自动提交")
                return False
            button.click()
            page.wait_for_timeout(1200)
            for text in ("确定", "确认", "是"):
                try:
                    confirm = page.get_by_text(text, exact=True).first
                    if confirm.count() and confirm.is_visible():
                        confirm.click()
                        break
                except Exception:
                    continue
            self.log("已点击交卷并提交")
            return True
        except Exception as exc:
            self.log(f"自动交卷失败：{exc}")
            return False

    def _extract_standard_answers(self, home, course: dict, test: dict) -> int:
        """Open the platform answer-review page and merge standard answers."""
        href = test.get("href") or ""
        match = re.search(r"[?&]videoid=([^&]+)", href)
        video_id = match.group(1) if match else ""
        if not video_id:
            return 0
        course_id = course.get("courseid") or ""
        candidates = []
        for key in ("videoid", "paperid", "id"):
            candidates.append(f"{SITE_BASE}/courseAnswerProblem.html?{key}={video_id}&courseid={course_id}")
        candidates.append(f"{SITE_BASE}/courseAnswerProblem.html?videoid={video_id}")
        ctx = home.context
        for url in candidates:
            page = None
            try:
                page = ctx.new_page()
                self._attach(page)
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(5000)
                rows = page.evaluate(
                    """() => {
                         const vm = window.avalon && avalon.vmodels && avalon.vmodels.courseAnswerProblem;
                         if (!vm) return [];
                         const out = [];
                         ['single', 'multi', 'judge'].forEach(kind => {
                           (vm[kind + 'List'] || []).forEach(q => {
                             const question = (q.questionname || '').replace(/<[^>]+>/g, ' ').trim();
                             const answer = String(q.standardanswer || q.answer || '').trim().toUpperCase();
                             if (question && answer) out.push({ question, answer });
                           });
                         });
                         return out;
                       }"""
                ) or []
                if rows:
                    key = self._load_answer_key()
                    for row in rows:
                        answer = "".join(re.findall(r"[A-Z]", row.get("answer", ""))).upper()
                        if answer:
                            key[self._norm_question(row.get("question", ""))] = "".join(sorted(set(answer)))
                    self._save_answer_key(key)
                    self.log(f"已从平台判卷页提取并保存 {len(rows)} 题标准答案")
                    return len(rows)
            except Exception as exc:
                self.log(f"标准答案提取失败：{exc}")
            finally:
                try:
                    if page is not None and not page.is_closed():
                        page.close()
                except Exception:
                    pass
        self.log("未提取到平台标准答案")
        return 0

    def _pending_tests(self, home, course: dict) -> list:
        """读取课程详情页“在线自测”标签里的试卷列表，返回未通过的试卷。"""
        href = course.get("href") or f"courseDetail.html?courseid={course.get('courseid')}"
        if not str(href).startswith("http"):
            href = f"{SITE_BASE}/{str(href).lstrip('/')}"
        try:
            home.goto(href, wait_until="domcontentloaded", timeout=60000)
            home.wait_for_timeout(3000)
            tests = []
            for attempt in range(1, 4):
                if attempt > 1:
                    try:
                        home.reload(wait_until="domcontentloaded", timeout=60000)
                        home.wait_for_timeout(3000)
                    except Exception:
                        pass
                try:
                    home.evaluate(
                        """() => {
                             const vm = window.avalon && avalon.vmodels && avalon.vmodels.courseDetail;
                             if (vm && vm.tabChange) { vm.tabChange('onlineTest'); return true; }
                             return false;
                           }"""
                    )
                    for text in ("在线自测", "测试"):
                        try:
                            tab = home.get_by_text(text, exact=True).first
                            if tab.count() and tab.is_visible():
                                tab.click()
                                break
                        except Exception:
                            continue
                except Exception:
                    pass
                for _ in range(10):  # 等接口返回，最多 10 秒
                    home.wait_for_timeout(1000)
                    tests = home.evaluate(
                        """() => {
                             const vm = window.avalon && avalon.vmodels && avalon.vmodels.courseDetail;
                             const list = vm && vm.onlineTestList;
                             if (!list || !list.testname || !list.testname.length) return [];
                             return list.testname.map(t => ({
                               name: t.papername || '',
                               state: t.teststate || '',
                               href: t.href || '',
                             }));
                           }"""
                    ) or []
                    if tests:
                        break
                if tests:
                    break
        except Exception as exc:
            self.log(f"读取在线自测列表失败：{exc}")
            return []
        return [t for t in (tests or []) if t.get("state") != "已通过"]

    def _handle_tests(self, home, course: dict, wait_minutes: float = 10) -> None:
        """逐份未通过试卷：打开答题页并提醒人工作答，答完自动继续。"""
        pending = self._pending_tests(home, course)
        if not pending:
            self.log(f"《{course.get('title')}》没有待答的在线自测")
            return
        self.log(f"《{course.get('title')}》有 {len(pending)} 份在线自测未通过")
        ctx = home.context
        for test in pending:
            if self.stop_flag.is_set():
                return
            name = test.get("name") or "在线自测"
            href = test.get("href") or ""
            if not href:
                self.log(f"试卷《{name}》没有地址，跳过")
                continue
            if href.startswith("http"):
                url = href
            else:
                url = f"{SITE_BASE}/{href.lstrip('/')}"
            self.log(f"打开试卷《{name}》：{url}")
            if not self.answer_mode:
                self.alert(f"请手动作答：《{name}》，答完并提交后软件会自动继续")
            page = None
            try:
                page = ctx.new_page()
                self._attach(page)
                filled = False
                had_items = False
                for attempt in range(1, 4):
                    if self.stop_flag.is_set():
                        break
                    self.dialogs.clear()
                    try:
                        if attempt == 1:
                            page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        else:
                            # 试卷页偶发不带登录态或试题不渲染，刷新一次通常就正常了
                            self.log(f"《{name}》试卷没渲染出来，刷新页面重试（第 {attempt - 1} 次）")
                            try:
                                page.reload(wait_until="domcontentloaded", timeout=60000)
                            except Exception:
                                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    except Exception as exc:
                        self.log(f"答题页打开异常：{exc}")
                        page.wait_for_timeout(2000)
                        continue
                    page.wait_for_timeout(3000)
                    try:
                        page.bring_to_front()
                    except Exception:
                        pass
                    filled, had_items = self._fill_exam(page)
                    if filled or had_items:
                        break
                    page.wait_for_timeout(1500)
                if self.answer_mode:
                    if filled:
                        page.wait_for_timeout(5000)
                        self._extract_standard_answers(home, course, test)
                        left_now = self._pending_tests(home, course)
                        still_pending = any(
                            (x.get("name") == name)
                            or (x.get("href") == test.get("href"))
                            for x in left_now
                        )
                        if still_pending:
                            self.log(f"《{name}》提交后仍为未通过，将自动重答")
                        else:
                            self.log(f"《{name}》已确认通过")
                    else:
                        self.log(f"《{name}》答案未全部填入，跳过，稍后重试")
                    continue
                self.log("答题页已打开：请核对已勾选的答案，确认后点【提交】，软件会自动继续下一份")
                deadline = time.time() + wait_minutes * 60
                answered = False
                while time.time() < deadline and not self.stop_flag.is_set():
                    page.wait_for_timeout(5000)
                    try:
                        text = page.evaluate(
                            """() => {
                                 const box = document.querySelector('#exerciseResult');
                                 if (!box) return '';
                                 if (getComputedStyle(box).display === 'none') return '';
                                 const tds = box.querySelectorAll('td');
                                 return tds.length >= 3 ? tds[2].innerText.trim() : box.innerText.trim();
                               }"""
                        )
                    except Exception:
                        break
                    if not text:
                        continue
                    if "未通过" in text:
                        self.log("本次作答未通过，请再答一次（可以重新选择答案后再次提交）")
                        self.alert(f"《{name}》未通过，请再答一次")
                        deadline = time.time() + wait_minutes * 60
                        continue
                    if "通过" in text:
                        self.log(f"检测到《{name}》已通过（结果：{text}）")
                        answered = True
                        break
                if not answered:
                    self.log(f"《{name}》在 {wait_minutes:.0f} 分钟内没有完成，先跳过（下次运行会再提醒）")
            except Exception as exc:
                self.log(f"答题页异常：{exc}")
            finally:
                try:
                    if page is not None and not page.is_closed():
                        page.close()
                except Exception:
                    pass
        left = self._pending_tests(home, course)
        if left:
            self.log(f"《{course.get('title')}》仍有 {len(left)} 份自测未通过，稍后可再运行软件补答")
        else:
            self.log(f"《{course.get('title')}》在线自测全部通过")

    def _study_course(self, home, course: dict) -> None:
        href = course.get("href") or f"courseDetail.html?courseid={course.get('courseid')}"
        url = href if href.startswith("http") else f"{SITE_BASE}/{href.lstrip('/')}"
        home.goto(url, wait_until="domcontentloaded", timeout=60000)
        home.wait_for_timeout(3500)
        try:
            body = home.evaluate("() => document.body.innerText")
            match = re.search(r"总\s*学\s*时[:：]?\s*([0-9]+(?:\.[0-9]+)?)", body or "")
            if match:
                hours = float(match.group(1))
                cid = course.get("courseid") or ""
                if cid:
                    self.known_hours[cid] = hours
                course["hours"] = hours
                for row in self.current_rows:
                    if row.get("courseid") == cid:
                        row["hours"] = hours
                self.courses(self.current_rows)
                self.log(f"课程总学时：{hours:.2f}")
        except Exception:
            pass
        wares = home.evaluate(
            "() => Array.from(document.querySelectorAll('ol.img-list > li > a'))"
            ".map(a => (a.innerText || '').split('\\n')[0].trim())"
        )
        if not wares:
            self.log("该课程没有可见课件，跳过")
            return
        self.log(f"课程《{course.get('title')}》共 {len(wares)} 个课件")
        try:
            sample = home.evaluate(
                """() => {
                     const vm = window.avalon && avalon.vmodels && avalon.vmodels.courseDetail;
                     const list = vm && vm.videoList;
                     if (!Array.isArray(list) || !list.length) return null;
                     const clean = (o) => {
                       const r = {};
                       for (const k in o) {
                         const v = o[k];
                         if (v === null || ['string', 'number', 'boolean'].includes(typeof v)) r[k] = v;
                       }
                       return r;
                     };
                     return list.map(clean);
                   }"""
            )
            if sample:
                self.log("课件明细：" + json.dumps(sample, ensure_ascii=False)[:2000])
        except Exception as exc:
            self.log(f"读取课件明细失败：{exc}")
        self.current_course = course
        self.current_ware_total = max(len(wares), 1)
        # 没达标的课件通常在后面，倒序补播：先补最后一节，补到 100% 就停，
        # 避免把前面已经学完的章节从头重播一遍。
        order = list(range(len(wares)))[::-1]
        self.log(
            "补播顺序：" + "、".join(str(i + 1) for i in order) + "（从最后一节往前补）"
        )
        for idx in order:
            name = wares[idx]
            if self.stop_flag.is_set():
                return
            # 每节开播前先看课程最新进度：补到 100% 就立刻收手，不再点后面的课件
            pct_now = self._course_progress_now(home, course)
            if pct_now >= 100:
                self.log(f"《{course.get('title')}》最新进度 {pct_now}%，已达标，停止补播")
                for row in self.current_rows:
                    if str(row.get("courseid") or "") == str(course.get("courseid") or ""):
                        row["progress_value"] = 100
                        row["progress"] = "100%"
                        break
                self.courses(self.current_rows)
                return
            result = "error"
            for attempt in range(1, 4):
                result = self._study_ware(home, idx, name or f"课件{idx + 1}")
                if result != "error":
                    break
                if attempt < 3 and not self.stop_flag.is_set():
                    self.log(f"课件加载失败，30 秒后重试（第 {attempt}/2 次重试）")
                    self._wait_idle(home, 30)
            if result == "stopped":
                return
            if result == "error":
                self.log("该课件重试后仍未成功，先跳过（下次运行会再试）")
                # 课件列表消失通常说明课程已经学完，复查进度后再决定要不要继续
                if self._course_progress_now(home, course) >= 100:
                    self.log(f"《{course.get('title')}》已达标，停止补播")
                    return
                break
            progress_now = self._course_progress_now(home, course)
            for _ in range(9):
                if progress_now >= 100 or self.stop_flag.is_set():
                    break
                self._wait_idle(home, 5)
                progress_now = self._course_progress_now(home, course)
            if progress_now >= 100:
                self.log(f"《{course.get('title')}》进度已达 100%，不再播放后续课件")
                for row in self.current_rows:
                    if str(row.get("courseid") or "") == str(course.get("courseid") or ""):
                        row["progress_value"] = 100
                        row["progress"] = "100%"
                        self.courses(self.current_rows)
                        break
                return
            self._wait_idle(home, 3)

    # ---------- 主流程 ----------
    def run(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            self.log(f"缺少 Playwright：{exc}（请执行 pip install playwright）")
            self.out.put(("done", "error"))
            return

        LOG_DIR.mkdir(exist_ok=True)
        self.log_file = LOG_DIR / f"learn-{datetime.now():%Y%m%d-%H%M%S}.log"
        self.log(f"日志文件：{self.log_file}")

        attempt = 0
        while attempt < 6 and not self.stop_flag.is_set():
            attempt += 1
            if attempt > 1:
                self.log(f"----- 第 {attempt} 次自动重连（平台已保存进度，会自动续播） -----")
            try:
                if self._session():
                    self.log("全部流程结束")
                    self.out.put(("done", "stopped" if self.stop_flag.is_set() else "finished"))
                    return
                self.out.put(("done", "login"))
                return
            except Exception as exc:
                self.log(f"第 {attempt} 次运行中断：{type(exc).__name__}: {exc}")
                if self.stop_flag.is_set():
                    break
                self.log("60 秒后自动重试整个流程")
                for _ in range(60):
                    if self.stop_flag.is_set():
                        break
                    time.sleep(1)
        self.out.put(("done", "stopped" if self.stop_flag.is_set() else "error"))

    def _session(self) -> bool:
        """一次完整会话：登录 -> 逐门逐课件学习。返回 True 表示流程跑完。"""
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            ctx = self._launch(pw)
            try:
                ctx.on("page", self._attach)
                home = ctx.pages[0] if ctx.pages else ctx.new_page()
                self._attach(home)

                if not self._login(home):
                    self.log("登录未完成，结束")
                    return False

                self.status("正在读取课程…")
                rows = self._read_courses(home)
                if not rows:
                    self.log("课程列表为空，尝试重新登录后重试")
                    if not self._login(home):
                        self.log("登录未完成，结束")
                        return False
                    rows = self._read_courses(home)
                if not rows:
                    raise RuntimeError("未能读取课程列表，请检查平台登录状态或网络")
                self.courses(rows)

                if self.list_only:
                    self.log(f"列表模式：共 {len(rows)} 门课程")
                    for row in rows:
                        self.log(
                            f"{row.get('title')} | {row.get('hours')} 学时 | "
                            f"{row.get('status')} | {row.get('progress')} | "
                            f"value={row.get('progress_value')}"
                        )
                    return True

                if self.tests_only:
                    self.log("仅检查在线自测状态（不播放视频）")
                    for row in rows:
                        if self.stop_flag.is_set():
                            break
                        pending = self._pending_tests(home, row)
                        if pending:
                            for test in pending:
                                self.log(f"《{row.get('title')}》待答：{test.get('name')}（{test.get('state')}）")
                        else:
                            self.log(f"《{row.get('title')}》自测：没有待答试卷")
                    return True

                if self.answer_mode:
                    self.log("答题模式：逐门打开未通过的在线自测，请在浏览器里作答")
                    for round_no in range(1, 6):
                        if self.stop_flag.is_set():
                            break
                        self.answer_round = round_no
                        self.log(f"===== 答题第 {round_no}/5 轮 =====")
                        for row in rows:
                            if self.stop_flag.is_set():
                                break
                            self._handle_tests(home, row, wait_minutes=120)
                        pending_count = 0
                        for row in rows:
                            if self.stop_flag.is_set():
                                break
                            pending_count += len(self._pending_tests(home, row))
                        if pending_count == 0:
                            self.log("全部在线自测已通过")
                            return True
                        self.log(f"本轮仍有 {pending_count} 份未通过，自动进入下一轮")
                        time.sleep(5)
                    self.log("已达到答题轮次上限，仍有未通过试卷，请查看日志")
                    return True

                todo = [r for r in rows if r.get("progress_value", 0) < 100]
                self.log(f"共 {len(rows)} 门课程，已完成 {len(rows) - len(todo)} 门，待学习 {len(todo)} 门")
                if not todo:
                    return True
                hours = sum(float(r.get("hours") or 0) for r in todo)
                if hours:
                    estimate = hours * MINUTES_PER_HOUR_CREDIT / 60 / max(self.playback_rate, 0.1)
                    self.log(f"待学习约 {hours:.1f} 学时，按 {self.playback_rate:g}x 估算约 {estimate:.1f} 小时")
                else:
                    self.log("学时会随课程逐个读取；按平台规则，视频必须真实播放一遍，全部学完预计需要十几个小时。")

                for idx, course in enumerate(todo, 1):
                    if self.stop_flag.is_set():
                        break
                    self.status(f"[{idx}/{len(todo)}] {course.get('title')}")
                    self.log(f"===== 第 {idx}/{len(todo)} 门：{course.get('title')} =====")
                    self._study_course(home, course)
                    self._handle_tests(home, course)
                    pct = 0
                    for verify_round in range(1, 6):
                        if self.stop_flag.is_set():
                            break
                        pct = self._course_progress(home, course)
                        self.log(f"《{course.get('title')}》当前平台进度：{pct}%")
                        if pct >= 100:
                            break
                        self.log(f"未到 100%，继续补播（第 {verify_round}/5 轮）")
                        self._study_course(home, course)
                        self._handle_tests(home, course)
                    for row in self.current_rows:
                        if row.get("courseid") == course.get("courseid"):
                            row["progress_value"] = pct
                            row["progress"] = f"{pct}%"
                            row["status"] = "已完成" if pct >= 100 else "未完成"
                    self.courses(self.current_rows)
                    self.log(f"《{course.get('title')}》处理结束，继续下一门")

                self.log("刷新课程状态并复查所有课程的在线自测…")
                refreshed = self._read_courses(home)
                self.courses(refreshed)
                for row in refreshed:
                    if self.stop_flag.is_set():
                        break
                    pct = row.get("progress_value", 0)
                    if pct < 100:
                        self.log(f"《{row.get('title')}》仍为 {pct}%，补播视频")
                        for _ in range(5):
                            if self.stop_flag.is_set():
                                break
                            self._study_course(home, row)
                            self._handle_tests(home, row)
                            pct = self._course_progress(home, row)
                            self.log(f"《{row.get('title')}》补播后进度：{pct}%")
                            if pct >= 100:
                                break
                    self._handle_tests(home, row, wait_minutes=5)
                return True
            finally:
                if self.keep_open and not self.stop_flag.is_set():
                    self.log("流程结束，浏览器保持打开；点“停止”或关闭软件才会退出")
                    while not self.stop_flag.is_set():
                        time.sleep(1)
                try:
                    ctx.close()
                except Exception:
                    pass


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.queue: "queue.Queue[tuple]" = queue.Queue()
        self.engine: Engine | None = None
        self.config = self._load_config()
        self.rows: list = []
        self.cli_tests_only = False
        self.cli_answer = False
        self.cli_list_only = False
        self.cli_check = False
        self.cli_force_visible = False
        self.update_running = False

        root.title("交通职业资格继续教育自动学习助手 — syjc.jtzyzg.org.cn")
        root.geometry("1000x700")
        root.minsize(900, 620)

        self._build_widgets()
        self.root.after(200, self._drain)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _load_config(self) -> dict:
        default = {
            "name": "交通职业资格继续教育自动学习（试验检测）",
            "url": MY_COURSES_URL,
            "username": "",
            "password": "",
            "browser_channel": "msedge",
            "playback_rate": 2.0,
            "headless": False,
            "use_ai": False,
        }
        if CONFIG_PATH.exists():
            try:
                default.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
            except Exception:
                pass
        return default

    def _save_config(self) -> None:
        self.config.pop("tests_only", None)  # 运行模式不写进配置文件
        self.config["username"] = self.var_user.get().strip()
        self.config["password"] = self.var_pass.get()
        self.config["browser_channel"] = self.var_browser.get()
        try:
            self.config["playback_rate"] = float(self.var_speed.get())
        except Exception:
            self.config["playback_rate"] = 2.0
        self.config["headless"] = False if self.cli_force_visible else bool(self.var_headless.get())
        try:
            CONFIG_PATH.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            self.log(f"配置保存失败：{exc}")

    def _build_widgets(self) -> None:
        top = ttk.LabelFrame(self.root, text="账号与浏览器")
        top.pack(fill="x", padx=10, pady=(10, 6))

        ttk.Label(top, text="账号").grid(row=0, column=0, padx=(10, 4), pady=8, sticky="e")
        self.var_user = tk.StringVar(value=self.config.get("username", ""))
        ttk.Entry(top, textvariable=self.var_user, width=26).grid(row=0, column=1, pady=8, sticky="w")

        ttk.Label(top, text="密码").grid(row=0, column=2, padx=(16, 4), pady=8, sticky="e")
        self.var_pass = tk.StringVar(value=self.config.get("password", ""))
        ttk.Entry(top, textvariable=self.var_pass, width=26, show="*").grid(row=0, column=3, pady=8, sticky="w")

        ttk.Label(top, text="浏览器").grid(row=0, column=4, padx=(16, 4), pady=8, sticky="e")
        self.var_browser = tk.StringVar(value=self.config.get("browser_channel", "chromium"))
        ttk.Combobox(top, textvariable=self.var_browser, values=("chromium", "chrome", "msedge"), width=10,
                     state="readonly").grid(row=0, column=5, pady=8, sticky="w")

        ttk.Label(top, text="倍速").grid(row=0, column=6, padx=(16, 4), pady=8, sticky="e")
        self.var_speed = tk.StringVar(value=str(self.config.get("playback_rate", 2.0)))
        ttk.Combobox(top, textvariable=self.var_speed, values=("1.0", "1.5", "2.0", "2.5", "3.0"),
                     width=6, state="readonly").grid(row=0, column=7, pady=8, sticky="w")
        self.var_headless = tk.BooleanVar(value=bool(self.config.get("headless", True)))
        ttk.Checkbutton(top, text="后台隐藏浏览器", variable=self.var_headless).grid(
            row=0, column=8, padx=(10, 4), pady=8, sticky="w"
        )

        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=10)
        self.btn_check = ttk.Button(bar, text="一键登录检查课程", command=self.on_check)
        self.btn_check.pack(side="left")
        self.btn_query = ttk.Button(bar, text="查询课程", command=self.on_query)
        self.btn_query.pack(side="left", padx=6)
        self.btn_start = ttk.Button(bar, text="开始刷课", command=self.on_start)
        self.btn_start.pack(side="left", padx=6)
        self.btn_stop = ttk.Button(bar, text="停止（先安全退出）", command=self.on_stop, state="disabled")
        self.btn_stop.pack(side="left")
        self.btn_answer = ttk.Button(bar, text="开始答题（在线自测）", command=self.on_answer)
        self.btn_answer.pack(side="left", padx=6)
        ttk.Button(bar, text="打开平台", command=self.open_site).pack(side="left", padx=6)
        ttk.Button(bar, text="打开日志目录", command=lambda: self._open(LOG_DIR)).pack(side="left")
        update_state = "normal" if self._update_files_available() else "disabled"
        self.btn_update = ttk.Button(bar, text="检查更新", command=self.on_update, state=update_state)
        self.btn_update.pack(side="left", padx=6)

        mid = ttk.LabelFrame(self.root, text="我的课程")
        mid.pack(fill="both", expand=True, padx=10, pady=6)
        columns = ("idx", "title", "hours", "progress", "status")
        self.tree = ttk.Treeview(mid, columns=columns, show="headings", height=9)
        for col, text, width in (
            ("idx", "序号", 50),
            ("title", "课程名称", 470),
            ("hours", "学时", 70),
            ("progress", "进度", 80),
            ("status", "状态", 150),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="center" if col != "title" else "w")
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)

        low = ttk.LabelFrame(self.root, text="运行日志")
        low.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self.txt = scrolledtext.ScrolledText(low, height=14, state="disabled", wrap="word")
        self.txt.pack(fill="both", expand=True)

        self.var_status = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.var_status, anchor="w").pack(fill="x", padx=12, pady=(0, 8))

        self.log("软件已就绪：可先点“一键登录检查课程”确认登录和课程。")

    def _open(self, path: Path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
            import os
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception as exc:
            self.log(f"打开目录失败：{exc}")

    def open_site(self) -> None:
        try:
            import webbrowser
            webbrowser.open(MY_COURSES_URL)
        except Exception as exc:
            self.log(f"打开平台失败：{exc}")

    def _update_files_available(self) -> bool:
        return (
            (PORTABLE_ROOT / "updater_core.py").exists()
            and (PORTABLE_ROOT / "update_source.json").exists()
            and (PORTABLE_ROOT / "update_runner.ps1").exists()
            and (PORTABLE_ROOT / UPDATE_EXE).exists()
        )

    def _configure_updater(self):
        import sys as _sys

        root = str(PORTABLE_ROOT)
        if root not in _sys.path:
            _sys.path.insert(0, root)
        import updater_core as updater

        updater.configure(
            app_version=APP_VERSION,
            app_dir=root,
            exe_name=UPDATE_EXE,
            update_source_file=str(PORTABLE_ROOT / "update_source.json"),
            runner_script=str(PORTABLE_ROOT / "update_runner.ps1"),
            default_manifest_urls=[
                "https://gh-proxy.com/https://raw.githubusercontent.com/"
                "752153071/relay-course-3f9c71/main/update.json",
                "https://github.com/752153071/relay-course-3f9c71/"
                "releases/latest/download/update.json",
            ],
            preserve=[
                "edu/config.json",
                "edu/答案库.json",
                "edu/runner.pid",
                "edu/logs",
                "edu/browser_profile",
                "syjc/config.json",
                "syjc/答案库.json",
                "syjc/runner.pid",
                "syjc/logs",
                "syjc/browser_profile",
            ],
            log=lambda message: self.queue.put(("log", "[更新] " + str(message))),
        )
        return updater

    def _set_update_button_state(self, state: str) -> None:
        try:
            self.btn_update.configure(state=state)
        except Exception:
            pass

    def _update_worker(self) -> None:
        try:
            updater = self._configure_updater()
            manifest = updater.fetch_best_manifest()
            if updater.version_key(manifest.get("version")) <= updater.version_key(APP_VERSION):
                self.queue.put(("update_info", "当前已是最新版"))
                return
            result = updater.prepare_update(manifest)
            if result.get("kind") == "none":
                self.queue.put(("update_info", "当前已是最新版"))
                return
            package = str(result.get("package") or "")
            if result.get("kind") == "full" and not updater.package_has_main_exe(package):
                raise RuntimeError("更新包缺少启动文件")
            self.queue.put(("update_ready", (manifest, package, result)))
        except Exception as exc:
            self.queue.put(("update_error", str(exc)))

    def on_update(self) -> None:
        if self.update_running:
            return
        if self.engine and self.engine.is_alive():
            messagebox.showwarning("请先停止", "请先停止刷课，再检查更新。")
            return
        if not self._update_files_available():
            messagebox.showinfo("不可更新", "当前不是完整便携版，无法自动更新。")
            return
        self.update_running = True
        self._set_update_button_state("disabled")
        self.log(f"[更新] 当前版本 {APP_VERSION}，正在检查更新…")
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_finished(self, message: str, error: bool = False) -> None:
        self.update_running = False
        if self._update_files_available():
            self._set_update_button_state("normal")
        self.log(f"[更新] {message}")
        self.var_status.set(message)
        if error:
            messagebox.showerror("更新失败", message)
        else:
            messagebox.showinfo("更新", message)

    def _update_ready(self, payload) -> None:
        manifest, package, result = payload
        self.update_running = False
        if self._update_files_available():
            self._set_update_button_state("normal")
        notes = str(manifest.get("notes") or "").strip()
        version = manifest.get("version") or ""
        size_mb = float(result.get("bytes") or 0) / 1024 / 1024
        if result.get("kind") == "files":
            text = (
                f"发现新版本 {version}\n\n{notes}\n\n"
                f"增量更新：只下载 {result.get('count')} 个文件，约 {size_mb:.2f} MB。\n"
                "现在安装并重启？"
            )
        else:
            text = (
                f"发现新版本 {version}\n\n{notes}\n\n"
                f"完整更新包已下载（{size_mb:.0f} MB），现在安装并重启？"
            )
        if not messagebox.askyesno("发现更新", text):
            try:
                os.remove(package)
            except Exception:
                pass
            self.log("[更新] 已取消本次更新")
            return
        try:
            updater = self._configure_updater()
            self.log("[更新] 正在启动更新程序，软件即将重启…")
            updater.launch_update_runner(package)
            self.var_status.set("正在更新，软件将自动重启…")
            self.root.after(800, self.root.destroy)
        except Exception as exc:
            self._update_finished(str(exc), error=True)

    def log(self, text: str) -> None:
        self.txt.configure(state="normal")
        self.txt.insert("end", text + "\n")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "status":
                    self.var_status.set(payload)
                elif kind == "courses":
                    self._fill_courses(payload)
                elif kind == "progress":
                    self._update_progress(payload)
                elif kind == "alert":
                    self._alert(payload)
                elif kind == "done":
                    self._finish(payload)
                elif kind == "update_info":
                    self._update_finished(payload)
                elif kind == "update_error":
                    self._update_finished(payload, error=True)
                elif kind == "update_ready":
                    self._update_ready(payload)
        except queue.Empty:
            pass
        self.root.after(200, self._drain)

    def _alert(self, text: str) -> None:
        self.log(f"※ 需要你配合：{text}")
        self.var_status.set(text)
        try:
            self.root.bell()
            self.root.attributes("-topmost", True)
            self.root.after(5000, lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass

    def _fill_courses(self, rows: list) -> None:
        self.rows = rows
        for item in self.tree.get_children():
            self.tree.delete(item)
        for i, row in enumerate(rows, 1):
            pct = row.get("progress_value", 0)
            status = "已完成" if pct >= 100 else ("未开始" if pct == 0 else "学习中")
            iid = str(row.get("courseid") or row.get("title") or i)
            self.tree.insert("", "end", iid=iid, values=(i, row.get("title", ""), row.get("hours", ""),
                                                         f"{pct}%", status))

    def _update_progress(self, payload: dict) -> None:
        try:
            iid = str(payload.get("courseid") or "")
            pct = float(payload.get("pct") or 0)
            if not iid or not self.tree.exists(iid):
                return
            values = list(self.tree.item(iid, "values"))
            values[3] = f"{pct:.0f}%"
            values[4] = "学习中"
            self.tree.item(iid, values=values)
            self.tree.see(iid)
        except Exception:
            pass

    def _finish(self, reason: str) -> None:
        self.btn_check.configure(state="normal")
        self.btn_query.configure(state="normal")
        self.btn_start.configure(state="normal")
        self.btn_answer.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self._set_update_button_state(
            "normal" if self._update_files_available() else "disabled"
        )
        text = {"finished": "全部课程处理完毕", "stopped": "已停止",
                "login": "登录未完成", "error": "运行出错"}.get(reason, reason)
        self.var_status.set(text)
        self.log(f"=== {text} ===")
        if reason == "finished":
            if self.cli_list_only or self.cli_tests_only or self.cli_check:
                self.root.after(500, self.root.destroy)
            else:
                messagebox.showinfo("完成", "所有课程已处理完毕。如需核对，请点“查询课程”刷新进度。")

    def _ensure_engine(self, mode: str = "start") -> None:
        if self.engine and self.engine.is_alive():
            messagebox.showinfo("提示", "任务正在运行，请先点“停止”")
            return
        self._save_config()
        if not self.var_user.get().strip() or not self.var_pass.get():
            messagebox.showwarning("缺少账号", "请先填写账号和密码")
            return
        engine_config = dict(self.config)
        engine_config["tests_only"] = self.cli_tests_only and mode == "query"
        engine_config["answer_mode"] = self.cli_answer or mode == "answer"
        engine_config["list_only"] = (self.cli_list_only and mode == "query") or mode == "check"
        if mode == "check":
            engine_config["headless"] = False
        self.engine = Engine(engine_config, self.queue)
        self.engine.start()
        self.btn_check.configure(state="disabled")
        self.btn_query.configure(state="disabled")
        self.btn_start.configure(state="disabled")
        self.btn_answer.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self._set_update_button_state("disabled")
        self.var_status.set({"query": "查询中…", "answer": "答题模式运行中…"}.get(mode, "运行中…"))

    def on_check(self) -> None:
        self._ensure_engine("check")

    def on_query(self) -> None:
        self._ensure_engine("query")

    def on_start(self) -> None:
        self._ensure_engine("start")

    def on_answer(self) -> None:
        self._ensure_engine("answer")

    def on_stop(self) -> None:
        if self.engine:
            self.log("收到停止指令：当前课件会先点“安全退出”保存进度，然后停下。")
            self.engine.stop()
            self.var_status.set("正在停止…")

    def _on_close(self) -> None:
        if self.engine and self.engine.is_alive():
            if not messagebox.askokcancel("退出", "后台还在学习，确定退出吗？\n（建议先点“停止”，让软件保存进度）"):
                return
            self.engine.stop()
            self.var_status.set("正在保存进度并退出…")
            self.root.after(500, self._wait_close)
            return
        self.root.destroy()

    def _wait_close(self) -> None:
        if self.engine and self.engine.is_alive():
            self.root.after(500, self._wait_close)
            return
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    try:
        from tkinter import ttk as _ttk
        style = _ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    app = App(root)
    if "--visible" in sys.argv:
        app.cli_force_visible = True
        app.config["headless"] = False
        app.var_headless.set(False)
    if "--tests-only" in sys.argv:
        app.cli_tests_only = True
        root.after(1500, app.on_query)
    elif "--check" in sys.argv:
        app.cli_check = True
        root.after(1500, app.on_check)
    elif "--answer" in sys.argv:
        app.cli_answer = True
        root.after(1500, app.on_answer)
    elif "--list-only" in sys.argv:
        app.cli_list_only = True
        root.after(1500, app.on_query)
    elif "--auto" in sys.argv:
        # 供无人值守启动：打开软件后自动开始刷课
        root.after(1500, app.on_start)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
