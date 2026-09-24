# -*- coding: utf-8 -*-
"""
厦门大学就业信息网爬虫（检索接口版）
====================================

站点：https://jy.xmu.edu.cn

────────────────────────────────────────────────────────────────
【这一版为什么重写】
────────────────────────────────────────────────────────────────
旧版本（ID 扫描版）走了一条弯路，记录在此以免重蹈覆辙：

❌ 错误策略：以「种子职位 ID」为中心做 ±N 的双向枚举
   DEFAULT_SEED_ID = 2397816，每个方向扫 30 个 ID，共约 61 次请求。
   代码注释里甚至写了「本站点的职位页无法按关键词检索，只能按职位
   ID 枚举」——**这个结论是错的**。

   错误带来的实际问题：
   1. 命中率极低：相邻 ID 未必是有效职位，更不是目标城市的职位；
   2. 结果不可控：抓到哪些城市的岗位全凭 ID 连续性，无法指定条件；
   3. 无法覆盖：厦门在招岗位分布在很宽的 ID 区间，扫 61 个 ID
      最多碰到个位数岗位，而实际有几百条；
   4. 无法表达业务条件：学历、工作性质、发布时间这些筛选维度，
      站点本来就支持，却被绕过去了。

✅ 正确策略：用站点自带的检索接口
   GET /job/search，参数以 PATH 形式拼接，支持 13 个筛选维度。

   实测（2026-09-20，每页 20 条）：
       全国（无限定）        195 页  ≈ 3900 条
       厦门                   29 页  ≈  580 条
       厦门 + 本科            16 页  ≈  320 条
       厦门 + 本科 + 全职      15 页  ≈  300 条
       厦门+本科+全职+近1月     9 页  ≈  180 条
       厦门 + 本科 + 专业      1-2 页 ≈  30 条

   代价：厦门+本科全量约 16 次列表请求（约 25 秒），
   远低于旧版扫 61 个 ID 却只能拿到几条。

────────────────────────────────────────────────────────────────
【站点结构事实（2026-09-20 实测）】
────────────────────────────────────────────────────────────────
1) 检索页 /job/search 的参数全集（表单 method=get）：
       title         关键词 —— ★需要登录，会 302 到统一身份认证，禁用
       city          地区，行政区划代码（厦门=350200）
       d_education   学历 101本科 / 102硕士 / 103博士
       d_category    工作性质 100全职 / 102实习
       min_salary    最低薪资（数值，如 10000）
       nature        单位性质 10机关 / 31国企 / 39其他企业 …
       scale         单位规模 100少于50人 … 106一万以上
       time          发布时间 0不限 / 1近1天 / 3近3天 / 7近1周 / 30近1月
       d_skill       职能类别
       d_industry    行业类别
       d_major       需求专业 —— ★值为 "s<专业代码>"，多选逗号连接
                     如 s112003,s112017（112003=信息与计算科学）

2) URL 两种写法都有效，但**分页只有 path 形式验证通过**：
       path : /job/search/city/350200/d_education/101/page/2
       query: /job/search?city=350200&d_education=101   （分页行为未验证）
   → 本实现统一使用 path 形式。

3) 列表数据不在 HTML 里，而是两层 base64+zlib 压缩内嵌，
   必须经 core.decoder.decode_embedded_html() 解码。这是最容易踩的坑：
   直接解析 HTML 会得到 0 条，看起来像「JS 动态渲染抓不到」。

4) 列表页的每条 <li data-id="..."> 只含：
       公司名 / 行业 / 单位规模 / 岗位名 / 发布日期 / 薪资 / 城市 / 性质 / 学历
   ★**不含「需求专业」**——而需求专业是本项目匹配打分的核心依据
   （权重表里「信息与计算科学」10 分、「数学类」8 分）。
   所以详情页 /job/view/id/{jid} 的解析必须保留。

5) 分页边界：越界页码（如 /page/30 而共 29 页）**不报错**，
   而是返回最后一页的内容。因此不能靠「下一>一页是否重复」判断结束，
   必须在第 1 页就解析出最大页码，再按页码遍历。

────────────────────────────────────────────────────────────────
【两段式抓取流程】
────────────────────────────────────────────────────────────────
    第 1 段  检索页遍历（快，16 次请求）
             按 city / education / category / time / major 精确取列表，
             拿到 jid + 公司 + 岗位 + 薪资 + 城市 + 性质 + 学历

    第 2 段  详情页补全（慢，每岗位 1 次请求）
             对每个 jid 抓 /job/view/id/{jid}，补「需求专业」与截止时间

    第 3 段  本地打分排序（core/matcher.py，与本次改造无关，保持不变）

站点筛选只能做「硬匹配」，算不出「综合匹配度」；本地的关键词加权打分
仍然是项目的差异化价值所在——两者分工，不互相替代。

────────────────────────────────────────────────────────────────
【增量运行（2026-09-21 新增）】
────────────────────────────────────────────────────────────────
第 2 段是全部开销的大头：每岗位 1 次详情请求、每次节流 1.6 秒，
全量 300 条就要 8 分钟，占整个流程耗时的 94%。
但这些详情字段在两次抓取之间绝大多数不会变，所以默认改成按需抓：

    第 1 段列表页  照常全量翻 —— 它是「有没有新岗位」的唯一来源，
                   而且只要 20 多秒，省它没有意义
    第 2 段详情页  只抓「该抓的」：
                      · 新出现的职位
                      · 列表字段（标题/薪资/城市/学历/发布日期）有变的
                      · 历史上从没抓到过「需求专业」的
                      · 距上次抓详情超过 TTL（默认 7 天）的
                   其余跳过，专业与截止时间从 ODS 历史快照回填

实测（厦门+本科+全职，20 条岗位）：
    第 1 次（全新建库）  列表 1 次 + 详情 20 次   约 40 秒
    第 2 次（同一条件）  列表 1 次 + 详情 0 次    约 1 秒

两点必须记住：
  1. **跳过详情 ≠ 丢字段**。被跳过的岗位会从 ODS 里「最近一次真正抓到
     详情的快照」回填 major_requirement / deadline（见
     core/incremental.apply_backfill）。回填缺失会把数据源越跑越空。
  2. **抓失败也要回填**。网络抖动导致这轮没抓到，不代表这岗位没有专业
     要求——此时必须用历史值兜住，否则一次失败就把库里的字段抹成空。

增量决策本身不写在爬虫里，而是 core/incremental.py 的纯函数
plan_detail_fetch()（输入列表结果 + ODS 状态，输出抓取计划）。
好处是全部判定分支都能脱离网络单测；混进这里就必须造 HTTP 才能测。
爬虫只负责执行计划：enrich_by_plan()。

────────────────────────────────────────────────────────────────
【合规声明】
- 仅抓取公开可访问页面，不登录、不绕过验证码
- 站点 robots.txt 不存在（404），无显式禁止
- 严格节流（列表/详情请求间隔均 ≥1.5 秒）
- 抓到目标量即停；分页上限 SEARCH_MAX_PAGES 兜底
- 数据仅供个人求职使用
────────────────────────────────────────────────────────────────
"""
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote

