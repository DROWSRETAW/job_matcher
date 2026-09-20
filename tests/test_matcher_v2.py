# -*- coding: utf-8 -*-
"""
打分引擎 v2 的回归测试
----------------------
把 2026-09-20 从真实数据里抓到的三个系统性偏差固化成断言，
避免以后改权重时又把它们改回去。

三个偏差（v1 实测）：
  1. 「不限专业」得 0 分 —— 8 个技术岗被误杀
     （中行信息科技岗、浩鲸云计算开发工程师、睿云联 C++后台/Web前端/运维/测试…）
  2. 分数与「专业罗列数量」正相关 —— 客户服务岗（列 48 个专业）53 分
     排在数据分析管培生（35 分）前面
  3. 专业字段抓取失败被静默当成「不匹配」
"""
import pytest

from core.matcher import JobMatcher
from core.models import Job


@pytest.fixture
def matcher():
    return JobMatcher()


# ===================================================================
# 偏差 1：「不限专业」= 无门槛，不是不匹配
# ===================================================================
class TestUnlimitedMajor:

    def test_unlimited_major_gets_base_score(self, matcher):
        """「不限专业」必须拿到基准分，而不是 0"""
        job = Job(company="某公司", title="某岗位", major_requirement="不限专业")
        score, hits = matcher.score(job)
        assert score > 0, "「不限专业」被打了 0 分 —— 把无门槛编码成了不匹配"
        assert "不限专业" in hits

    def test_unlimited_major_returns_base_exactly(self, matcher):
        """无方向命中的「不限专业」岗位 = 基准分"""
        from config import UNLIMITED_MAJOR_SCORE
        job = Job(company="某公司", title="管理培训生", major_requirement="不限专业")
        score, _ = matcher.score(job)
        assert score == UNLIMITED_MAJOR_SCORE

    def test_unlimited_major_alternate_spelling(self, matcher):
        """「专业不限」和「不限专业」是同义写法，都要认"""
        from config import UNLIMITED_MAJOR_SCORE
        job = Job(company="某公司", title="管理培训生", major_requirement="专业不限")
        score, _ = matcher.score(job)
        assert score == UNLIMITED_MAJOR_SCORE

    def test_killed_tech_jobs_now_survive(self, matcher):
        """
        回归：v1 里被打 0 分误杀的技术岗，现在必须能进 B 级（默认筛选线 8 分）。
        数据取自 2026-09-20 实测库（284 条）。
        """
        cases = [
            ("浩鲸云计算科技", "开发工程师-厦门-2027届校招"),
            ("中国银行厦门市分行", "信息科技岗"),
            ("厦门睿云联创新科技", "C++后台开发工程师"),
            ("厦门睿云联创新科技", "Web前端开发工程师"),
            ("厦门睿云联创新科技", "运维工程师（福州）"),
            ("网宿科技厦门分公司", "【27届校招】产品运维工程师-厦门-03124"),
        ]
        for company, title in cases:
            job = Job(company=company, title=title, major_requirement="不限专业")
            score, _ = matcher.score(job)
            assert score >= 8, (
                f"「{title}」应达 B 级（≥8 分），实际 {score} 分 —— "
                "技术岗因「不限专业」被误杀的问题回归了"
            )

    def test_unlimited_with_named_major_uses_normal_rules(self, matcher):
        """「不限专业，数学类优先」这类混合写法应走正常打分，且不低于基准分"""
        from config import UNLIMITED_MAJOR_SCORE
        job = Job(company="某公司", title="某岗位",
                  major_requirement="不限专业，数学类优先")
        score, hits = matcher.score(job)
        assert score >= UNLIMITED_MAJOR_SCORE
        assert "数学类" in hits

    def test_sales_with_unlimited_major_still_penalized(self, matcher):
        """反向词扣分不能被基准分掩盖：销售岗仍应是 0 分"""
        job = Job(company="某公司", title="销售代表", major_requirement="不限专业")
        score, hits = matcher.score(job)
        assert score == 0
        assert any("扣分" in h for h in hits)


