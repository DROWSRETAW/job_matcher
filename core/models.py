# -*- coding: utf-8 -*-
"""
数据模型定义
------------
定义岗位数据的标准结构，贯穿「抓取 -> 存储 -> 打分 -> 导出」全链路。
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime


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
