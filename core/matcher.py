# -*- coding: utf-8 -*-
"""
专业匹配度打分引擎
------------------
项目核心模块。把「岗位是否适合信息与计算科学专业」这件事量化成可复现的分数。

职责边界：本模块只管**打分**（岗位对口不对口，客观判断，与用户偏好无关）。
「用户想看哪些岗位」的条件筛选在 core/filters.py。

===================================================================
v2 设计（2026-09-20 重构）
===================================================================

v1 是「一张词表加权求和」，实测暴露三个系统性偏差（都被真实数据抓到）：

  1. 「不限专业」得 0 分 —— 把「无门槛」编码成了「不匹配」。
     后果：中行信息科技岗、浩鲸云计算开发工程师等 8 个技术岗被直接扔掉。
  2. 分数与「专业罗列数量」正相关 —— 客户服务岗（列 48 个专业）53 分，
     排在数据分析管培生（35 分）前面。逻辑是反的：一家公司把专业写 40 个，
     恰恰说明它对专业**没有偏好**，对求职者是弱信号；而 v1 按「命中 9 个词」
     累加了 9 次，宽口径岗位反而吃香。
  3. 专业字段抓取失败（14 条，4.9%）被静默当成「不匹配」而不是「数据缺失」。

v2 的解法是把打分拆成两个**语义独立**的通道：

    总分 = 岗位方向分（标题） + 专业准入分（专业字段） - 反向词扣分

  · 岗位方向分回答「这个岗位是不是我要投的方向」→ 只看岗位名称。
    岗位名称是**强信号**，公司不会把客服岗写成数据岗。
  · 专业准入分回答「我的专业能不能过这道门槛」→ 只看专业要求。
    专业要求是**弱信号**，常写宽口径（不限 / 罗列几十个）。

为什么必须拆开：v1 把两者混在一张词表里累加，就必然出现
「客服岗因为专业列得多而压过算法岗」——因为专业罗列数成了分数的主要来源。
拆开后，「方向不对」的岗位无论专业写多少，方向分都是 0。

三个辅助机制：
  · 专指度衰减：专业字段命中的词越多，单个词贡献越低（几何衰减 + 系数修正）
  · 「不限专业」给基准分（= 可投，不是不匹配）
  · 字段缺失单独标记，与「真实不匹配」区分开

为什么仍然用规则引擎而不是机器学习（面试可讲）：
  · 结果可解释：能逐项说清「为什么这条 24 分、那条 8 分」
  · 零标注冷启动：只有 284 条数据，训练不出模型
  · 权重来自专业能力结构分析（数学类 > 数据类 > 计算机类），可随认知迭代
  · 与筛选解耦后可复用：同一批岗位能按不同条件反复筛，不必重爬重打分
"""
from typing import List, Tuple

from config import (
    KEYWORD_WEIGHTS, DIRECTION_WEIGHTS, NEGATIVE_KEYWORDS, MATCH_LEVELS,
    MAJOR_DECAY, MAJOR_CHANNEL_CAP, MAJOR_SPECIFICITY_FULL,
    UNLIMITED_MAJOR_SCORE, UNLIMITED_MAJOR_PATTERNS,
)
from core.models import Job


