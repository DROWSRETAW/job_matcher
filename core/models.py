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

    # ---- 单位（雇主）属性（2026-09-23 新增）----
    # 这三项站点两个页面都给，但**主源不同**，见 spiders/xmu_career.py：
    #   industry / company_scale  列表页就有（.company 下的嵌套 <ul>），
    #                             任何一次抓取都能拿到，不依赖详情页
    #   company_nature            只有详情页有（「单位性质：国有企业」），
    #                             增量跳过详情时必须靠回填补回来
    # 【为什么要采它们】规划里的分析目标包含「城市 × 行业供需对比」，
    # 而行业这个维度在数据模型里原本是**缺失**的——页面有、解析没取，
    # 等于整个维度不可能算。补上它才是纯采集侧的改动，成本最低。
    industry: str = ""           # 单位行业，如「交通运输、仓储和邮政业」
    company_nature: str = ""     # 单位性质，如「国有企业」
    company_scale: str = ""      # 单位规模，如「10000人以上」

    # ---- 详情页的结构化字段（2026-09-24 新增）----
    # 【为什么是这几个，而不是"详情正文"】
    #   规划里 DWD 技能抽取的第三个来源写的是「详情正文」。实测该站点上
    #   「职位详情」是一个**空标签页**（2026-09-24 抓 6 个详情页确认，
    #   页面只有标签名、没有任何正文）。同一区域真正有值的是下面这几个
    #   结构化字段——它们比正文更适合做分析，因为是站点用下拉框维护的：
    #
    #   experience    工作经验：站点原样值（"应届毕业生" / "不限" / "1-3年"）
    #                 ★ 缺口分析点名「经验字段根本没采集」，这一项直接补上
    #   job_category  职能类别：站点自带的标准分类（"内容运营" / "业务拓展"）
    #                 它替代了不可用的"详情正文"，成为技能抽取的第三个来源
    #   language_req  语言要求（"不限" / "英语"）
    #   headcount     招聘人数：站点原样文本（"3人" / "99人"）
    #   city_detail   详情页的完整工作地点（"福建省厦门市湖里区"）
    #                 ★ 列表页只给到市（"福建省厦门市"），**95% 的岗位因此
    #                 拿不到区县**，DWD 的城市三级拆分沦为空谈。详情页才
    #                 有区级，所以单独存一列，不覆盖 city。
    #
    #   ⚠️ 为什么不直接覆盖 city：city 参与 LIST_FINGERPRINT_FIELDS。
    #   若用详情页的值覆盖它，同一条岗位在「列表阶段」与「写库阶段」会算出
    #   不同的列表指纹，下一轮增量必然误判成"列表有变"而全量重抓。
    #   加一列而不是改一列，是这一约束下的唯一选择。
    experience: str = ""         # 工作经验（站点原样值）
    job_category: str = ""       # 职能类别（站点原样值）
    language_req: str = ""       # 语言要求（站点原样值）
    headcount: str = ""          # 招聘人数（站点原样值）
    city_detail: str = ""        # 详情页的完整工作地点（含区县）

    # 本条记录是由哪一版详情解析器产生的（0 = 没有抓到过详情）。
    # 见 config.DETAIL_SCHEMA_VERSION：解析器学会新字段时 +1，
    # 增量据此把老快照排进重抓队列。它是血缘标记，**不进指纹**——
    # 它描述的是"谁解析的"，不是"内容是什么"。
    detail_schema_version: int = 0

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
#
# 【单位属性为什么只进内容指纹、不进列表指纹】（2026-09-23 新增）
#   industry / company_scale 是从列表页解析的，看上去该进列表指纹。
#   但列表指纹的用途是「决定要不要重抓详情页」——而这两个字段本身
#   就来自列表页，新值本次已经拿到了，再为它触发一次详情请求纯属浪费。
#   所以只在内容指纹里登记：行业/规模真的变了，能被变更检测发现
#   （change_count +1、--ods-changes 看得到），但不会引发多余的网络请求。
#
#   ⚠️ 由此产生一条**必须遵守的约束**：
#   进了内容指纹的字段，必须在「跳过详情」的链路上也能被还原。
#   否则同一条岗位，抓了详情时算出指纹 A、跳过详情时算出指纹 B，
#   每次增量跑都会被误判成「内容有变化」，change_count 一路虚增。
#   这就是 company_nature 必须纳入回填表的原因——它只有详情页有。
#   （回填机制见 core/incremental.py 的 BackfillFields / apply_backfill）
#
#   2026-09-24 新增的 experience / job_category / language_req / headcount /
#   city_detail 五项**全部只有详情页有**，所以全部登记进了 BackfillFields。
#   加它们时漏登记任何一项，都会立刻表现为 change_count 每天虚增。

LIST_FINGERPRINT_FIELDS = (
    "company", "title", "city", "salary", "education", "publish_date",
)
CONTENT_FINGERPRINT_FIELDS = LIST_FINGERPRINT_FIELDS + (
    "major_requirement", "deadline",
    "industry", "company_nature", "company_scale",
    # 以下五项只有详情页有（2026-09-24 新增）。
    # city_detail 单列而不合并进 city，理由见 Job 定义处的注释。
    "experience", "job_category", "language_req", "headcount", "city_detail",
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
