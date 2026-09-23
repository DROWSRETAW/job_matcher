# -*- coding: utf-8 -*-
"""
增量抓取判定
============

输入「本批次从列表页拿到的岗位」+「ODS 里已知的岗位状态」，
输出「这一个批次里，哪些岗位需要去抓详情页」。

────────────────────────────────────────────────────────────────
【为什么增量能省时间，以及省在哪】
────────────────────────────────────────────────────────────────
一次完整抓取有两段开销：

    第 1 段  列表页翻页      16 次请求   ≈ 25 秒   （每页 20 条）
    第 2 段  详情页补专业    284 次请求  ≈ 7.6 分钟（每条节流 1.6 秒）

总耗时约 8 分钟，其中 **94% 花在第 2 段**。

但第 2 段抓的是「需求专业 / 截止时间」，这些字段在两次抓取之间
绝大多数根本不会变。所以只要能把「没变的」识别出来跳过，
日常增量跑就能压到 1 分钟以内。

**列表页那 16 次请求不能省**，也不需要省：
    它是「有没有新岗位」的唯一来源，而且很便宜（25 秒）。
    省掉它就等于不知道市场上新出现了什么。
增量省的是「重复的详情页请求」，不是「发现新岗位的能力」。

────────────────────────────────────────────────────────────────
【判定规则（按优先级从高到低，命中即停）】
────────────────────────────────────────────────────────────────
    1. 增量关闭                → 全抓（回到老行为，用于排查问题）
    2. ODS 里没有这个 job_key  → NEW     新岗位，必须抓
    3. 列表字段指纹变了        → CHANGED 薪资/城市/学历/标题/发布日期动过，
                                        大概率详情也动过，重抓更稳
    4. 历史上从未抓到过详情    → NO_DETAIL 上次可能网络失败或站点改版，
                                        这次补上，否则专业字段永远是空的
    5. 详情数据超过 TTL        → STALE   列表没变但详情可能变了（站点可能
                                        只改了专业要求），按天数兜底刷新
    6. 其余                    → UNCHANGED 跳过详情，用历史详情字段回填

第 5 条是整套机制里唯一「不确定但必须做」的一步，说明白它的代价：
    它决定了增量跑的最低成本——只要 TTL 到期的岗位有 N 个，
    这一批就至少要发 N 次详情请求。TTL 设得越短越准，也越慢。
    默认 7 天（见 config.DETAIL_REFRESH_TTL_DAYS）。

────────────────────────────────────────────────────────────────
【回填：跳过详情不等于丢掉字段】
────────────────────────────────────────────────────────────────
被跳过的岗位，major_requirement / company_nature 会是空的。直接写库
会把这些字段抹掉，所以要从 ODS 里取该岗位**最近一次真正抓到详情的
快照**填回去。这是 ODS 只追加（append-only）带来的直接能力——
历史抓到的事实都还在，随时能取回来。

回填的字段清单见 BackfillFields。它有一个容易被忽略的硬约束：
**凡是登记进内容指纹的字段，都必须在跳过详情时也能被还原**，
否则同一条岗位在「抓了详情」和「跳过详情」两种情况下会算出不同指纹，
变更统计会静默地虚增。
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Set

from config import (
    INCREMENTAL_ENABLED,
    DETAIL_REFRESH_TTL_DAYS,
    DETAIL_REFRESH_ON_LIST_CHANGE,
)
from core.models import Job, list_fingerprint
from core.ods import JobState


# 判定原因（同时也是统计口径，别随意改名，测试与日志都依赖它）
REASON_NEW = "new"                  # 新岗位
REASON_CHANGED = "changed"          # 列表字段有变化
REASON_NO_DETAIL = "no_detail"      # 从未抓到过详情
REASON_STALE = "stale"              # 详情数据超过 TTL
REASON_UNCHANGED = "unchanged"      # 无需重抓
REASON_FORCED = "forced"            # 增量关闭，全量抓


@dataclass
class BackfillFields:
    """
    跳过详情页时，需要用 ODS 历史还原的字段集合（2026-09-23 新增）。

    【为什么这些字段必须**成组**还原，而不是各管各的】
        它们都登记在内容指纹里（见 core/models.py 的 CONTENT_FINGERPRINT_FIELDS）。
        增量跑跳过详情时，这些字段在内存对象里是空的，如果直接写库，
        同一条岗位就会算出与上一条快照**不同**的指纹——表象是
        change_count 每天虚增、--ods-changes 里堆满假变化，
        根因只是漏还原了一个字段，而且不会报任何错。
        所以「进内容指纹」和「能回填」是一对必须同时成立的约束。
        抽成结构体而不是二元组，就是为了以后再加字段时，
        回填循环能自动覆盖，不必再去改两处解包代码。

    【各字段的来源，以及它到底会不会缺】
        major_requirement / deadline   只有详情页有 → 跳过详情必缺
        company_nature                 只有详情页有 → 跳过详情必缺
        industry / company_scale       列表页就有，正常不会缺；留着是为了
                                       列表页偶发缺值时也有历史兜底
    """
    major_requirement: str = ""
    deadline: str = ""
    company_nature: str = ""
    industry: str = ""
    company_scale: str = ""

    def as_items(self):
        """(字段名, 值) 序列，供回填循环按名字赋值"""
        return (
            ("major_requirement", self.major_requirement),
            ("deadline", self.deadline),
            ("company_nature", self.company_nature),
            ("industry", self.industry),
            ("company_scale", self.company_scale),
        )


@dataclass
class DetailPlan:
    """
    详情页抓取计划。

    to_fetch  需要真正发请求的岗位
    skipped   跳过详情的岗位（字段从 backfill 回填）
    reasons   job_key -> 判定原因，用于日志与测试断言
    """
    to_fetch: List[Job] = field(default_factory=list)
    skipped: List[Job] = field(default_factory=list)
    backfill: Dict[str, BackfillFields] = field(default_factory=dict)
    reasons: Dict[str, str] = field(default_factory=dict)

    # 本批未在列表中出现的已知岗位（可能已下架，由调用方决定怎么处理）
    missing_keys: Set[str] = field(default_factory=set)
    # 本次判定时 ODS 中已知的全部岗位数（用于算覆盖率）
    known_count: int = 0

    @property
    def fetch_count(self) -> int:
        return len(self.to_fetch)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)

    def count_by_reason(self) -> Dict[str, int]:
        dist: Dict[str, int] = {}
        for reason in self.reasons.values():
            dist[reason] = dist.get(reason, 0) + 1
        return dist


def _age_days(stamp: str, now: datetime) -> Optional[float]:
    """把 'YYYY-MM-DD HH:MM:SS' 换算成「距今多少天」；无法解析返回 None"""
    if not stamp:
        return None
    try:
        dt = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return (now - dt).total_seconds() / 86400.0


def plan_detail_fetch(
    list_jobs: Iterable[Job],
    states: Dict[str, JobState],
    *,
    enabled: bool = None,
    ttl_days: int = None,
    refresh_on_change: bool = None,
    now: Optional[datetime] = None,
) -> DetailPlan:
    """
    算出本批次需要抓哪些详情页。

    :param list_jobs: 本批列表页解析出的岗位（此时 major_requirement 还是空的）
    :param states:    ODS 已知状态，来自 OdsRepository.latest_states()
    :param enabled:   是否启用增量；None 用 config 默认值
    :param ttl_days:  详情数据可信天数；None 用 config 默认值
    :param refresh_on_change: 列表变化是否触发重抓；None 用 config 默认值
    :param now:       当前时间（测试注入点）
    :return: DetailPlan
    """
    if enabled is None:
        enabled = INCREMENTAL_ENABLED
    if ttl_days is None:
        ttl_days = DETAIL_REFRESH_TTL_DAYS
    if refresh_on_change is None:
        refresh_on_change = DETAIL_REFRESH_ON_LIST_CHANGE
    now = now or datetime.now()

    plan = DetailPlan(known_count=len(states))
    seen_keys: Set[str] = set()

    for job in list_jobs:
        key = job.job_key
        if not key:
            # 没有稳定主键就无法判断历史，保守起见按新岗位处理
            job.detail_fetched = False
            plan.reasons[key] = REASON_FORCED
            plan.to_fetch.append(job)
            continue

        # 同一批次里出现重复主键时只保留第一条，避免重复发请求
        if key in seen_keys:
            continue
        seen_keys.add(key)

        state = states.get(key)

        # 先把该岗位的历史详情字段记进回填表（如果有）。
        # 它有两个用途：
        #   ① 被跳过的岗位直接回填（规则 6）
        #   ② 需要抓、但抓失败的岗位用它兜住旧值 —— 否则一次网络抖动
        #      就会把库里的「需求专业」抹成空字符串，而这只是抓取失败，
        #      不是「这个岗位没有专业要求」。信息缺失 ≠ 信息为空。
        if state is not None and state.has_detail:
            plan.backfill[key] = BackfillFields(
                major_requirement=state.major_requirement,
                deadline=state.deadline,
                company_nature=state.company_nature,
                industry=state.industry,
                company_scale=state.company_scale,
            )

        # ---- 规则 1：增量关闭 ----
        if not enabled:
            plan.reasons[key] = REASON_FORCED
            plan.to_fetch.append(job)
            continue

        # ---- 规则 2：新岗位 ----
        if state is None:
            plan.reasons[key] = REASON_NEW
            plan.to_fetch.append(job)
            continue

        # ---- 规则 3：列表字段有变化 ----
        if refresh_on_change and state.list_hash != list_fingerprint(job):
            plan.reasons[key] = REASON_CHANGED
            plan.to_fetch.append(job)
            continue

        # ---- 规则 4：历史上从未抓到过详情 ----
        if not state.has_detail:
            plan.reasons[key] = REASON_NO_DETAIL
            plan.to_fetch.append(job)
            continue

        # ---- 规则 5：详情数据过期 ----
        age = _age_days(state.detail_at, now)
        if age is None or age > ttl_days:
            plan.reasons[key] = REASON_STALE
            plan.to_fetch.append(job)
            continue

        # ---- 规则 6：无需重抓，回填历史详情字段 ----
        plan.reasons[key] = REASON_UNCHANGED
        plan.skipped.append(job)

    # 本批列表里没再出现的已知岗位
    plan.missing_keys = set(states) - seen_keys
    return plan


def apply_backfill(plan: DetailPlan) -> int:
    """
    把回填数据写进被跳过的岗位对象。

    单独拆成一个函数而不写在 plan_detail_fetch 里，是因为判定（纯计算）
    与赋值（改对象）是两件事：测试判定规则时不必构造完整 Job，
    而且回填只能在真正跳过详情时执行，顺序上更容易看清。

    【回填规则：只补空值，不覆盖本次已抓到的】
        industry / company_scale 这类列表页就有的字段，本次解析已经拿到
        更新的值了，不该被历史快照里的旧值盖回去。
        所以统一用「缺失才填」，而不是无条件赋值。

    :return: 实际回填了专业字段的岗位数
    """
    filled = 0
    for job in plan.skipped:
        recovery = plan.backfill.get(job.job_key)
        if recovery is not None:
            for name, value in recovery.as_items():
                if value and not getattr(job, name, ""):
                    setattr(job, name, value)
            if recovery.major_requirement:
                filled += 1
        job.detail_fetched = False
    return filled


def summarize_plan(plan: DetailPlan) -> List[str]:
    """
    把计划整理成可直接打印的几行说明。

    返回的每一行都不带缩进——调用方（日志 or 命令行）自己决定缩进多少，
    这里加缩进会导致两处缩进叠加。
    """
    dist = plan.count_by_reason()
    total = len(plan.reasons) or 1

    lines = [
        f"需抓详情 {plan.fetch_count} 条 / 免抓 {plan.skipped_count} 条"
        f"（共 {len(plan.reasons)} 条岗位，省下 {plan.skipped_count * 100 // total}% 详情请求）",
    ]

    parts = []
    for reason, label in (
        (REASON_NEW, "新岗位"),
        (REASON_CHANGED, "信息有变"),
        (REASON_NO_DETAIL, "缺专业字段"),
        (REASON_STALE, "超期刷新"),
        (REASON_UNCHANGED, "无需重抓"),
        (REASON_FORCED, "强制全量"),
    ):
        n = dist.get(reason, 0)
        if n:
            parts.append(f"{label}={n}")
    if parts:
        lines.append(" | ".join(parts))

    if plan.missing_keys:
        lines.append(
            f"另有 {len(plan.missing_keys)} 条历史岗位本批未再出现"
            "（需按检索口径判断是否已下架）"
        )
    return lines