class JobMatcher:
    """岗位匹配度打分器（v2：方向通道 + 专业通道）"""

    def __init__(self, weights: dict = None, negative: dict = None,
                 direction: dict = None):
        self.weights = weights if weights is not None else KEYWORD_WEIGHTS
        self.negative = negative if negative is not None else NEGATIVE_KEYWORDS
        self.direction = direction if direction is not None else DIRECTION_WEIGHTS

    # ---------------------------------------------------------------
    # 通道一：岗位方向分（只看岗位名称，取最高命中权重）
    # ---------------------------------------------------------------
    def direction_score(self, title: str) -> Tuple[int, List[str]]:
        """
        岗位名称表达方向，通常只表达一个，所以取**最高权重**而不是累加。
        累加会重复计权：「数据分析/软件开发管培生」同时命中「数据分析」和「开发」。
        """
        if not title:
            return 0, []
        best_kw, best_w = None, 0
        for kw, w in self.direction.items():
            if kw in title and w > best_w:
                best_kw, best_w = kw, w
        if best_kw is None:
            return 0, []
        return best_w, [best_kw]

    # ---------------------------------------------------------------
    # 通道二：专业准入分（只看专业要求字段）
    # ---------------------------------------------------------------
    @staticmethod
    def _dedupe_substrings(hits: List[Tuple[str, int]]) -> List[Tuple[str, int]]:
        """
        子串去重：短词若是长词的子串，视为同一处文本，不重复计权。

        为什么需要：公告里写「计算机科学与技术」时，词表里的「计算机」
        也会命中，白送一次权重。实测「算法工程师」这一条就因此虚高。
        只在短词确实被长词覆盖时丢弃，独立出现的词（如同时写了
        「计算机类」和「计算机科学与技术」）仍各自计权。
        """
        ordered = sorted(hits, key=lambda x: -len(x[0]))
        kept: List[Tuple[str, int]] = []
        for kw, w in ordered:
            if any(kw != k and kw in k for k, _ in kept):
                continue
            kept.append((kw, w))
        return kept

    def major_score(self, major_text: str) -> Tuple[int, List[str], bool]:
        """
        计算专业准入分。

        :return: (分数, 命中词列表, 字段是否缺失)

        专指度衰减的必要性：
            列 1 个专业  = 精准定向（强信号）
            列 40 个专业 = 无偏好（弱信号）
        线性累加会让「无偏好」的岗位反而拿高分，所以按命中顺序做几何衰减，
        再乘专指度系数（命中数超过阈值后，系数 = 阈值 / 命中数）。
        """
        text = (major_text or "").strip()
        if not text:
            # 数据缺失 ≠ 不匹配。分开表达，让调用方能区别对待。
            return 0, [], True

        hits = self._dedupe_substrings(
            [(kw, w) for kw, w in self.weights.items() if kw in text]
        )

        # 「不限专业」= 无门槛、人人可投，给基准分。
        # 注意要先确认没有点名具体专业：「不限专业，数学类优先」这类写法
        # 应该走正常打分，而不是拿基准分。
        is_unlimited = (
            any(p in text for p in UNLIMITED_MAJOR_PATTERNS)
            or text == "不限"
        )
        if is_unlimited and not hits:
            return UNLIMITED_MAJOR_SCORE, ["不限专业"], False
        if is_unlimited and hits:
            # 宽口径 + 点名：按正常规则算，但不让它超过基准分太多
            val = self._weighted_decay(hits)
            return max(val, UNLIMITED_MAJOR_SCORE), [kw for kw, _ in hits], False

        if not hits:
            return 0, [], False

        val = self._weighted_decay(hits)
        return val, [kw for kw, _ in hits], False

    @staticmethod
    def _weighted_decay(hits: List[Tuple[str, int]]) -> int:
        """按权重降序做几何衰减 + 专指度系数，最后封顶"""
        ordered = sorted(hits, key=lambda x: -x[1])
        raw = 0.0
        for i, (_, w) in enumerate(ordered):
            raw += w * (MAJOR_DECAY ** i)

        n = len(ordered)
        if n > MAJOR_SPECIFICITY_FULL:
            raw *= MAJOR_SPECIFICITY_FULL / float(n)

        return int(round(min(raw, MAJOR_CHANNEL_CAP)))

    # ---------------------------------------------------------------
    # 打分核心
    # ---------------------------------------------------------------
    @staticmethod
    def extract_text(job: Job) -> str:
        """
        抽取参与反向词检测的文本（岗位名称 + 专业要求）。
        保留此方法以兼容既有调用；两个正向通道各自有独立的文本来源。
        """
        parts = [job.title or "", job.major_requirement or ""]
        return " ".join(p for p in parts if p)

    def score(self, job: Job) -> Tuple[int, List[str]]:
        """
        计算单个岗位的匹配分。

        :return: (总分, 命中说明列表)
        """
        d_score, d_hits = self.direction_score(job.title or "")
        m_score, m_hits, missing = self.major_score(job.major_requirement or "")

        total = d_score + m_score
        hits: List[str] = []
        if d_hits:
            hits.append("方向:" + "/".join(d_hits))
        hits.extend(m_hits)

        # 反向扣分：作用在岗位名称 + 专业要求合并文本上
        text = self.extract_text(job)
        if text.strip():
            for kw, penalty in self.negative.items():
                if kw in text:
                    total += penalty  # 负值
                    hits.append(f"[扣分]{kw}")

        # 标记数据缺失（而非静默当成不匹配）。
        # 只在「有岗位名称但专业字段为空」时标记，避免空对象也被打标。
        if missing and (job.title or "").strip():
            hits.append("【专业字段缺失】")

        return max(total, 0), hits

    def score_jobs(self, jobs: List[Job]) -> List[Job]:
        """
        批量打分并写回 Job 对象。

        :return: 按匹配分降序排列的岗位列表
        """
        for job in jobs:
            score, hits = self.score(job)
            job.match_score = score
            job.hit_keywords = hits
            job.match_level, job.match_label = self.level_of(score)

        return sorted(jobs, key=lambda j: j.match_score, reverse=True)

    @staticmethod
    def level_of(score: int) -> Tuple[str, str]:
        """分数 -> (等级, 中文说明)"""
        for threshold, level, label in MATCH_LEVELS:
            if score >= threshold:
                return level, label
        return "D", "不匹配"