# ===================================================================
# 偏差 2：分数不该与「专业罗列数量」正相关
# ===================================================================
class TestSpecificityDecay:

    def test_wide_listing_scores_lower_than_targeted(self, matcher):
        """
        回归：宽口径罗列（方向不对）必须低于精准定向。
        v1 里「客户服务岗（48 个专业）」53 分 > 「数据分析管培生」35 分。
        """
        wide = Job(
            company="中远海运", title="客户服务岗",
            major_requirement=("信息与计算科学,数据科学,大数据,人工智能,"
                               "计算机类,计算机,计算机科学与技术,软件工程,电子信息"),
        )
        narrow = Job(
            company="活石网络", title="数据分析师",
            major_requirement="信息与计算科学,数学类",
        )
        wide_score, _ = matcher.score(wide)
        narrow_score, _ = matcher.score(narrow)

        assert narrow_score > wide_score, (
            f"精准定向（{narrow_score}）应高于宽口径罗列（{wide_score}）—— "
            "分数与专业罗列数正相关的问题回归了"
        )
        assert matcher.level_of(wide_score)[0] in ("B", "C"), (
            f"方向不对的宽口径岗位不该进 A 级，实际 {wide_score} 分"
        )

    def test_more_hits_does_not_mean_more_score(self, matcher):
        """单调性：在同一方向下，加更多低权重专业词，分数不应无限增长"""
        from config import MAJOR_CHANNEL_CAP
        many = Job(
            company="某公司", title="某岗位",
            major_requirement=(
                "信息与计算科学,数学类,应用数学,数理统计,统计学类,数据科学,"
                "数据分析,机器学习,算法工程,人工智能,计算机类,计算机科学与技术,"
                "软件工程,网络安全,电子信息,理工类专业,金融科技,风控,量化"
            ),
        )
        score, _ = matcher.score(many)
        assert score <= MAJOR_CHANNEL_CAP, (
            f"专业通道应封顶 {MAJOR_CHANNEL_CAP} 分，实际 {score} 分"
        )

    def test_direction_channel_breaks_the_tie(self, matcher):
        """
        真正的判别力来自「岗位方向」：专业字段完全相同，标题方向不同，
        分数必须拉开差距。
        """
        major = "信息与计算科学,数据科学,大数据,计算机类,软件工程"
        data_job = Job(company="A", title="数据分析工程师", major_requirement=major)
        other_job = Job(company="B", title="行政专员", major_requirement=major)

        assert matcher.score(data_job)[0] > matcher.score(other_job)[0]
        assert matcher.direction_score("行政专员")[0] == 0


# ===================================================================
# 偏差 3：字段缺失 ≠ 不匹配
# ===================================================================
class TestMissingField:

    def test_missing_major_field_is_flagged(self, matcher):
        job = Job(company="某公司", title="某岗位", major_requirement="")
        score, hits = matcher.score(job)
        assert "【专业字段缺失】" in hits, (
            "专业字段为空时必须显式标记为「数据缺失」，"
            "不能静默当成「不匹配」——这是第 5 号调试记录同类型的错误"
        )

    def test_missing_field_not_flagged_for_empty_job(self, matcher):
        """空对象不该被标记（它不是一个抓取失败的岗位）"""
        score, hits = matcher.score(Job())
        assert hits == []
        assert score == 0

    def test_missing_and_genuinely_unmatched_are_distinguishable(self, matcher):
        """数据缺失 vs 真实不匹配，两条路径的可解释输出必须不同"""
        missing = Job(company="A", title="某岗位", major_requirement="")
        unmatched = Job(company="B", title="某岗位", major_requirement="汉语言文学")

        _, missing_hits = matcher.score(missing)
        _, unmatched_hits = matcher.score(unmatched)

        assert "【专业字段缺失】" in missing_hits
        assert "【专业字段缺失】" not in unmatched_hits


# ===================================================================
# 通道结构
# ===================================================================
class TestChannels:

    def test_direction_takes_max_not_sum(self, matcher):
        """
        方向通道取最高权重，不累加。
        「数据分析/软件开发管培生」同时命中「数据分析」「开发」「软件」，
        累加会重复计权。
        """
        score, hits = matcher.direction_score("数据分析/软件开发管培生")
        from config import DIRECTION_WEIGHTS
        assert score == DIRECTION_WEIGHTS["数据分析"]
        assert hits == ["数据分析"]

    def test_direction_score_zero_for_irrelevant_title(self, matcher):
        assert matcher.direction_score("客户服务岗")[0] == 0
        assert matcher.direction_score("供应链管理岗")[0] == 0
        assert matcher.direction_score("")[0] == 0
        assert matcher.direction_score(None)[0] == 0

    def test_substring_hits_are_deduped(self, matcher):
        """
        「计算机科学与技术」命中时，词表里的「计算机」不该重复计权。
        """
        _, hits, _ = matcher.major_score("计算机科学与技术")
        assert hits == ["计算机科学与技术"], (
            f"短词被长词覆盖时应去重，实际命中 {hits}"
        )

    def test_independent_mentions_both_count(self, matcher):
        """独立出现的两个词仍各自计权（不能过度去重）"""
        _, hits, _ = matcher.major_score("计算机类、电子信息")
        assert "计算机类" in hits
        assert "电子信息" in hits

    def test_score_parts_are_additive(self, matcher):
        """总分 = 方向分 + 专业分（无扣分时）"""
        job = Job(company="A", title="算法工程师",
                  major_requirement="信息与计算科学")
        d, _ = matcher.direction_score(job.title)
        m, _, _ = matcher.major_score(job.major_requirement)
        total, _ = matcher.score(job)
        assert total == d + m

    def test_precise_targeted_job_reaches_s_level(self, matcher):
        """精准点名的数据/算法岗必须能到 S 级（保证排序顶端有意义）"""
        job = Job(company="A", title="数据开发工程师",
                  major_requirement="信息与计算科学、数学类")
        score, hits = matcher.score(job)
        assert matcher.level_of(score)[0] == "S", f"实际 {score} 分"
        assert "信息与计算科学" in hits