# 允许以脚本方式直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup

from config import (
    SEARCH_MAX_PAGES,
    CITY_CODES, EDUCATION_CODES, CATEGORY_CODES, TIME_CODES,
    NATURE_CODES, SCALE_CODES, MAJOR_CODES, DEFAULT_MAJOR_KEYS,
    DETAIL_MAX_CONSECUTIVE_FAILURES,
)
from core.decoder import decode_embedded_html, probe_encoding
from core.models import Job, make_job_key, list_fingerprint
from core.fetcher import fetch, build_session
from spiders.base_spider import BaseSpider


# ===================================================================
# 检索条件
# ===================================================================
@dataclass
class SearchProfile:
    """
    站点侧检索条件。只描述「向站点索取哪一批岗位」，不含本地打分与筛选。

    每个字段都对应 /job/search 的一个 URL 参数（见模块头部说明）。
    取值为**中文名**（如 "厦门"、"本科"），由 to_path() 翻译成站点代码。
    也允许直接填代码（如 city="350200"），便于临时试验。
    """
    city: str = "厦门"
    education: str = "本科"
    category: str = "全职"
    majors: List[str] = field(default_factory=lambda: list(DEFAULT_MAJOR_KEYS))
    time_range: str = "不限"
    salary_min: int = 0
    nature: str = ""
    scale: str = ""
    max_pages: Optional[int] = None

    # ---------------------------------------------------------------
    def _major_param(self) -> str:
        """
        把专业中文名翻译成站点参数值。

        站点格式：每个专业带前缀 s，多选用逗号连接。
            ["信息与计算科学", "数学类"] -> "s112003,s112017"
        """
        codes = []
        for name in self.majors:
            code = MAJOR_CODES.get(name)
            if code:
                codes.append(f"s{code}")
            elif re.fullmatch(r"s?\d{6}", str(name)):
                # 已经是代码，直接兼容
                codes.append(name if str(name).startswith("s") else f"s{name}")
        return ",".join(codes)

    def to_path(self, page: int = 1) -> str:
        """
        构造检索页 URL（PATH 形式）。

        站点原生分页链接就是这个形状：
            /job/search/city/350200/d_education/101/page/2
        """
        parts = ["/job/search"]

        def add(name: str, value) -> None:
            if value in (None, "", 0):
                return
            # 逗号必须编码成 %2C，否则多专业参数会被截断
            parts.extend([name, quote(str(value), safe="")])

        add("city", CITY_CODES.get(self.city, self.city))
        add("d_education", EDUCATION_CODES.get(self.education, self.education))
        add("d_category", CATEGORY_CODES.get(self.category, self.category))
        # 时间参数特殊：站点的 "0" 表示不限时间，等价于不传，故跳过以保持 URL 干净
        time_code = TIME_CODES.get(self.time_range, self.time_range)
        if time_code and time_code != "0":
            add("time", time_code)
        add("d_major", self._major_param())
        add("min_salary", self.salary_min)
        add("nature", NATURE_CODES.get(self.nature, self.nature))
        add("scale", SCALE_CODES.get(self.scale, self.scale))

        if page and page > 1:
            parts.extend(["page", str(page)])

        return "/".join(parts)

    @classmethod
    def for_luge(cls) -> "SearchProfile":
        """卢兄的默认投递口径：厦门 + 本科 + 全职 + 宽口径专业"""
        return cls(
            city="厦门",
            education="本科",
            category="全职",
            majors=list(DEFAULT_MAJOR_KEYS),
        )

    def describe(self) -> List[str]:
        items = [f"城市={self.city}", f"学历={self.education}",
                 f"性质={self.category}", f"发布={self.time_range}"]
        if self.majors:
            items.append("专业=" + "/".join(self.majors))
        if self.salary_min:
            items.append(f"月薪≥{self.salary_min}")
        if self.nature:
            items.append(f"单位性质={self.nature}")
        if self.scale:
            items.append(f"单位规模={self.scale}")
        return items

    # ---------------------------------------------------------------
    # 序列化：让一套检索条件可以存下来反复用
    # （与 core.filters.JobFilter 的方案文件机制对称）
    # ---------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "city": self.city,
            "education": self.education,
            "category": self.category,
            "majors": self.majors,
            "time_range": self.time_range,
            "salary_min": self.salary_min,
            "nature": self.nature,
            "scale": self.scale,
            "max_pages": self.max_pages,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "SearchProfile":
        if not data:
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path) -> str:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        import json
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return str(p)

    @classmethod
    def load(cls, path) -> "SearchProfile":
        import json
        p = Path(path)
        if not p.exists():
            return cls()
        return cls.from_dict(json.loads(p.read_text(encoding="utf-8")))


