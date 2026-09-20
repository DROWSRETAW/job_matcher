# -*- coding: utf-8 -*-
"""
HTTP 请求工具
-------------
统一处理请求头、重试、限速，所有 spider 复用。
反爬礼貌约定：每次请求间隔 REQUEST_DELAY_SECONDS，失败指数退避重试。
"""
import time
import random
import logging
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import (
    USER_AGENTS,
    REQUEST_DELAY_SECONDS,
    REQUEST_TIMEOUT,
    MAX_RETRIES,
)

logger = logging.getLogger(__name__)

_last_request_time = 0.0


def build_session() -> requests.Session:
    """构建带重试策略的 Session"""
    session = requests.Session()

    retry_strategy = Retry(
        total=MAX_RETRIES,
        backoff_factor=1,          # 重试间隔：1s, 2s, 4s
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    return session


def _throttle():
    """请求节流：保证相邻两次请求间隔不小于配置值"""
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < REQUEST_DELAY_SECONDS:
        time.sleep(REQUEST_DELAY_SECONDS - elapsed)
    _last_request_time = time.time()


def fetch(
    url: str,
    session: Optional[requests.Session] = None,
    encoding: str = None,
) -> Optional[str]:
    """
    发起 GET 请求并返回 HTML 文本。

    :param url: 目标地址
    :param session: 复用的 Session，不传则新建
    :param encoding: 强制指定编码（部分站点返回编码有误，需手动指定）
    :return: HTML 文本；失败返回 None
    """
    session = session or build_session()
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    _throttle()

    try:
        resp = session.get(
            url, headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()

        if encoding:
            resp.encoding = encoding
        elif resp.encoding in (None, "ISO-8859-1"):
            # 中文站点常见编码问题，优先按 apparent_encoding 处理
            resp.encoding = resp.apparent_encoding

        return resp.text

    except requests.RequestException as e:
        logger.warning("请求失败 %s -> %s", url, e)
        return None
