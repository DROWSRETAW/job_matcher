# -*- coding: utf-8 -*-
"""
内嵌数据解码器
==============
厦门大学就业信息网（智联系「就业管理系统」模板）的**列表页不会把职位列表
直接写在 HTML 里**，而是压缩后内嵌进页面。这一点是本项目改造中踩过的坑：

    直接对 /job/search 的返回 HTML 做 BeautifulSoup 解析，
    会得到 0 个职位链接——不是没有数据，而是数据被编码了。

真实的编码链条（2026-09-20 实测）：

    <script>
        $("#content2730").each(function(){
            $(this).replaceWith(Base64.decode(unzip("eJztnUl3osAW...")));
        });
    </script>

    unzip(x)      等价于 Python 的 zlib.decompress（zlib 头 0x78 0x9c，
                  所以外层 base64 常以 "eJ" 开头）
    Base64.decode 标准 base64 解码（站点引入 /static/js/base64.min.js）

第 1 层解压后得到：

    "view2d " + <另一个长 base64>
    （"view2d" 是模板里的视图名，空格后紧跟内层数据）

第 2 层解码后才得到真正的职位列表 DOM：

    <div class="job-box"><ul class="list">
      <li data-id="2401083">
        <div class="left"><div class="job">
          <div class="company"><a href="/company/view/id/1107806">厦门天马微电子有限公司</a>
            <div><ul><li>制造业</li><li>10000人以上</li></ul></div></div>
          <div class="name"><a href="/job/view/id/2401083" title="...">研发类、智能制造类…</a>
            <span>2026-09-20</span></div>
          <div class="salary"><p class="text-orange">9000-25000</p>
            <ul><li>福建省厦门市</li><li>全职</li><li>本科</li></ul></div>
        </div></div>
      </li>
      ...

判断一个页面是否用这套编码的快速特征：
  1. HTML 里出现 base64.min.js / pako / LZString
  2. 页面文本里有 "Base64.decode" / "unzip("
  3. BeautifulSoup 解析出的条目为 0，但页面体积很大（>100KB）

【为什么不写在 spider 里】
    这是**站点传输层的编码问题**，与「厦门大学就业网」的业务逻辑无关。
    任何字段改动都不应影响它；单独成模块也便于单元测试和解码失败时定位。
"""
import base64
import re
import zlib
from typing import Optional

# 页面中的长 base64 串（最短 200 字符，避免命中普通字符串）
_B64_CANDIDATE = re.compile(r"""['"]([A-Za-z0-9+/=]{200,})['"]""")

# 第 1 层解压后的 "视图名 + 内层base64" 结构
_INNER_WRAPPER = re.compile(r"([A-Za-z0-9_]+)\s+([A-Za-z0-9+/=]{60,})")


def _pad(b64: str) -> str:
    """补齐 base64 的 = 填充位（站点拼接时可能被截断）"""
    return b64 + "=" * (-len(b64) % 4)


def unzip_base64(chunk: str) -> Optional[str]:
    """
    第 1 层：base64 解码 + zlib 解压，返回文本；失败返回 None。

    同时兜底尝试 raw-deflate（wbits=-15）与 gzip（wbits=47），
    因为不同模板对 zlib 头部的处理不完全一致。
    """
    try:
        raw = base64.b64decode(_pad(chunk))
    except Exception:
        return None

    for wbits in (15, -15, 47):
        try:
            return zlib.decompress(raw, wbits).decode("utf-8", "replace")
        except Exception:
            continue
    return None


def decode_embedded_html(html: str) -> str:
    """
    从页面 HTML 中取出内嵌的列表 HTML（两层解码）。

    :param html: /job/search 等页面返回的原始 HTML
    :return: 列表片段 HTML；若页面不含内嵌数据则返回空字符串

    实现要点：
      - 逐个尝试候选 base64 串，任何一个能解出「视图名 + base64」结构即采用；
      - 第 2 层解码失败时退回第 1 层结果（至少保留可读文本，便于排障）。
    """
    if not html:
        return ""

    for chunk in _B64_CANDIDATE.findall(html):
        lv1 = unzip_base64(chunk)
        if not lv1:
            continue

        m = _INNER_WRAPPER.search(lv1)
        if not m:
            # 只有一层：直接就是列表 HTML
            return lv1

        try:
            lv2 = base64.b64decode(_pad(m.group(2))).decode("utf-8", "replace")
        except Exception:
            return lv1

        return lv2

    return ""


def probe_encoding(html: str) -> dict:
    """
    诊断用：报告页面的编码情况。用于解码失败时快速定位问题。

    :return: {"has_candidate": bool, "candidate_len": int,
              "layer1_ok": bool, "layer1_len": int,
              "wrapper": str, "layer2_ok": bool, "layer2_len": int}
    """
    info = {
        "has_candidate": False, "candidate_len": 0,
        "layer1_ok": False, "layer1_len": 0,
        "wrapper": "", "layer2_ok": False, "layer2_len": 0,
    }
    if not html:
        return info

    cands = _B64_CANDIDATE.findall(html)
    if not cands:
        return info
    info["has_candidate"] = True
    info["candidate_len"] = len(cands[0])

    lv1 = unzip_base64(cands[0])
    if not lv1:
        return info
    info["layer1_ok"] = True
    info["layer1_len"] = len(lv1)

    m = _INNER_WRAPPER.search(lv1)
    if not m:
        return info
    info["wrapper"] = m.group(1)

    try:
        lv2 = base64.b64decode(_pad(m.group(2))).decode("utf-8", "replace")
    except Exception:
        return info
    info["layer2_ok"] = True
    info["layer2_len"] = len(lv2)
    return info
