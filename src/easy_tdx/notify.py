"""公共通知模块（docs/trading_system_tasks.md P0-T3 抽取）。

从 `scripts/track_161129_premium.py` 内联实现抽出，供各定时任务复用：
钉钉机器人 webhook（text 消息）优先，未配置回退 macOS 桌面通知。

钉钉机器人要求消息内容含配置的关键词，调用方标题须带"预警"。
webhook 配置优先级：环境变量 `CUSTOM_WEBHOOK_URLS`（逗号分隔）>
`<EASY_TDX_CONFIG_DIR|custom_webhook_urls>` 配置文件（每行一个，# 注释）。
cron/launchd 环境不继承 shell 环境变量，配置文件保证定时任务可用。
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from pathlib import Path

from easy_tdx import config as _config


def _config_dir() -> Path:
    return _config.config_dir()


def webhook_urls() -> list[str]:
    """webhook 地址列表：环境变量 `CUSTOM_WEBHOOK_URLS`（逗号分隔）优先，
    其次 `<EASY_TDX_CONFIG_DIR>/custom_webhook_urls` 配置文件（每行一个，# 开头为注释）。"""
    urls = [u.strip() for u in os.environ.get("CUSTOM_WEBHOOK_URLS", "").split(",") if u.strip()]
    if not urls:
        conf = _config_dir() / "custom_webhook_urls"
        if conf.exists():
            urls = [
                u.strip()
                for u in conf.read_text(encoding="utf-8").splitlines()
                if u.strip() and not u.startswith("#")
            ]
    return urls


def notify(title: str, text: str) -> None:
    """发送通知：钉钉机器人 webhook（text 消息）优先，未配置退回 macOS 桌面通知。

    打印一律 flush：launchd 重定向到文件时 stdout 为块缓冲，不 flush 预警行
    会憋在缓冲区（实测 2026-09-02：告警已发送但日志 4 分钟后才落盘）。
    """
    print(f"[通知] {title}: {text}", flush=True)
    urls = webhook_urls()
    if urls:
        payload = json.dumps({"msgtype": "text", "text": {"content": f"{title}\n{text}"}}).encode()
        for url in urls:
            try:
                req = urllib.request.Request(
                    url, data=payload, headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = json.loads(resp.read().decode("utf-8", "ignore"))
                    if body.get("errcode") != 0:
                        print(
                            f"[钉钉发送失败] errcode={body.get('errcode')} "
                            f"errmsg={body.get('errmsg')}",
                            flush=True,
                        )
            except Exception as e:
                print(f"[钉钉发送失败] {e}", flush=True)
    else:
        try:
            subprocess.run(
                ["osascript", "-e", f'display notification "{text}" with title "{title}"'],
                check=True,
                timeout=10,
                capture_output=True,
            )
        except Exception:
            pass