# ===================================================================
# 爬虫
# ===================================================================
class XmuCareerSpider(BaseSpider):
    """厦门大学就业信息网爬虫（基于 /job/search 检索接口）"""

    name = "xmu_career"
    base_url = "https://jy.xmu.edu.cn"
    encoding = "utf-8"

    # 职位详情页（用于补「需求专业」）
    JOB_URL = "https://jy.xmu.edu.cn/job/view/id/{jid}"

    def __init__(
        self,
        profile: Optional[SearchProfile] = None,
        fetch_detail: bool = True,
        detail_delay: float = 1.6,
        detail_limit: Optional[int] = None,
    ):
        """
        :param profile:      检索条件；不传则用卢兄的默认口径
        :param fetch_detail: 是否抓详情页补「需求专业」（关掉则打分精度下降）
        :param detail_delay: 详情页请求间隔（秒）
        :param detail_limit: 最多补多少条详情（None=全部），用于快速试跑
        """
        super().__init__()
        self.profile = profile or SearchProfile.for_luge()
        self.fetch_detail = fetch_detail
        self.detail_delay = detail_delay
        self.detail_limit = detail_limit

        # 运行统计，供验证与排查（run() 结束后可读）
        self.stats: Dict[str, int] = {
            "list_requests": 0,
            "list_items": 0,
            "total_pages": 0,
            "detail_requests": 0,
            "detail_ok": 0,
            "detail_major_found": 0,
            # ---- 增量相关（2026-09-21 新增）----
            "detail_skipped": 0,     # 因增量判定而省下的详情请求数
            "backfilled": 0,         # 用历史快照回填了专业字段的岗位数
            # ---- 止损相关（2026-09-23 新增）----
            "detail_aborted": 0,     # 因连续失败而放弃抓取的详情条数
        }

    # -----------------------------------------------------------------
    # 主流程
    # -----------------------------------------------------------------
    def run(self, plan=None) -> List[Job]:
        """
        完整抓取：检索页取列表 -> 详情页补专业。

        :param plan: core.incremental.DetailPlan（可选）。
            不传 → 按 fetch_detail 全量抓详情（改造前的行为）。
            传入 → 只抓计划中标记要抓的岗位，其余从历史快照回填。
            增量决策本身不在这里做：plan 要基于列表结果和 ODS 状态
            才能算出来，属于编排层的职责（见 main.py）。
        :return: 岗位列表（未打分，打分在 core/matcher.py）
        """
        jobs = self.search(self.profile)
        if not jobs:
            self.logger.warning("检索结果为空，请检查检索条件是否正确")
            return []

        if plan is not None:
            self.enrich_by_plan(plan)
        elif self.fetch_detail:
            self.enrich_majors(jobs)

        self.finalize(jobs)

        self.logger.info(
            "抓取完成：%d 个岗位（列表请求 %d 次，详情请求 %d 次，"
            "增量省下 %d 次详情请求）",
            len(jobs), self.stats["list_requests"],
            self.stats["detail_requests"], self.stats["detail_skipped"])
        return jobs

    # =================================================================
    # 第 1 段：检索页遍历
    # =================================================================
    def search(self, profile: Optional[SearchProfile] = None) -> List[Job]:
        """
        按条件遍历检索页，返回列表页能提供的全部岗位。

        分页策略（重要）：
            第 1 页解析出「最大页码」后，按 1..max_page 顺序遍历。
            不能靠「下一页内容是否重复」判断结束——越界页码返回的是
            最后一页内容（HTTP 200），会造成假死循环。
        """
        profile = profile or self.profile
        max_pages = profile.max_pages or SEARCH_MAX_PAGES

        self.logger.info("检索条件：%s", " | ".join(profile.describe()))

        all_jobs: List[Job] = []
        seen_ids = set()
        total_page = 1
        page = 1

        while page <= min(total_page, max_pages):
            path = profile.to_path(page)
            url = self.base_url + path
            inner = self._fetch_list_inner(url)

            if not inner:
                self.logger.warning("第 %d 页无内嵌数据，停止翻页（%s）", page, path)
                break

            if page == 1:
                total_page = self._parse_max_page(inner)
                self.stats["total_pages"] = total_page
                self.logger.info("第 1 页解析完成：共 %d 页（每页 20 条）", total_page)

            page_jobs = self.parse_list(inner, url)

            new_count = 0
            for job in page_jobs:
                jid = self._jid_of(job.url)
                if jid and jid in seen_ids:
                    continue
                if jid:
                    seen_ids.add(jid)
                all_jobs.append(job)
                new_count += 1

            self.logger.info("第 %d/%d 页：%d 条（新增 %d，累计 %d）",
                             page, total_page, len(page_jobs), new_count, len(all_jobs))

            if not page_jobs:
                break

            page += 1

        self.stats["list_items"] = len(all_jobs)
        return all_jobs

    def _fetch_list_inner(self, url: str) -> str:
        """请求检索页并解出内嵌的列表 HTML"""
        html = fetch(url, session=self.session, encoding=self.encoding)
        self.stats["list_requests"] += 1
        if not html:
            return ""

        inner = decode_embedded_html(html)
        if not inner:
            # 解码失败时给出可执行的诊断信息，而不是静默返回空
            info = probe_encoding(html)
            self.logger.error(
                "列表数据解码失败：页面 %d 字节，候选 base64=%s，"
                "第1层解压=%s。站点模板可能改版，请检查 core/decoder.py",
                len(html), info["candidate_len"], info["layer1_ok"],
            )
        return inner

    @staticmethod
    def _parse_max_page(inner_html: str) -> int:
        """
        从分页区解析最大页码。

        分页 HTML 形如：
            <div class="pages"><ul class="page">
              <li class="page selected"><a href=".../job/search/city/350200/...">1</a></li>
              <li class="page"><a href=".../page/2">2</a></li>
              ...
              <li class="page"><a href=".../page/29">29</a></li>
            </ul></div>

        注意：分页链接里可能夹着模板附加段
        （实测为 /do123/jy.xmu.edu.cn/domain/xdu），所以只提取 /page/N。
        """
        pages = [int(n) for n in re.findall(r"/page/(\d+)", inner_html)]
        return max(pages) if pages else 1

    # -----------------------------------------------------------------
    def parse_list(self, inner_html: str, page_url: str) -> List[Job]:
        """
        解析检索页的列表片段。

        单条 <li> 的真实结构（2026-09-20 实测）：
            <li data-id="2401083">
              <div class="right"><img .../></div>
              <div class="left"><div class="job">
                <div class="company">
                  <a href="/company/view/id/1107806">厦门天马微电子有限公司</a>
                  <div><ul><li>制造业</li><li>10000人以上</li></ul></div>
                </div>
                <div class="name">
                  <a href="/job/view/id/2401083" title="...">研发类…</a>
                  <span>2026-09-20</span>
                </div>
                <div class="salary">
                  <p class="text-orange">9000-25000</p>
                  <ul><li>福建省厦门市</li><li>全职</li><li>本科</li></ul>
                </div>
              </div></div>
            </li>

        ★ 用 BeautifulSoup 按 class 取，不要用跨层正则——
          列表项内部有嵌套的 <ul>/<li>（行业·规模和城市·性质·学历），
          惰性正则会提前闭合，导致薪资、城市静默变空。
        """
        soup = BeautifulSoup(inner_html, "lxml")
        jobs: List[Job] = []

        for li in soup.find_all("li", attrs={"data-id": True}):
            jid = (li.get("data-id") or "").strip()

            name_node = li.select_one(".name a")
            title = name_node.get_text(strip=True) if name_node else ""

            company_node = li.select_one(".company a")
            company = company_node.get_text(strip=True) if company_node else ""

            salary_node = li.select_one(".salary .text-orange") or li.select_one(".salary p")
            salary = salary_node.get_text(strip=True) if salary_node else ""

            # 薪资下方 <ul> 固定三项：城市 / 工作性质 / 学历
            meta = []
            meta_ul = li.select_one(".salary ul")
            if meta_ul:
                meta = [x.get_text(strip=True) for x in meta_ul.find_all("li")]
            city = meta[0] if len(meta) > 0 else ""
            education = meta[2] if len(meta) > 2 else ""

            # 公司名下方还有一个嵌套 <ul>：单位行业 / 单位规模
            # （2026-09-23 补。此前只取了 .salary 那个 ul，
            #   行业与规模被静默丢弃，导致「城市 × 行业」这个分析维度
            #   在设计上就不可能成立）
            # 注意用 `.company > div > ul` 限定在 company 块内，
            # 不要写 li.select("ul") 之类的宽匹配——列表项里有两个 ul。
            company_meta = []
            cm_ul = li.select_one(".company > div > ul") or li.select_one(".company ul")
            if cm_ul:
                company_meta = [x.get_text(strip=True) for x in cm_ul.find_all("li")]
            industry = company_meta[0] if len(company_meta) > 0 else ""
            company_scale = company_meta[1] if len(company_meta) > 1 else ""

            pub = li.select_one(".name span")
            publish_date = pub.get_text(strip=True) if pub else ""

            url = self.JOB_URL.format(jid=jid) if jid else page_url

            job = Job(
                company=company,
                title=title,
                city=city,
                salary=salary,
                education=education,
                major_requirement="",     # ★ 列表页没有，由详情页补
                deadline="",              # 同理；抓不到详情时用发布日期兜底
                publish_date=publish_date,  # 独立记录发布日期，供增量判定
                industry=industry,        # 列表页有，不依赖详情页
                company_scale=company_scale,
                # company_nature 列表页没有（那里的「全职」是工作性质，
                # 不是单位性质），只能等详情页
                apply_method=url,
                source="厦门大学就业信息网",
                url=url,
                # 列表页唯一稳定的主键是职位 ID（data-id 属性）
                source_job_id=jid,
                job_key=make_job_key(jid),
            )
            # 列表指纹在结果返回前就固定下来：
            # 详情页回填会改 deadline，若把指纹算在回填之后，
            # 下次抓取时的列表指纹（此时详情还没抓）就会与之不等，
            # 导致所有岗位被误判成「信息有变」而全量重抓。
            job.list_hash = list_fingerprint(job)

            if job.is_valid():
                jobs.append(job)

        return jobs

    # =================================================================
    # 第 2 段：详情页补「需求专业」
    # =================================================================
    def enrich_majors(self, jobs: List[Job]) -> None:
        """
        全量抓详情页，补齐列表页拿不到的「需求专业」（+ 截止时间）。

        :param jobs: 原地修改，回填 major_requirement / deadline
        """
        target = jobs
        if self.detail_limit:
            target = jobs[: self.detail_limit]
            self.logger.info("按 detail_limit 仅补前 %d 条详情", len(target))

        self._fetch_detail_loop(target)

    def enrich_by_plan(self, plan) -> None:
        """
        按增量计划抓详情页。

        :param plan: core.incremental.DetailPlan

        比 enrich_majors 多做三件事：
          1. 只抓计划里标记为「需要抓」的岗位（新增 / 信息有变 / 缺专业 / 超期）
          2. 跳过的岗位用 ODS 历史快照回填专业与截止时间
          3. 抓失败的岗位也用历史快照兜底 —— 抓取失败只代表这次没拿到，
             不代表这个岗位没有专业要求，不能因此把库里已有值抹掉

        为什么把决策写在 core/incremental.py 而不是这里：
            那是纯函数（输入列表 + 状态，输出计划），可以脱离网络单独
            单测全部判定分支；混进爬虫里就必须造 HTTP 才能测。
        """
        from core.incremental import apply_backfill, summarize_plan

        for line in summarize_plan(plan):
            self.logger.info("%s", line)

        self.stats["detail_skipped"] = plan.skipped_count

        target = plan.to_fetch
        if self.detail_limit:
            target = target[: self.detail_limit]
            self.logger.info("按 detail_limit 本批最多抓 %d 条详情", len(target))

        # 先回填被跳过的：它们的字段全部来自历史快照
        filled = apply_backfill(plan)
        self.stats["backfilled"] = filled
        if plan.skipped_count:
            self.logger.info(
                "跳过 %d 条详情，其中 %d 条已从历史快照回填专业字段",
                plan.skipped_count, filled)

        before_ok = self.stats["detail_ok"]
        self._fetch_detail_loop(target)

        failed = [j for j in target if not j.detail_fetched]
        if failed:
            rescued = 0
            for job in failed:
                recovery = plan.backfill.get(job.job_key)
                if recovery is None:
                    continue
                # 与 apply_backfill 同一套语义：只补空值。
                # 抓失败的岗位，这些字段在内存里本来就是空的，
                # 不补的话一次网络抖动就把库里的值抹成空串。
                for name, value in recovery.as_items():
                    if value and not getattr(job, name, ""):
                        setattr(job, name, value)
                if job.major_requirement:
                    rescued += 1
            self.logger.warning(
                "%d 条详情抓取失败（成功 %d 条），已用历史快照兜住 %d 条的专业字段；"
                "失败的下次运行会重试",
                len(failed), self.stats["detail_ok"] - before_ok, rescued)

    def _fetch_detail_loop(self, target: List[Job]) -> None:
        """
        逐条抓详情页并回填（enrich_majors 与 enrich_by_plan 共用的循环体）。

        只回填详情页才有的字段——公司名/岗位名保持列表页的值，
        因为它们参与数据库的去重判断，改写可能让同一岗位重复入库。

        单位属性按来源分两类（2026-09-23）：
            company_nature          只有详情页有 → 直接取详情值
            industry / company_scale 列表页也有 → 本次列表值优先，
                                     详情值只在列表页没给时兜底。
                                     反过来的话，同一条岗位会因为「抓没抓详情」
                                     而算出不同的内容指纹，变更统计会虚增。
        """
        if not target:
            return

        self.logger.info("开始抓取详情页补「需求专业」，共 %d 个岗位，"
                         "预计约 %.1f 分钟",
                         len(target), len(target) * self.detail_delay / 60)

        consecutive_failures = 0

        for i, job in enumerate(target, 1):
            jid = self._jid_of(job.url)
            if not jid:
                continue

            detail = self._fetch_job_page(jid)
            if detail:
                consecutive_failures = 0
                job.major_requirement = detail.major_requirement or ""
                if detail.deadline:
                    job.deadline = detail.deadline
                if detail.company_nature:
                    job.company_nature = detail.company_nature
                if detail.industry and not job.industry:
                    job.industry = detail.industry
                if detail.company_scale and not job.company_scale:
                    job.company_scale = detail.company_scale
                # 页面成功解析即算「这次真抓到了详情」。
                # 若页面本身没写专业要求（parse 会返回空列表），
                # detail_fetched 仍为 False，下次会再试一次。
                job.detail_fetched = True
                if job.major_requirement:
                    self.stats["detail_major_found"] += 1
            else:
                # 连续失败计数：偶发失败（个别岗位 404）靠增量下次重试即可，
                # 但连着失败说明整站不可用，继续只会把时间烧在必失败的请求上。
                consecutive_failures += 1

            if i % 20 == 0 or i == len(target):
                self.logger.info("  详情进度 %d/%d（已取到专业 %d 条）",
                                 i, len(target), self.stats["detail_major_found"])

            if (DETAIL_MAX_CONSECUTIVE_FAILURES
                    and consecutive_failures >= DETAIL_MAX_CONSECUTIVE_FAILURES):
                remaining = len(target) - i
                self.stats["detail_aborted"] = remaining
                self.logger.error(
                    "连续 %d 条详情请求失败，判定为目标站点不可用，"
                    "提前中止剩余 %d 条抓取。已抓到的数据会正常落库，"
                    "未抓的部分下次运行增量补上。",
                    consecutive_failures, remaining)
                break

            if i < len(target):
                time.sleep(self.detail_delay)

    @staticmethod
    def finalize(jobs: List[Job]) -> None:
        """
        抓取收尾：给没有截止时间的岗位用发布日期兜底。

        为什么要有这一步：列表页展示的是发布日期，详情页才有「过期时间」。
        详情没抓（增量跳过 / 关闭详情 / 抓失败）时，deadline 会是空的，
        导出到 Excel 就是一片空白。用发布日期兜底，至少信息不为空。
        """
        for job in jobs:
            if not job.deadline:
                job.deadline = job.publish_date

    def _fetch_job_page(self, jid: str) -> Optional[Job]:
        """抓取并解析单个职位详情页；失败返回 None"""
        url = self.JOB_URL.format(jid=jid)
        html = fetch(url, session=self.session, encoding=self.encoding)
        self.stats["detail_requests"] += 1
        if not html:
            return None

        try:
            parsed = self.parse(html, url)
        except Exception as e:
            self.logger.debug("解析职位 %s 失败：%s", jid, e)
            return None

        if parsed:
            self.stats["detail_ok"] += 1
            return parsed[0]
        return None

    @staticmethod
    def _jid_of(url: str) -> str:
        """从职位 URL 中取出职位 ID"""
        m = re.search(r"/job/view/id/(\d+)", url or "")
        return m.group(1) if m else ""

    # =================================================================
    # 详情页解析（沿用已验证的「锚点定位法」）
    # =================================================================
    def parse(self, html: str, url: str) -> List[Job]:
        """
        解析职位详情页，返回单元素列表（一个职位页 = 一个职位）。

        页面关键结构（文本层面）：
            首页 / 职位 / 详情
            {岗位名}
            {薪资}（独立成行）
            |                      ← 分隔符独占一行
            {地点}
            |
            {工作性质}
            |
            {学历}
            {发布日期}
            浏览次数：{N}
            职能类别：{...}
            招聘人数：{N}人
            需求专业：
            【本科】专业1,【本科】专业2,...
            职位详情 / 单位介绍 / 工作地址
            {详细地址}
            {公司名}
            单位性质：{...}
            单位行业：{...}
            单位规模：{...}
        """

        soup = BeautifulSoup(html, "lxml")

        # 导航栏目文本（用于剔除干扰）
        NAV_WORDS = {
            "主页", "学生服务", "就业信息", "宣讲会", "招聘活动", "招聘信息",
            "岗位信息", "实习信息", "公共部门", "事业单位", "地市组团",
            "企业招聘", "国际组织", "其他招聘", "就业服务", "就业手续",
            "户口档案", "办事大厅", "生源信息核对", "签约中心", "解约中心",
            "毕业去向登记", "就业推荐表", "去向登记确认", "核验授权",
            "档案查询", "职业辅导", "咨询预约", "自助服务", "就业指导",
            "就业调查", "单位服务", "了解学校", "学校简介", "院系介绍",
            "生源速览", "发布信息", "招聘公告", "线下宣讲申请",
            "空中宣讲申请", "双选会展位预订", "单位问卷调查", "招聘须知",
            "信息公开", "关于我们", "中心简介", "联系方式",
        }

        raw_lines = [l.strip() for l in soup.get_text("\n", strip=True).split("\n")]
        lines = [l for l in raw_lines if l and l not in NAV_WORDS]

        if not lines:
            return []

        # ---- 岗位名：优先页面 title ----
        title = ""
        if soup.title:
            t = soup.title.get_text(strip=True)
            title = re.sub(r"[-_|]\s*厦门大学.*$", "", t).strip()
        if not title:
            title = lines[0] if lines else ""

        # ---- 校验：确认这是职位页而非错误页 ----
        full_text = " ".join(lines)
        if "职位" not in full_text and "需求专业" not in full_text:
            return []
        majors = self._parse_majors(full_text)
        if not majors:
            # 没有专业字段的页面不是有效的职位详情页
            return []

        meta = self._parse_meta_block(lines)
        company_block = self._parse_company_block(soup, lines)

        job = Job(
            title=title,
            city=meta["city"],
            salary=meta["salary"],
            education=meta["education"],
            major_requirement=majors,
            company=self._parse_company(soup, lines),
            deadline=self._parse_deadline(soup) or meta["publish_date"],
            industry=company_block["industry"],
            company_nature=company_block["nature"],
            company_scale=company_block["scale"],
            apply_method=url,
            source="厦门大学就业信息网",
            url=url,
        )

        return [job] if job.is_valid() else []

    # -----------------------------------------------------------------
    # ★ 核心解析：锚点定位法
    # -----------------------------------------------------------------
    @classmethod
    def _parse_meta_block(cls, lines: List[str]) -> dict:
        """
        解析职位页的头部信息块。

        实测到的真实行序（关键！）：
            4  | '27届校招-游戏数值策划'   ← 岗位名
            5  | '7000-10000'              ← 薪资
            6  | '|'                       ← 分隔符（★独立成行）
            7  | '福建省厦门市思明区'       ← 城市
            8  | '|'
            9  | '全职'                    ← 工作性质
            10 | '|'
            11 | '本科'                    ← 学历
            15 | '2026-09-03'              ← 发布日期

        策略：
        1. 用正则找出「薪资行」的索引（纯数字区间，如 7000-10000）
        2. 从薪资行往后读，跳过 '|' 分隔符，按顺序取城市 / 性质 / 学历
        3. 用白名单校验，防止页面改版后错位

        这是旧版本踩坑后修正的写法：曾误以为元信息是「一行用 | 拼接」，
        结果 city 和 education 全部解析为空。**`|` 是独立的一行**。
        """
        result = {"salary": "", "city": "", "education": "",
                  "job_nature": "", "publish_date": ""}

        # ---- step 1: 找薪资行作锚点 ----
        anchor = -1
        for i, line in enumerate(lines[:40]):
            if re.fullmatch(r"\d{3,6}\s*[-~—]\s*\d{3,6}", line):
                result["salary"] = re.sub(r"\s*[-~—]\s*", "-", line)
                anchor = i
                break

        if anchor < 0:
            # 兜底：可能写的是「薪资：面议」
            for i, line in enumerate(lines[:40]):
                m = re.match(r"^(薪资|工资)[:：]\s*(.+)$", line)
                if m:
                    result["salary"] = m.group(2).strip()
                    anchor = i
                    break

        if anchor < 0:
            return result

        # ---- step 2: 从锚点往后收集「非 | 行」 ----
        tail = [l for l in lines[anchor + 1:anchor + 14] if l != "|"]

        NATURES = {"全职", "兼职", "实习", "临时", "不限"}
        EDU_RE = re.compile(r"^(不限|大专|专科|本科|硕士|博士|研究生)"
                            r"(及以上|以上)?$")

        for l in tail:
            if not result["city"] and re.search(r"(省|市|区|县|镇)", l) \
                    and len(l) <= 30 and not l.startswith(("职能", "单位")):
                result["city"] = l
                continue
            if not result["job_nature"] and l in NATURES:
                result["job_nature"] = l
                continue
            if not result["education"] and EDU_RE.match(l):
                result["education"] = l
                continue
            if not result["publish_date"] and re.fullmatch(r"\d{4}-\d{2}-\d{2}", l):
                result["publish_date"] = l

        # ---- step 3: 全页兜底（应对页面改版）----
        if not result["city"]:
            for l in lines[:40]:
                if re.search(r"(省|市|区|县)", l) and len(l) <= 30:
                    result["city"] = l
                    break
        if not result["education"]:
            for l in lines[:40]:
                m = re.match(r"^(学历要求|学历)[:：]\s*(.+)$", l)
                if m:
                    result["education"] = m.group(2).strip()
                    break

        return result

    @staticmethod
    def _parse_majors(text: str) -> str:
        """
        需求专业：全文本中「需求专业：」到下一个字段之间的内容。
        形如：
            需求专业：【本科】汉语言文学,【本科】历史学,…,【本科】信息与计算科学,…
        """
        m = re.search(r"需求专业[:：]\s*(.+?)(?:职位详情|单位介绍|工作地址|$)",
                      text, re.S)
        if not m:
            return ""

        majors = m.group(1).strip()
        majors = re.sub(r"【(本科|硕士|博士|专科)】", "", majors)
        majors = re.sub(r"[,，、]{2,}", "、", majors)
        majors = re.sub(r"\s+", " ", majors)
        return majors.strip("、, ")

    @staticmethod
    def _parse_company(soup: BeautifulSoup, lines: List[str]) -> str:
        """
        公司名：多路兜底。

        实测页面里公司名出现在「工作地址」+详细地址之后、
        「单位性质：」之前，如：
            36 | '厦门市思明区观音山宜兰路5号天瑞99商务中心25层'
            37 | '厦门点触科技股份有限公司'      ← 就是它
            38 | '单位性质：'
        """
        # 路 1：指向公司主页的链接（最可靠）
        node = soup.find("a", href=re.compile(r"/company/view/id/"))
        if node:
            name = node.get_text(strip=True)
            if name:
                return name

        # 路 2：「单位性质」前一行且不含数字地址特征
        for i, line in enumerate(lines):
            if line.startswith("单位性质"):
                for k in range(i - 1, max(i - 4, -1), -1):
                    cand = lines[k]
                    if re.search(r"(路|号|街|道|楼|层|号院)", cand):
                        continue
                    if 2 < len(cand) <= 40 and not cand.startswith("单位"):
                        return cand
                break
        return ""

    # -----------------------------------------------------------------
    # 单位属性解析（2026-09-23 新增）
    # -----------------------------------------------------------------
    @staticmethod
    def _parse_company_block(soup: BeautifulSoup, lines: List[str]) -> dict:
        """
        解析详情页底部的公司信息块：单位性质 / 单位行业 / 单位规模。

        :return: {"nature": 单位性质, "industry": 单位行业, "scale": 单位规模}

        实测到的真实结构（2026-09-23 对 jy.xmu.edu.cn 抓取确认）：
            <div class="info"><div style="padding-top: 15px;">
              <div class="item"><label class="label">单位性质：</label><span>国有企业</span></div>
              <div class="item"><label class="label">单位行业：</label><span>交通运输、仓储和邮政业</span></div>
              <div class="item"><label class="label">单位规模：</label><span>10000人以上</span></div>
            </div></div>

        文本层面则是「标签独占一行、值占下一行」：
            86 | '单位性质：'
            87 | '国有企业'
            88 | '单位行业：'
            89 | '交通运输、仓储和邮政业'
            90 | '单位规模：'
            91 | '10000人以上'

        两路解析的理由与 _parse_company 一致：结构化选择器最准，
        但站点改版换掉 class 时它会静默返回空。留一条按行扫描的兜底，
        改版后字段降级为「可能少几条」，而不是整列全空。

        注意：这里**不做归一化**。ODS 只记录页面怎么写，
        「国有企业 / 国企 / 中央企业」这类归一属于 DWD 的活。
        """
        result = {"nature": "", "industry": "", "scale": ""}
        labels = {"单位性质": "nature", "单位行业": "industry", "单位规模": "scale"}

        # ---- 路 1：结构化 <label> + 相邻 <span> ----
        for label in soup.find_all("label"):
            key = labels.get(label.get_text(strip=True).rstrip("：:").strip())
            if not key or result[key]:
                continue
            node = label.find_next_sibling("span")
            if node:
                result[key] = node.get_text(strip=True)

        # ---- 路 2：按行扫描（标签行 + 下一行是值）----
        for i, line in enumerate(lines):
            for prefix, key in labels.items():
                if result[key]:
                    continue
                if not line.startswith(prefix):
                    continue
                # 同一行的形式：'单位性质：国有企业'
                inline = line[len(prefix):].lstrip("：:").strip()
                if inline:
                    result[key] = inline
                    continue
                # 下一行的形式（站点实际就是这个）
                nxt = lines[i + 1] if i + 1 < len(lines) else ""
                if nxt and not nxt.endswith("："):
                    result[key] = nxt

        return result

    @staticmethod
    def _parse_deadline(soup: BeautifulSoup) -> str:
        """过期/截止时间"""
        text = soup.get_text(" ", strip=True)
        for pat in [r"过期时间[:：]\s*(\d{4}-\d{2}-\d{2})",
                    r"截止时间[:：]\s*(\d{4}-\d{2}-\d{2})",
                    r"截止日期[:：]\s*(\d{4}-\d{2}-\d{2})"]:
            m = re.search(pat, text)
            if m:
                return m.group(1)
        return ""


