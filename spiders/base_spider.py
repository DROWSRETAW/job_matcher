# -*- coding: utf-8 -*-
"""
爬虫基类
--------
定义 spider 的统一接口与公共资源（HTTP Session、日志器）。
子类只需声明站点信息，并实现 parse()。

设计说明：
    基类**不**提供通用的「遍历 URL 抓取」流程。因为不同站点的抓取策略
    差异很大：本项目对接的厦大就业网需要先按筛选参数遍历检索页
    （/job/search 的 PATH 风格分页），再对命中的职位逐个补详情页——
    这个两段式流程里，分页规则、筛选参数编码、列表页内嵌数据的解码
    方式都是站点专有的。把它们塞进基类只会产生「基类实现用不上、
    子类全量覆写」的空壳代码。

    parse() 在本项目里负责的是**详情页**解析（列表页解析见
    XmuCareerSpider.parse_list），因为只有详情页才有「需求专业」这个
    匹配打分的核心字段。
"""
import logging
from abc import ABC, abstractmethod
from typing import List

import requests

from core.models import Job
from core.fetcher import build_session


class BaseSpider(ABC):
    """
    爬虫抽象基类。

    使用方式：
        class XxxSpider(BaseSpider):
            name = "xxx"
            base_url = "https://..."

            def parse(self, html, url): ...   # 必须实现
    """

    name: str = "base"
    base_url: str = ""
    encoding: str = None  # 如站点编码异常，子类可指定，例："utf-8"

    def __init__(self):
        self.session: requests.Session = build_session()
        self.logger = logging.getLogger(f"spider.{self.name}")

    @abstractmethod
    def parse(self, html: str, url: str) -> List[Job]:
        """
        解析单个页面的 HTML，返回岗位列表。
        必须由子类实现——不同站点 DOM 结构不同，无法通用。
        """
        raise NotImplementedError
