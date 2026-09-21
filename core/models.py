# -*- coding: utf-8 -*-
"""
数据模型定义
------------
定义岗位数据的标准结构，贯穿「抓取 -> 存储 -> 打分 -> 导出」全链路。
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
import hashlib


@dataclass
class Job:
    """标准化的岗位数据结构"""

    # ---- 基础字段（抓取阶段填充）----
    company: str = ""            # 公司名称
    title: str = ""              # 岗位名称
    city: str = ""               # 工作地点
    salary: str = ""             # 薪资
    education: str = ""          # 学历要求
    major_requirement: str = ""  # 专业要求
    apply_method: str = ""       # 投递方式 / 链接
    deadline: str = ""           # 截止时间
    source: str = ""             # 数据来源站点
    url: str = ""                # 原文链接

    # ---- 打分阶段填充 ----
    match_score: int = 0                 # 匹配总分
    match_level: str = ""                # 匹配等级 S/A/B/C
    match_label: str = ""                # 等级中文说明
    hit_keywords: list = field(default_factory=list)  # 命中的关键词

    # ---- 数据血缘与增量标识（2026-09-21 新增）----
    source_job_id: str = ""      # 站点侧职位 ID，如 "2401083"
    job_key: str = ""            # 稳定业务主键，如 "xmu:2401083"
    publish_date: str = ""       # 列表页的发布日期（独立于 deadline）
    list_hash: str = ""          # 列表字段指纹：增量抓取靠它判断「有没有变」
    detail_fetched: bool = False  # 本条是否真的抓过详情页（False=字段来自回填）

    # ---- 元数据 ----
    crawl_time: str = field(
        default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )

    def to_dict(self) -> dict:
        """转为字典（用于写库 / 导出）"""
        d = asdict(self)
        d["hit_keywords"] = ",".join(self.hit_keywords)
        return d

    def is_valid(self) -> bool:
        """基础校验：公司和岗位名都非空才算有效数据"""
        return bool(self.company.strip()) and bool(self.title.strip())


# ===================================================================
# 内容指纹 —— 增量抓取的地基
# ===================================================================
# 【为什么用指纹，而不是逐字段比对】
# 判断「这条岗位跟上次抓到的相比有没有变」，最直觉的做法是把字段一个个
# 比一遍。但字段会越加越多（这次就新加了 publish_date），每加一个都得
# 记得去改比对逻辑，漏掉一个就静默失效——这类「能力造出来了但没接线」
# 的 bug 在本项目里已经出现过一次。
# 改成算指纹：参与比对的字段固定成一份清单，拼串后取哈希。以后加字段
# 只改这一份清单，增量判定与变更检测自动跟上。
#
# 【为什么需要两个指纹】
#   LIST_FINGERPRINT_FIELDS    只有列表页能提供的字段
#       → 增量判定用它。因为增量跑只请求列表页，详情字段此时还是空的。
#   CONTENT_FINGERPRINT_FIELDS 列表 + 详情全部字段
#       → 变更检测用它，判断这条岗位的完整内容相对上一条快照是否真的变了。
#
# 两个混用都会出问题：拿内容指纹做增量判定，详情字段一空就被判成
# 「变了」，每次都会全量重抓；拿列表指纹做变更检测，则详情页的专业
# 要求改了也检测不到。

LIST_FINGERPRINT_FIELDS = (
    "company", "title", "city", "salary", "education", "publish_date",
)
CONTENT_FINGERPRINT_FIELDS = LIST_FINGERPRINT_FIELDS + (
    "major_requirement", "deadline",
)


def fingerprint(job: "Job", fields) -> str:
    """
    对指定字段求稳定指纹。

    实现细节：
      · 用 \\x1f（ASCII 单元分隔符）拼串，而不是逗号或竖线——岗位标题里
        本来就常出现「、」「,」「|」，用它们做分隔符会产生歧义：
        ("A|B", "C") 与 ("A", "B|C") 会算出同一个指纹。
      · 每个字段带上字段名（f"title=xxx"），字段错位也能被发现。
      · 取 md5 前 16 位：库内自用，不涉及安全场景，够用且短。
    """
    parts = []
    for name in fields:
        value = getattr(job, name, "") or ""
        parts.append(f"{name}={str(value).strip()}")
    raw = "\x1f".join(parts)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def list_fingerprint(job: "Job") -> str:
    """列表字段指纹：增量判定的依据（详情页字段不参与）"""
    return fingerprint(job, LIST_FINGERPRINT_FIELDS)


def content_fingerprint(job: "Job") -> str:
    """全字段指纹：变更检测的依据"""
    return fingerprint(job, CONTENT_FINGERPRINT_FIELDS)


def make_job_key(source_job_id: str, source_key: str = "xmu") -> str:
    """
    构造稳定业务主键。

    为什么要独立于「公司+岗位名」：
        老库用 UNIQUE(company, title) 去重，一旦站点把岗位名从
        「27届校招-游戏数值策划」改成「游戏数值策划」，就会被当成
        一条全新岗位重复入库——实际发生过。职位 ID 才是站点侧真正的
        主键，公司改行、标题改名，它都不变。
    """
    sid = str(source_job_id or "").strip()
    return f"{source_key}:{sid}" if sid else ""
