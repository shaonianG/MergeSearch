"""检查更新：依托 GitHub Releases。对比最新 release 版本号与 APP_VERSION，
有新版时下载附件中的 exe（走 ProxyRouter 分流），写入本目录后提示重启。
"""
import asyncio
import logging
import os
import re
import sys

import httpx

from app_config import APP_VERSION, GITHUB_REPO, app_dir

logger = logging.getLogger("updater")

API_BASE = f"https://api.github.com/repos/{GITHUB_REPO}"


def _version_tuple(v: str):
    """'v1.2.3' -> (1, 2, 3)；解析失败返回空元组。"""
    m = re.match(r"^[vV]?(\d+(?:\.\d+)*)", (v or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def is_newer(remote: str, local: str = APP_VERSION) -> bool:
    rt, lt = _version_tuple(remote), _version_tuple(local)
    if not rt:
        return False
    if not lt:
        return True
    # 逐段比较，短的补 0：1.2 == 1.2.0
    n = max(len(rt), len(lt))
    rt += (0,) * (n - len(rt))
    lt += (0,) * (n - len(lt))
    return rt > lt


def _pick_asset(release: dict):
    """从 release 附件里挑 MergeSearch 的 Windows exe（不含源码 zip）。"""
    pat = re.compile(r"mergesearch.*\.exe$", re.IGNORECASE)
    for asset in release.get("assets", []) or []:
        name = asset.get("name", "")
        if pat.match(name) and asset.get("browser_download_url"):
            return asset
    return None


async def _fetch_release(client: httpx.AsyncClient):
    resp = await client.get(f"{API_BASE}/releases/latest", headers={
        "Accept": "application/vnd.github+json",
    })
    resp.raise_for_status()
    return resp.json()


async def check_update(proxy_cfg: dict) -> dict:
    """检查最新 release。返回 {has_update, current, latest, notes, size, url, name}。"""
    from proxy_router import ProxyRouter

    router = ProxyRouter(proxy_cfg)
    url = f"{API_BASE}/releases/latest"
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True,
                                 **router.proxy_httpx_kwargs(url)) as client:
        release = await _fetch_release(client)

    latest = (release.get("tag_name") or "").strip()
    asset = _pick_asset(release)
    notes = (release.get("body") or "").strip() or "（无更新说明）"
    info = {
        "has_update": is_newer(latest) and asset is not None,
        "current": APP_VERSION,
        "latest": latest,
        "notes": notes[:2000],
        "size": asset.get("size") if asset else None,
        "url": asset.get("browser_download_url") if asset else None,
        "name": asset.get("name") if asset else None,
    }
    logger.info("检查更新: 当前 v%s, 最新 %s%s", APP_VERSION, latest,
                "" if asset else "（未找到 exe 附件）")
    return info


async def download_update(url: str, proxy_cfg: dict, progress=None) -> str:
    """下载新版本 exe 到临时文件，返回本地路径。progress(done, total) 回调进度。"""
    from proxy_router import ProxyRouter

    router = ProxyRouter(proxy_cfg)
    target = os.path.join(app_dir(), "MergeSearch.update.exe")

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=600.0), follow_redirects=True,
                                 **router.proxy_httpx_kwargs(url)) as client:
        done = 0
        tmp = target + ".part"
        with open(tmp, "wb") as f:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length") or 0)
                async for chunk in resp.aiter_bytes(64 * 1024):
                    f.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        os.replace(tmp, target)
    logger.info("更新已下载: %s (%d bytes)", target, done)
    return target


def apply_update(downloaded_path: str) -> dict:
    """
    用下载好的 exe 替换当前程序并重启（仅打包 exe 环境）。
    策略: 写一个批处理脚本，等本进程退出后 rename 替换、清理、重启。
    """
    if not getattr(sys, "frozen", False):
        return {"ok": False, "msg": "仅打包后的 exe 支持自动更新（源码运行请自行 git pull）"}

    exe = sys.executable
    exe_dir = app_dir()
    new_exe = downloaded_path
    if not os.path.exists(new_exe):
        return {"ok": False, "msg": "更新文件不存在"}

    # MergeSearch.update.exe -> 等旧进程退出 -> 替换 -> 删旧文件 -> 启动新版本
    bat = os.path.join(exe_dir, "_update.bat")
    with open(bat, "w", encoding="gbk", errors="replace") as f:
        f.write("@echo off\r\n")
        f.write(f"cd /d \"{exe_dir}\"\r\n")
        f.write("timeout /t 2 /nobreak >nul\r\n")
        f.write(f'copy /y "{new_exe}" "{exe}" >nul 2>&1\r\n')
        f.write(f'del /f /q "{new_exe}" >nul 2>&1\r\n')
        f.write(f'del /f /q "{bat}" >nul 2>&1\r\n')
        f.write(f'start "" "{exe}"\r\n')
    os.startfile(bat)  # noqa
    return {"ok": True, "msg": "更新即将应用，程序将自动重启"}


def run_check_and_download(proxy_cfg: dict) -> dict:
    """同步包装（JsApi 调用用）。检查 + 不下载，仅返回信息。"""
    return asyncio.run(check_update(proxy_cfg))


def run_download(url: str, proxy_cfg: dict) -> dict:
    return {"ok": True, "path": asyncio.run(download_update(url, proxy_cfg))}


def download_update_sync(url: str, proxy_cfg: dict, progress=None) -> str:
    """线程内直接跑协程的同步下载（已有事件循环的线程不要用这个）。"""
    return asyncio.run(download_update(url, proxy_cfg, progress))


def run_apply(path: str) -> dict:
    return apply_update(path)
