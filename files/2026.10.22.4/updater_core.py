#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Drop-in auto-update client for a packaged desktop app.

Design goals that came out of real deployment on a China-facing Windows app:

* Read the manifest from several sources and keep the newest version, preferring a
  manifest that carries ``parts``. GitHub main-site URLs are flaky in mainland China,
  so the mirror comes first and the release URL is the fallback.
* Add a cache-buster to ``github.com/.../releases/...`` URLs; GitHub's CDN can serve a
  cached 404 for minutes after an asset is recreated.
* Download chunks with resume, stall detection, per-URL retries, per-part SHA256, and a
  final whole-zip SHA256. Never install unverified bytes.
* Hand the swap to a detached PowerShell runner that waits for this process to exit,
  preserves user files, and relaunches.

Wire-up in the host app::

    import updater_core as updater

    updater.configure(
        app_version=APP_VERSION,
        app_dir=HERE,
        exe_name="MyApp.exe",
        update_source_file=os.path.join(HERE, "update_source.json"),
        log=my_log,
    )

    manifest = updater.fetch_best_manifest()
    if updater.version_key(manifest["version"]) > updater.version_key(APP_VERSION):
        package = updater.download_update_package(manifest)
        if updater.package_has_main_exe(package):
            updater.launch_update_runner(package)

Every call is blocking; run it on a worker thread and report progress through ``log``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import uuid
import zipfile

import requests

MIRROR_PREFIX = "https://gh-proxy.com/"
USER_AGENT = "DesktopAutoUpdater/1.0"
STALL_TIMEOUT = 100
OVERALL_TIMEOUT = 900
ATTEMPTS_PER_URL = 2

_config: dict = {
    "app_version": "0.0.0",
    "app_dir": os.getcwd(),
    "exe_name": "",
    "update_source_file": "",
    "runner_script": "",
    "default_manifest_urls": [],
    "preserve": [
        "cookies",
        "登录信息.json",
        "授权文件.json",
        "更新日志.log",
    ],
    "log": print,
}


def configure(**kwargs) -> None:
    """Set app-specific values. Call once before any other function."""
    _config.update(kwargs)


def log(message: str) -> None:
    callback = _config.get("log") or print
    try:
        callback(str(message))
    except Exception:
        pass


def version_key(value) -> tuple:
    parts = re.findall(r"\d+", str(value or ""))
    return tuple(int(part) for part in parts) if parts else (0,)


def load_update_source() -> dict:
    source: dict = {}
    path = str(_config.get("update_source_file") or "")
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                source.update(data)
        except Exception:
            pass
    if not source.get("manifest_urls"):
        source["manifest_urls"] = list(_config.get("default_manifest_urls") or [])
    env_url = os.environ.get("APP_UPDATE_MANIFEST_URL", "").strip()
    if env_url:
        source["manifest_url"] = env_url
    return source


def update_manifest_urls(source: dict) -> list[str]:
    values: list[str] = []
    single = str(source.get("manifest_url") or "").strip()
    if single:
        values.append(single)
    many = source.get("manifest_urls")
    if isinstance(many, list):
        values.extend(str(item or "").strip() for item in many)
    values.extend(str(item) for item in _config.get("default_manifest_urls") or [])
    result: list[str] = []
    cache_buster = str(int(time.time()))
    for value in values:
        url = str(value or "").strip()
        if not url:
            continue
        parsed = urllib.parse.urlparse(url)
        local = parsed.hostname in ("127.0.0.1", "localhost")
        if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
            continue
        if "github.com/" in url and "releases/" in url:
            url = url + ("&" if "?" in url else "?") + "ts=" + cache_buster
        if url in result:
            continue
        result.append(url)
    return result


def pick_latest_manifest(manifests: list) -> dict | None:
    valid = [item for item in manifests if isinstance(item, dict)]
    if not valid:
        return None

    def rank(pair):
        index, item = pair
        has_parts = 1 if item.get("parts") else 0
        return (version_key(item.get("version")), has_parts, -index)

    return max(list(enumerate(valid)), key=rank)[1]