# ===================================================================
# 快速自测
#     python spiders/xmu_career.py           # 默认：厦门+本科+全职+3个专业
#     python spiders/xmu_career.py --full    # 不限定专业，看全量
# ===================================================================
if __name__ == "__main__":
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    full = "--full" in sys.argv
    profile = SearchProfile(
        city="厦门", education="本科", category="全职",
        majors=[] if full else list(DEFAULT_MAJOR_KEYS),
        max_pages=2,          # 自测只跑 2 页
    )

    spider = XmuCareerSpider(profile=profile, detail_limit=5)
    jobs = spider.run()

    out = []
    out.append("=" * 74)
    out.append(f"  检索条件：{' | '.join(profile.describe())}")
    out.append(f"  抓取结果：{len(jobs)} 个岗位")
    out.append(f"  请求统计：{spider.stats}")
    out.append("=" * 74)
    for i, j in enumerate(jobs, 1):
        out.append(f"\n【{i}】{j.title}")
        out.append(f"    公司：{j.company}")
        out.append(f"    地点：{j.city} | 薪资：{j.salary} | 学历：{j.education}")
        out.append(f"    专业：{j.major_requirement[:80] or '（未抓详情）'}")
        out.append(f"    链接：{j.url}")

    p = Path(__file__).resolve().parent.parent / "_spider_out.txt"
    p.write_text("\n".join(out), encoding="utf-8")
    print(f"结果已写入 {p}")
