# -*- coding: utf-8 -*-
"""监理工程师继续教育自动学习（jtgcjl.jtzyzg.org.cn）。

流程：登录 -> 读“我的课程/已购买课程” -> 逐个课件 2 倍速播放 -> 安全退出 -> 下一节。
"""
from __future__ import annotations

import json
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_DIR = BASE_DIR / "logs"
PROFILE_DIR = BASE_DIR / "browser_profile"
APP_VERSION = "2026.10.27.2"

SITE_BASE = "https://jtgcjl.jtzyzg.org.cn/LTSP/LEAP/ltsp/html"
LOGIN_URL = f"{SITE_BASE}/index.html"
MY_URL = f"{SITE_BASE}/myCollection.html"
DETAIL_URL = f"{SITE_BASE}/educationDetail.html?courseId="


def now_text() -> str:
    return datetime.now().strftime("%H:%M:%S")


def mmss(seconds) -> str:
    if seconds is None:
        return "--:--"
    seconds = int(seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def parse_pct(text) -> int:
    import re

    return int(re.sub(r"[^0-9]", "", str(text or "0")) or 0)


class Engine(threading.Thread):
    def __init__(self, config: dict, out: "queue.Queue[tuple]") -> None:
        super().__init__(daemon=True)
        self.config = config
        self.out = out
        self.stop_flag = threading.Event()
        self.log_file: Path | None = None
        self.dialogs: list[str] = []
        try:
            self.rate = float(config.get("playback_rate") or 2.0)
        except Exception:
            self.rate = 2.0
        self.headless = bool(config.get("headless", False))
        self.keep_open = bool(config.get("keep_open", True))

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

    def stop(self) -> None:
        self.stop_flag.set()

    def _launch(self, pw):
        PROFILE_DIR.mkdir(exist_ok=True)
        candidates = []
        for name in (self.config.get("browser_channel") or "msedge", "chrome", "msedge"):
            name = str(name).strip().lower()
            if name and name not in candidates:
                candidates.append(name)
        last_error = None
        for name in candidates:
            try:
                ctx = pw.chromium.launch_persistent_context(
                    user_data_dir=str(PROFILE_DIR),
                    channel=name,
                    headless=self.headless,
                    args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
                    no_viewport=True,
                    locale="zh-CN",
                )
                self.log(f"浏览器已启动：{name}（{'后台' if self.headless else '可见窗口'}）")
                return ctx
            except Exception as exc:
                last_error = exc
                self.log(f"启动 {name} 失败：{exc}")
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

    def _logged_in(self, page) -> bool:
        try:
            box = page.locator("#inLogname")
            if box.count() and box.is_visible():
                return False
        except Exception:
            pass
        try:
            return "退出" in page.evaluate("() => document.body.innerText")
        except Exception:
            return False

    def _login(self, page) -> bool:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        if self._logged_in(page):
            self.log("已有登录状态，跳过登录")
            return True
        user = str(self.config.get("username") or "").strip()
        pwd = str(self.config.get("password") or "")
        for attempt in range(1, 5):
            if self.stop_flag.is_set():
                return False
            try:
                if not page.locator("#inLogname").is_visible():
                    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(2500)
                if self._logged_in(page):
                    self.log("检测到已登录")
                    return True
                if attempt > 1:
                    try:
                        page.locator("#checkCode02").click()
                        page.wait_for_timeout(800)
                    except Exception:
                        pass
                captcha = page.locator("#checkCode02").inner_text().strip()
                self.log(f"第 {attempt} 次登录：验证码{'已读取' if captcha else '为空'}")
                if not captcha:
                    page.wait_for_timeout(1500)
                    continue
                page.fill("#inLogname", user)
                page.fill("#inLogpwd", pwd)
                page.fill("#inLogvaricode", captcha)
                page.locator("button.bg_red", has_text="登录").first.click()
                for _ in range(10):
                    page.wait_for_timeout(1000)
                    if self._logged_in(page):
                        self.log("登录成功")
                        return True
                self.log("登录未通过，重试")
            except Exception as exc:
                self.log(f"登录异常：{exc}")
                page.wait_for_timeout(2000)
        self.log("自动登录失败，请在浏览器里手动登录（等待 5 分钟）")
        end = time.time() + 300
        while time.time() < end and not self.stop_flag.is_set():
            page.wait_for_timeout(2000)
            if self._logged_in(page):
                self.log("检测到已登录，继续")
                return True
        return False

    def _read_courses(self, page) -> list:
        page.goto(MY_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        try:
            tab = page.get_by_text("已购买课程", exact=True).first
            if tab.count() and tab.is_visible():
                tab.click()
                page.wait_for_timeout(2500)
        except Exception:
            pass
        rows: list[dict] = []
        seen = set()
        for page_no in range(1, 40):
            items = page.evaluate(
                """() => {
                     const vm = window.avalon && avalon.vmodels && avalon.vmodels.myCollection;
                     const list = vm && vm.boughtcourseList;
                     if (!Array.isArray(list)) return [];
                     return list.map(c => ({
                       id: c.id || '', name: c.videoname || '', course: c.typename || '',
                       kind: c.coursetypename || '', rate: c.learnrate || '0%',
                       finished: c.isfinishvideo || 0, length: c.warelength || '',
                       hours: c.timelength || '', href: c.href || ''
                     }));
                   }"""
            ) or []
            fresh = 0
            for item in items:
                key = item.get("id") or item.get("name")
                if not key or key in seen:
                    continue
                seen.add(key)
                item["pct"] = parse_pct(item.get("rate"))
                rows.append(item)
                fresh += 1
            if fresh == 0:
                break
            try:
                next_link = page.locator("a", has_text=str(page_no + 1)).first
                if not next_link.count() or not next_link.is_visible():
                    break
                cls = next_link.get_attribute("class") or ""
                if "on" in cls.split():
                    break
                next_link.click()
                page.wait_for_timeout(2500)
            except Exception:
                break
        return rows

    def _video_url(self, page, course_id: str) -> str:
        page.goto(DETAIL_URL + course_id, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        href = page.evaluate(
            """() => {
                 const vm = window.avalon && avalon.vmodels && avalon.vmodels.educationDetail;
                 return vm && vm.courseInfo ? (vm.courseInfo.href || '') : '';
               }"""
        )
        href = str(href or "")
        if href.startswith("http"):
            return href
        return f"{SITE_BASE}/{href.lstrip('/')}"

    def _study(self, ctx, page, course: dict) -> str:
        name = course.get("name") or course.get("id") or "课件"
        for attempt in range(1, 5):
            if self.stop_flag.is_set():
                return "stopped"
            self.dialogs.clear()
            try:
                url = self._video_url(page, course.get("id"))
                self.log(f"打开课件《{name}》")
                player = ctx.new_page()
                self._attach(player)
                player.goto(url, wait_until="domcontentloaded", timeout=60000)
                player.wait_for_timeout(4000)
                if any("已经存在打开的页面" in d for d in self.dialogs):
                    self.log("平台提示已有学习页面，等待 100 秒后重试")
                    try:
                        player.close()
                    except Exception:
                        pass
                    end = time.time() + 100
                    while time.time() < end and not self.stop_flag.is_set():
                        time.sleep(1)
                    continue
                state = {}
                for _ in range(30):
                    state = player.evaluate(
                        """() => { const v=document.querySelector('video');
                             return v ? {t:v.currentTime,d:isFinite(v.duration)?v.duration:null,p:v.paused} : {}; }"""
                    )
                    if state.get("d"):
                        break
                    player.wait_for_timeout(2000)
                if not state.get("d"):
                    self.log("视频没有加载出来，重试")
                    try:
                        player.close()
                    except Exception:
                        pass
                    time.sleep(3)
                    continue
                duration = float(state["d"])
                self.log(f"开始播放：《{name}》（时长 {mmss(duration)}，{self.rate:g}x）")
                player.evaluate(
                    """(rate) => { const v=document.querySelector('video');
                         if (!v) return; v.muted=true; v.defaultPlaybackRate=rate; v.playbackRate=rate;
                         const p=v.play(); if(p&&p.catch)p.catch(()=>{}); }""",
                    self.rate,
                )
                last_t = 0.0
                last_progress = time.time()
                last_log = 0.0
                while not self.stop_flag.is_set():
                    player.wait_for_timeout(1000)
                    try:
                        st = player.evaluate(
                            """() => { const v=document.querySelector('video');
                                 return v ? {t:v.currentTime,d:isFinite(v.duration)?v.duration:null,
                                             ended:v.ended,p:v.paused} : {}; }"""
                        )
                    except Exception:
                        st = {}
                    t = float(st.get("t") or 0)
                    d = float(st.get("d") or duration)
                    if st.get("ended") or t >= d - 2:
                        self.log(f"播放完成：《{name}》")
                        break
                    if t > last_t + 0.3:
                        last_t = t
                        last_progress = time.time()
                    elif time.time() - last_progress > 30:
                        # 平台会弹“真人学习”确认框把视频暂停，这里先尝试点掉再继续播放
                        overlay = ""
                        try:
                            overlay = player.evaluate(
                                """() => {
                                     const nodes = Array.from(document.querySelectorAll(
                                       '.layui-layer, .popbox, .dialog, [class*=mask], [class*=tip], [class*=box]'
                                     )).filter(e => {
                                       const s = getComputedStyle(e);
                                       return s.display !== 'none' && s.visibility !== 'hidden' && e.offsetHeight > 0;
                                     });
                                     return nodes.map(e => (e.innerText || '').trim()).filter(Boolean).join(' | ').slice(0, 120);
                                   }"""
                            )
                        except Exception:
                            overlay = ""
                        if overlay:
                            self.log(f"检测到页面提示：{overlay}")
                        clicked = False
                        for label in ("继续学习", "继续播放", "确定", "我知道了", "继续", "关闭"):
                            try:
                                target = player.get_by_text(label, exact=True).first
                                if target.count() and target.is_visible():
                                    target.click()
                                    self.log(f"已点击“{label}”继续学习")
                                    clicked = True
                                    break
                            except Exception:
                                continue
                        self.log("播放停滞，尝试恢复")
                        try:
                            player.evaluate(
                                """(rate) => { const v=document.querySelector('video');
                                     if (!v) return; const p=v.play(); if(p&&p.catch)p.catch(()=>{});
                                     v.playbackRate=rate; }""",
                                self.rate,
                            )
                        except Exception:
                            pass
                        last_progress = time.time()
                    now = time.time()
                    if now - last_log >= 60:
                        last_log = now
                        self.log(f"播放中 {mmss(t)} / {mmss(d)}（{t/d*100:.1f}%）")
                        self.status(f"{name} {t/d*100:.0f}%")
                try:
                    btn = player.get_by_text("安全退出", exact=True).first
                    if btn.count() and btn.is_visible():
                        btn.click()
                        self.log("已点击安全退出")
                    else:
                        self.log("未找到安全退出按钮")
                except Exception as exc:
                    self.log(f"安全退出失败：{exc}")
                player.wait_for_timeout(5000)
                try:
                    player.close()
                except Exception:
                    pass
                return "ok"
            except Exception as exc:
                self.log(f"课件异常：{exc}")
                time.sleep(5)
        return "error"

    def run(self) -> None:
        from playwright.sync_api import sync_playwright

        LOG_DIR.mkdir(exist_ok=True)
        self.log_file = LOG_DIR / f"learn-{datetime.now():%Y%m%d-%H%M%S}.log"
        self.log(f"日志文件：{self.log_file}")
        self.log(f"版本 {APP_VERSION}")
        with sync_playwright() as pw:
            ctx = self._launch(pw)
            try:
                home = ctx.pages[0] if ctx.pages else ctx.new_page()
                self._attach(home)
                if not self._login(home):
                    self.log("登录未完成，结束")
                    self.out.put(("done", "登录未完成"))
                    return
                rows = self._read_courses(home)
                self.out.put(("courses", rows))
                todo = [r for r in rows if r.get("pct", 0) < 100 or not r.get("finished")]
                self.log(f"共 {len(rows)} 个已购课件，未完成 {len(todo)} 个")
                if not todo:
                    self.log("所有课件都已完成")
                for idx, course in enumerate(todo, 1):
                    if self.stop_flag.is_set():
                        break
                    self.status(f"[{idx}/{len(todo)}] {course.get('name')}")
                    self.log(f"===== 第 {idx}/{len(todo)} 个：{course.get('name')} =====")
                    result = self._study(ctx, home, course)
                    if result == "stopped":
                        break
                self.log("本次流程结束")
                self.out.put(("done", "全部课程处理完毕"))
                if self.keep_open and not self.stop_flag.is_set():
                    self.log("浏览器保持打开；点“停止”或关闭软件才退出")
                    while not self.stop_flag.is_set():
                        time.sleep(1)
            finally:
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

        root.title("交通职业资格继续教育自动学习（监理工程师）")
        root.geometry("980x680")

        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="账号").grid(row=0, column=0, sticky="w")
        self.var_user = tk.StringVar(value=self.config.get("username", ""))
        ttk.Entry(top, textvariable=self.var_user, width=24).grid(row=0, column=1, padx=4)
        ttk.Label(top, text="密码").grid(row=0, column=2, sticky="w")
        self.var_pass = tk.StringVar(value=self.config.get("password", ""))
        ttk.Entry(top, textvariable=self.var_pass, width=20, show="*").grid(row=0, column=3, padx=4)
        ttk.Label(top, text="倍速").grid(row=0, column=4, sticky="w")
        self.var_rate = tk.StringVar(value=str(self.config.get("playback_rate", 2.0)))
        ttk.Entry(top, textvariable=self.var_rate, width=6).grid(row=0, column=5, padx=4)
        self.var_headless = tk.BooleanVar(value=bool(self.config.get("headless", False)))
        ttk.Checkbutton(top, text="后台隐藏浏览器", variable=self.var_headless).grid(row=0, column=6, padx=8)

        bar = ttk.Frame(root, padding=(8, 0))
        bar.pack(fill="x")
        self.btn_start = ttk.Button(bar, text="开始学习", command=self.on_start)
        self.btn_start.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(bar, text="停止（先安全退出）", command=self.on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=4)
        self.var_status = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self.var_status).pack(side="left", padx=12)

        self.logbox = scrolledtext.ScrolledText(root, height=32, state="disabled")
        self.logbox.pack(fill="both", expand=True, padx=8, pady=8)

        self.root.after(200, self._drain)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _load_config(self) -> dict:
        default = {
            "username": "", "password": "", "browser_channel": "msedge",
            "playback_rate": 2.0, "headless": False, "keep_open": True,
        }
        if CONFIG_PATH.exists():
            try:
                default.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
            except Exception:
                pass
        return default

    def _save_config(self) -> None:
        self.config["username"] = self.var_user.get().strip()
        self.config["password"] = self.var_pass.get()
        try:
            self.config["playback_rate"] = float(self.var_rate.get())
        except Exception:
            self.config["playback_rate"] = 2.0
        self.config["headless"] = bool(self.var_headless.get())
        try:
            CONFIG_PATH.write_text(
                json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def log(self, text: str) -> None:
        self.logbox.configure(state="normal")
        self.logbox.insert("end", text + "\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def on_start(self) -> None:
        if self.engine and self.engine.is_alive():
            messagebox.showinfo("提示", "任务正在运行")
            return
        if not self.var_user.get().strip() or not self.var_pass.get():
            messagebox.showwarning("缺少账号", "请先填写账号和密码")
            return
        self._save_config()
        self.engine = Engine(dict(self.config), self.queue)
        self.engine.start()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.var_status.set("运行中…")

    def on_stop(self) -> None:
        if self.engine:
            self.log("收到停止指令：当前课件会先安全退出再停下")
            self.engine.stop()

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "status":
                    self.var_status.set(payload)
                elif kind == "done":
                    self.btn_start.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    self.var_status.set(str(payload))
                    self.log(f"=== {payload} ===")
        except queue.Empty:
            pass
        self.root.after(200, self._drain)

    def _on_close(self) -> None:
        if self.engine and self.engine.is_alive():
            self.engine.stop()
            self.root.after(1500, self.root.destroy)
        else:
            self.root.destroy()


def main() -> int:
    root = tk.Tk()
    app = App(root)
    root.after(800, app.on_start)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