def fetch_update_manifest(url: str) -> dict:
    response = requests.get(
        url, headers={"User-Agent": USER_AGENT}, timeout=(10, 30)
    )
    response.raise_for_status()
    data = json.loads(response.text)
    if not isinstance(data, dict) or "version" not in data:
        raise ValueError("更新清单格式不正确")
    return data


def fetch_best_manifest(source: dict | None = None) -> dict:
    source = source if source is not None else load_update_source()
    urls = update_manifest_urls(source)
    if not urls:
        raise ValueError("尚未配置更新地址")
    attempts = urls + urls
    manifests: list[dict] = []
    errors: list[str] = []
    for index, url in enumerate(attempts):
        try:
            log("读取更新清单: " + url)
            manifest = fetch_update_manifest(url)
            manifest["_manifest_url"] = url
            manifests.append(manifest)
        except Exception as exc:
            errors.append(str(exc))
            log("更新清单不可用: " + str(exc)[:160])
        if index + 1 < len(attempts):
            time.sleep(2)
    best = pick_latest_manifest(manifests)
    if best is None:
        raise RuntimeError("无法读取更新清单: " + ("; ".join(errors[-2:]) or "未知错误"))
    return best


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest().lower()


def _remove_quietly(path: str) -> None:
    for _ in range(3):
        try:
            if os.path.exists(path):
                os.remove(path)
            return
        except OSError:
            time.sleep(0.5)


def _stream_to_file(response, target: str, label: str, offset: int, started: float) -> int:
    total_header = int(response.headers.get("Content-Length") or 0)
    total = total_header + offset if total_header else 0
    received = offset
    last_percent = -10
    last_progress = time.time()
    mode = "ab" if offset else "wb"
    with open(target, mode) as output:
        for chunk in response.iter_content(chunk_size=1024 * 512):
            if not chunk:
                if time.time() - last_progress > STALL_TIMEOUT:
                    raise TimeoutError(label + "长时间无数据，切换下载地址")
                if time.time() - started > OVERALL_TIMEOUT:
                    raise TimeoutError(label + "下载超时，切换下载地址")
                continue
            output.write(chunk)
            received += len(chunk)
            last_progress = time.time()
            if total:
                percent = int(received * 100 / total)
                if percent >= last_percent + 10:
                    last_percent = percent
                    log(label + "下载进度: " + str(percent) + "%")
    return total


def download_to_file(url: str, target: str, label: str = "更新包") -> str:
    offset = os.path.getsize(target) if os.path.exists(target) else 0
    starts = [offset] if offset else []
    starts.append(0)
    last_error: Exception | None = None
    for index, start_at in enumerate(starts):
        started = time.time()
        if index > 0:
            _remove_quietly(target)
            start_at = 0
        headers = {"Range": f"bytes={start_at}-"} if start_at else {}
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        try:
            with session.get(
                url,
                headers=headers,
                stream=True,
                timeout=(15, 30),
                allow_redirects=True,
            ) as response:
                if response.status_code == 416 and start_at:
                    last_error = RuntimeError("服务器不支持续传，改为重新下载")
                    log(label + str(last_error))
                    continue
                response.raise_for_status()
                resumed = start_at > 0 and response.status_code == 206
                if start_at and not resumed:
                    _remove_quietly(target)
                    start_at = 0
                if resumed:
                    log(label + "续传: " + str(int(start_at / 1024)) + " KB")
                total = _stream_to_file(response, target, label, start_at, started)
                if total == 0:
                    log(label + "下载完成")
                return target
        except Exception as exc:  # noqa: BLE001 - roll over to the next attempt
            last_error = exc
        finally:
            session.close()
    raise RuntimeError(str(last_error or (label + "下载失败")))


def download_urls_with_fallback(
    urls: list[str], target: str, expected_sha: str, label: str
) -> str:
    attempts: list[str] = []
    for _ in range(max(1, ATTEMPTS_PER_URL)):
        attempts.extend(urls)
    last_error = ""
    for index, url in enumerate(attempts):
        try:
            log("开始下载" + label + ": " + url)
            download_to_file(url, target, label)
            actual = sha256_file(target)
            if actual != expected_sha:
                raise ValueError(
                    label + "校验失败，期望 " + expected_sha[:12] + "，实际 " + actual[:12]
                )
            log(label + "下载并校验完成")
            return target
        except Exception as exc:
            last_error = str(exc)
            log(label + "地址失败: " + last_error[:160])
        if index + 1 < len(attempts):
            time.sleep(5)
    raise RuntimeError("所有" + label + "地址均下载失败: " + last_error)


def download_update_package(manifest: dict) -> str:
    expected = str(manifest.get("sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("更新清单缺少有效的 SHA256")
    fd, package_path = tempfile.mkstemp(prefix="app_update_", suffix=".zip")
    os.close(fd)
    parts = [item for item in manifest.get("parts") or [] if isinstance(item, dict)]
    try:
        if parts:
            with open(package_path, "wb") as combined:
                for index, part in enumerate(parts, start=1):
                    urls: list[str] = []
                    for key in ("urls", "url"):
                        value = part.get(key)
                        if isinstance(value, list):
                            urls.extend(str(item or "").strip() for item in value)
                        elif value:
                            urls.append(str(value).strip())
                    urls = [url for url in urls if url]
                    part_sha = str(part.get("sha256") or "").strip().lower()
                    if not urls:
                        raise ValueError(f"第 {index} 个分片缺少下载地址")
                    if not re.fullmatch(r"[0-9a-f]{64}", part_sha):
                        raise ValueError(f"第 {index} 个分片缺少有效的 SHA256")
                    part_path = os.path.join(
                        tempfile.gettempdir(),
                        f"app_update_part_{part_sha[:12]}_{index}.bin",
                    )
                    try:
                        download_urls_with_fallback(
                            urls,
                            part_path,
                            part_sha,
                            f"分片 {index}/{len(parts)} ",
                        )
                        with open(part_path, "rb") as handle:
                            shutil.copyfileobj(handle, combined, 1024 * 1024)
                    except Exception:
                        _remove_quietly(part_path)
                        raise
        else:
            urls = []
            for key in ("url", "download_url"):
                if manifest.get(key):
                    urls.append(str(manifest[key]).strip())
            if isinstance(manifest.get("urls"), list):
                urls.extend(str(item or "").strip() for item in manifest["urls"])
            urls = [url for url in urls if url]
            if not urls:
                raise ValueError("更新清单没有可用的 HTTPS 下载地址")
            download_urls_with_fallback(urls, package_path, expected, "更新包 ")
        actual = sha256_file(package_path)
        if actual != expected:
            raise ValueError(
                "更新包校验失败，期望 " + expected[:12] + "，实际 " + actual[:12]
            )
        log("更新包下载并校验完成")
        return package_path
    except Exception:
        _remove_quietly(package_path)
        raise


def package_has_main_exe(package_path: str) -> bool:
    exe_name = str(_config.get("exe_name") or "")
    if not exe_name:
        return True
    with zipfile.ZipFile(package_path) as archive:
        return any(name.endswith(exe_name) for name in archive.namelist())


def _local_sha256(path: str) -> str:
    try:
        return sha256_file(path)
    except Exception:
        return ""


def prepare_update(manifest: dict) -> dict:
    """优先做文件级增量更新，不可用时回退整包下载。

    返回 {"kind": "none"} 表示本地已是最新；
    {"kind": "files", "package": <zip>, "count": n, "bytes": b} 表示增量包；
    {"kind": "full", "package": <zip>, "bytes": b} 表示整包。
    """
    app_dir = str(_config.get("app_dir") or "")
    files = [item for item in (manifest.get("files") or []) if isinstance(item, dict)]
    if files and app_dir and os.path.isdir(app_dir):
        runtime_ok = all(
            os.path.exists(os.path.join(app_dir, rel))
            for rel in ("python/pythonw.exe", "browsers/ms-playwright")
        )
        if runtime_ok:
            changed: list[tuple[str, dict, str]] = []
            for item in files:
                rel = str(item.get("path") or "").replace("\\", "/").strip("/")
                expected = str(item.get("sha256") or "").strip().lower()
                if not rel or not re.fullmatch(r"[0-9a-f]{64}", expected):
                    continue
                local = os.path.join(app_dir, rel.replace("/", os.sep))
                if _local_sha256(local) != expected:
                    changed.append((rel, item, expected))
            if not changed:
                log("本地文件与最新版一致，无需下载")
                return {"kind": "none"}
            total = sum(int(item.get("size") or 0) for _, item, _ in changed)
            log(
                "增量更新：需要更新 %d 个文件，共约 %.0f KB"
                % (len(changed), total / 1024.0)
            )
            temp_root = tempfile.mkdtemp(prefix="app_patch_")
            try:
                for index, (rel, item, expected) in enumerate(changed, start=1):
                    target = os.path.join(temp_root, rel.replace("/", os.sep))
                    parent = os.path.dirname(target)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    urls: list[str] = []
                    for key in ("urls", "url"):
                        value = item.get(key)
                        if isinstance(value, list):
                            urls.extend(str(u or "").strip() for u in value)
                        elif value:
                            urls.append(str(value).strip())
                    urls = [u for u in urls if u]
                    if not urls:
                        raise ValueError(rel + " 缺少下载地址")
                    download_urls_with_fallback(
                        urls, target, expected, "文件 %d/%d %s " % (index, len(changed), rel)
                    )
                package_path = os.path.join(
                    tempfile.gettempdir(), "app_patch_" + uuid.uuid4().hex + ".zip"
                )
                with zipfile.ZipFile(package_path, "w", zipfile.ZIP_DEFLATED) as archive:
                    for rel, _, _ in changed:
                        archive.write(os.path.join(temp_root, rel.replace("/", os.sep)), rel)
                log("增量包准备完成")
                return {
                    "kind": "files",
                    "package": package_path,
                    "count": len(changed),
                    "bytes": total,
                }
            finally:
                shutil.rmtree(temp_root, ignore_errors=True)
    package = download_update_package(manifest)
    return {"kind": "full", "package": package, "bytes": os.path.getsize(package)}


def write_update_runner(package_path: str) -> str:
    """Write the swap script next to the package and return its path."""
    script_path = os.path.join(
        tempfile.gettempdir(), "app_update_" + uuid.uuid4().hex + ".ps1"
    )
    source = str(_config.get("runner_script") or "")
    if not source:
        source = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "update_runner.ps1"
        )
    if os.path.exists(source):
        shutil.copy2(source, script_path)
    else:
        raise RuntimeError("缺少 update_runner.ps1")
    return script_path


def launch_update_runner(package_path: str) -> None:
    app_dir = str(_config.get("app_dir") or "")
    exe_name = str(_config.get("exe_name") or "")
    if not app_dir or not exe_name:
        raise RuntimeError("updater_core 未配置 app_dir / exe_name")
    if not os.path.exists(os.path.join(app_dir, exe_name)):
        raise RuntimeError("当前不是打包版，不能自动替换")
    script_path = write_update_runner(package_path)
    preserve = ";".join(str(item) for item in _config.get("preserve") or [])
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        script_path,
        "-ParentPid",
        str(os.getpid()),
        "-ZipPath",
        package_path,
        "-AppDir",
        app_dir,
        "-ExeName",
        exe_name,
        "-Preserve",
        preserve,
        "-LogPath",
        os.path.join(app_dir, "更新日志.log"),
    ]
    subprocess.Popen(
        command,
        cwd=app_dir,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    log("更新程序已启动，软件即将重启")
