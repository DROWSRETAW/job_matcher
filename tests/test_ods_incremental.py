# -*- coding: utf-8 -*-
"""
ODS 贴源层与增量抓取的单元测试
==============================

覆盖 2026-09-21「增量运行 + 写入 ODS」改造新增的四个部分：

    1. 内容指纹（core/models.py）—— 增量判定的地基
    2. ODS 贴源层（core/ods.py）—— 只追加、可回填、有台账
    3. 增量判定（core/incremental.py）—— 六条规则的全部分支
    4. 最新状态层（core/storage.py）—— 自动迁移、按主键 upsert、生命周期

全部为离线测试，不发起任何网络请求。
"""
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.models import (  # noqa: E402
    Job, make_job_key, list_fingerprint, content_fingerprint,
)
from core.ods import OdsRepository, new_batch_id  # noqa: E402
from core.incremental import (  # noqa: E402
    plan_detail_fetch, apply_backfill, summarize_plan,
    REASON_NEW, REASON_CHANGED, REASON_NO_DETAIL, REASON_STALE,
    REASON_UNCHANGED, REASON_FORCED,
)
from core.storage import JobStorage  # noqa: E402


# ===================================================================
# 测试夹具
# ===================================================================
def make_job(jid: str, company: str = "", title: str = "", *,
             city: str = "福建省厦门市", salary: str = "8000-12000",
             education: str = "本科", publish_date: str = "2026-09-20",
             major: str = "数学类、信息与计算科学",
             deadline: str = "2026-10-01", detail: bool = True,
             industry: str = "制造业", company_scale: str = "10000人以上",
             company_nature: str = "国有企业") -> Job:
    """
    造一个岗位，模拟爬虫的产出形状。

    :param detail: True 表示已抓过详情页（含专业/截止时间/单位性质），
                   False 表示这是列表页阶段的产物。

    注意 industry / company_scale 无论 detail 与否都给值——
    这两个字段来自列表页，与「抓没抓详情」无关。
    company_nature 则只有详情页才有，detail=False 时必须为空，
    这正是「跳过详情会丢字段」的现实来源。
    """
    job = Job(
        company=company or f"公司{jid}",
        title=title or f"岗位{jid}",
        city=city, salary=salary, education=education,
        publish_date=publish_date,
        major_requirement=major if detail else "",
        deadline=deadline if detail else "",
        industry=industry,
        company_scale=company_scale,
        company_nature=company_nature if detail else "",
        source="厦门大学就业信息网",
        url=f"https://jy.xmu.edu.cn/job/view/id/{jid}",
        source_job_id=str(jid),
        job_key=make_job_key(str(jid)),
        detail_fetched=detail,
    )
    # 与爬虫 parse_list 保持一致：列表指纹在列表阶段就固定下来
    job.list_hash = list_fingerprint(job)
    return job


@pytest.fixture
def ods(tmp_path):
    return OdsRepository(db_path=tmp_path / "test.db")


@pytest.fixture
def storage(tmp_path):
    return JobStorage(db_path=tmp_path / "test.db")


# ===================================================================
# 1. 内容指纹
# ===================================================================
class TestFingerprint:
    """
    指纹是增量判定与变更检测的唯一依据，它错了整套机制就静默失效。
    """

    def test_same_content_same_fingerprint(self):
        assert content_fingerprint(make_job("1")) == content_fingerprint(make_job("1"))

    def test_list_fingerprint_ignores_detail_fields(self):
        """
        ★ 关键：列表指纹必须对详情页字段免疫。

        因为增量跑只请求列表页，此时 major/deadline 都还是空的。
        若列表指纹把这些字段算进去，每次抓取都会得出「指纹变了」，
        于是所有岗位被判定为「信息有变」，增量完全失效——
        退化回全量重抓，而且不会报任何错。
        """
        a = make_job("1", detail=False)     # 列表阶段：无专业
        b = make_job("1", detail=True)      # 详情阶段：有专业
        assert list_fingerprint(a) == list_fingerprint(b)

    def test_content_fingerprint_detects_detail_change(self):
        """内容指纹必须能感知详情字段的变化"""
        a = make_job("1", major="数学类")
        b = make_job("1", major="计算机类")
        assert content_fingerprint(a) != content_fingerprint(b)

    def test_list_fingerprint_detects_list_change(self):
        """列表字段（这里是薪资）变了，列表指纹必须跟着变"""
        assert (list_fingerprint(make_job("1", salary="8000-12000"))
                != list_fingerprint(make_job("1", salary="12000-20000")))

    def test_separator_has_no_ambiguity(self):
        """
        字段之间用 \\x1f 分隔，而不是逗号或竖线。

        若用竖线拼串，("A|B", "C") 与 ("A", "B|C") 会得到同一个指纹。
        """
        a = make_job("1", company="A|B", title="C")
        b = make_job("1", company="A", title="B|C")
        assert list_fingerprint(a) != list_fingerprint(b)

    def test_empty_job_is_stable(self):
        assert list_fingerprint(Job()) == list_fingerprint(Job())

    # ---- 单位属性：只进内容指纹，不进列表指纹（2026-09-23 新增）----
    def test_content_fingerprint_covers_company_attributes(self):
        """
        行业 / 单位性质 / 单位规模 变了，内容指纹必须能感知。

        这三个字段是 2026-09-23 补采的。若漏登记进内容指纹，
        岗位改了行业就永远检测不到——不会报错，只会静默漏掉变更。
        """
        base = make_job("1")
        for field, kwargs in (
            ("industry", {"industry": "信息传输、软件和信息技术服务业"}),
            ("company_nature", {"company_nature": "其他企业"}),
            ("company_scale", {"company_scale": "500-1000人"}),
        ):
            changed = make_job("1", **kwargs)
            assert content_fingerprint(base) != content_fingerprint(changed), \
                f"{field} 变了，内容指纹却没变"

    def test_list_fingerprint_ignores_company_attributes(self):
        """
        ★ 单位属性**不该**进列表指纹。

        列表指纹决定「要不要重抓详情页」。industry / company_scale
        本来就来自列表页，本次已经拿到新值了，再为它触发一次详情请求
        纯属浪费（约 1.6 秒/条）。所以这两个字段变化必须对列表指纹免疫。
        """
        base = make_job("1", detail=False)
        changed = make_job("1", detail=False,
                           industry="信息传输、软件和信息技术服务业",
                           company_scale="500-1000人")
        assert list_fingerprint(base) == list_fingerprint(changed)


class TestJobKey:
    def test_format(self):
        assert make_job_key("2401083") == "xmu:2401083"

    def test_empty_id_gives_empty_key(self):
        assert make_job_key("") == ""
        assert make_job_key(None) == ""


# ===================================================================
# 2. ODS 贴源层
# ===================================================================
class TestOdsSnapshots:

    def test_saves_and_counts(self, ods):
        n = ods.save_snapshots("b1", [make_job("1"), make_job("2")])
        assert n == 2
        assert ods.count_snapshots() == 2
        assert ods.count_jobs() == 2

    def test_same_batch_same_job_is_idempotent(self, ods):
        """同一批次重跑，不应产生重复快照"""
        ods.save_snapshots("b1", [make_job("1")])
        ods.save_snapshots("b1", [make_job("1")])
        assert ods.count_snapshots() == 1

    def test_snapshots_are_append_only(self, ods):
        """★ 核心约定：同一岗位在不同批次各留一条快照，历史不被覆盖"""
        ods.save_snapshots("b1", [make_job("1", salary="8000-12000")])
        ods.save_snapshots("b2", [make_job("1", salary="12000-20000")])

        assert ods.count_snapshots() == 2, "两次抓取应留下两条快照"
        history = ods.history_of("xmu:1")
        assert [h["salary"] for h in history] == ["8000-12000", "12000-20000"], \
            "历史快照必须保留原值，不能被覆盖"

    def test_job_without_key_is_skipped(self, ods):
        """拿不到稳定主键的记录不进 ODS（无法追溯历史，只是噪音）"""
        orphan = Job(company="A", title="B")     # 没有 job_key
        assert ods.save_snapshots("b1", [orphan]) == 0
        assert ods.count_snapshots() == 0

    def test_snapshot_stores_company_attributes(self, ods):
        """
        单位行业 / 单位性质 / 单位规模必须原样落进快照（2026-09-23 新增）。

        只加字段名不加写库语句，是最容易漏的一步：模型上加了三列、
        表结构也加了三列，但 INSERT 没带上，结果是「字段全空且不报错」。
        所以这里直接断言取回来的值。
        """
        ods.save_snapshots("b1", [make_job("1")])

        row = ods.history_of("xmu:1")[0]
        assert row["industry"] == "制造业"
        assert row["company_nature"] == "国有企业"
        assert row["company_scale"] == "10000人以上"

        # 状态层也要能带出来（回填靠它）
        state = ods.latest_states()["xmu:1"]
        assert state.company_nature == "国有企业"
        assert state.industry == "制造业"
        assert state.company_scale == "10000人以上"

    def test_latest_states_reports_latest(self, ods):
        ods.save_snapshots("b1", [make_job("1", salary="8000-12000")])
        ods.save_snapshots("b2", [make_job("1", salary="12000-20000")])

        state = ods.latest_states()["xmu:1"]
        assert state.last_batch_id == "b2"
        assert state.change_count == 1, "内容变过一次"

    def test_detail_backfill_survives_skipped_snapshot(self, ods):
        """
        ★ 这是 ODS 只追加带来的关键能力。

        场景：第一次抓到了详情；第二次增量跳过详情（快照里专业为空）。
        此时 latest_states() 仍必须能给出第一次那份专业字段——
        否则增量跑会把库里的「需求专业」越抹越空。
        """
        ods.save_snapshots("b1", [make_job("1", major="数学类", detail=True)])
        # 第二批：跳过详情，专业字段为空写进快照
        skipped = make_job("1", detail=False)
        ods.save_snapshots("b2", [skipped])

        state = ods.latest_states()["xmu:1"]
        assert state.has_detail is True, "必须记得历史上有抓到过详情"
        assert state.major_requirement == "数学类", "回填来源必须是那条含详情的快照"
        assert state.last_batch_id == "b2", "但「最近快照」仍是第二批"

    def test_recent_changes_only_reports_real_changes(self, ods):
        ods.save_snapshots("b1", [make_job("1", salary="8000-12000")])
        ods.save_snapshots("b2", [make_job("1", salary="8000-12000")])   # 没变
        ods.save_snapshots("b3", [make_job("1", salary="12000-20000")])  # 变了

        changes = ods.recent_changes(days=7)
        assert len(changes) == 1
        assert changes[0]["salary"] == "12000-20000"

    def test_stats(self, ods):
        ods.save_snapshots("b1", [make_job("1"), make_job("2")])
        ods.open_batch("b1", "incremental")
        stats = ods.stats()
        assert stats["snapshots"] == 2
        assert stats["jobs"] == 2
        assert stats["batches"] == 1


class TestBatchLedger:

    def test_open_and_close(self, ods):
        ods.open_batch("b1", "incremental", '{"city": "厦门"}')
        batch = ods.get_batch("b1")
        assert batch["status"] == "running"
        assert batch["mode"] == "incremental"

        ods.close_batch("b1", status="ok", list_items=10, new_jobs=3,
                        detail_requests=4, detail_skipped=6)
        batch = ods.get_batch("b1")
        assert batch["status"] == "ok"
        assert batch["list_items"] == 10
        assert batch["new_jobs"] == 3
        assert batch["detail_skipped"] == 6
        assert batch["finished_at"], "结束时间必须写上"

    def test_unknown_counter_is_ignored(self, ods):
        """多传的计数不应导致 SQL 报错"""
        ods.open_batch("b1", "full")
        ods.close_batch("b1", status="ok", 不存在的字段=1, list_items=2)
        assert ods.get_batch("b1")["list_items"] == 2

    def test_batch_id_is_second_precision(self):
        when = datetime(2026, 9, 21, 10, 52, 30)
        assert new_batch_id(when) == "20260921_105230"

    # ---- 孤儿批次（2026-09-24 新增）----
    #
    # 背景：进程被强杀时 close_batch 根本没机会执行，批次会永远挂在
    # running。台账里出现「永远在跑」的批次是假账——监控会误判，
    # 人工排查时也分不清「真的在跑」还是「死了没埋」。

    @staticmethod
    def _rewind(ods, batch_id, hours):
        """把批次的开始时间往前拨，模拟一个很久以前开、但没收尾的批次"""
        from core.dbutil import connect
        stamp = (datetime.now() - timedelta(hours=hours)).strftime(
            "%Y-%m-%d %H:%M:%S")
        with connect(ods.db_path) as conn:
            conn.execute(
                "UPDATE ods_crawl_batch SET started_at = ? WHERE batch_id = ?",
                (stamp, batch_id))

    def test_fresh_running_batch_is_not_orphan(self, ods):
        """★ 刚开始跑的批次不能被当成孤儿——否则会误伤正在执行的任务"""
        ods.open_batch("b_now", "incremental")
        assert ods.orphan_batches(stale_hours=6) == []

    def test_stale_running_batch_is_orphan(self, ods):
        ods.open_batch("b_old", "incremental")
        self._rewind(ods, "b_old", hours=30)

        orphans = ods.orphan_batches(stale_hours=6)
        assert [b["batch_id"] for b in orphans] == ["b_old"]

    def test_closed_batch_is_not_orphan(self, ods):
        """已正常收尾的批次，无论放多久都不该被再动一次"""
        ods.open_batch("b_done", "incremental")
        ods.close_batch("b_done", status="ok", list_items=5)
        self._rewind(ods, "b_done", hours=30)

        assert ods.orphan_batches(stale_hours=6) == []

    def test_close_orphan_batches_marks_failed(self, ods):
        ods.open_batch("b1", "incremental")
        ods.open_batch("b2", "incremental")
        self._rewind(ods, "b1", hours=30)
        self._rewind(ods, "b2", hours=30)
        ods.open_batch("b_fresh", "incremental")      # 这条不该被动

        closed = ods.close_orphan_batches(stale_hours=6)

        assert sorted(closed) == ["b1", "b2"]
        for bid in ("b1", "b2"):
            b = ods.get_batch(bid)
            assert b["status"] == "failed"
            assert b["finished_at"], "收尾必须写上结束时间"
            assert b["note"], "要说明为什么被标记为 failed"
        assert ods.get_batch("b_fresh")["status"] == "running"

    def test_no_orphan_returns_empty(self, ods):
        assert ods.close_orphan_batches(stale_hours=6) == []


# ===================================================================
# 3. 增量判定
# ===================================================================
class TestIncrementalPlan:
    """
    六条规则逐条锁定。这些规则决定了「会不会重复抓详情」，
    判错方向的代价是实打实的：多判一分就多花几十秒，少判一分就漏更新。
    """

    NOW = datetime(2026, 9, 21, 12, 0, 0)

    def _states(self, ods, jobs):
        """把一批岗位写进 ODS，再取回已知状态"""
        ods.save_snapshots("b1", jobs, crawled_at="2026-09-21 11:00:00")
        return ods.latest_states()

    # ---- 规则 2：新岗位 ----
    def test_new_job_needs_detail(self, ods):
        jobs = [make_job("1", detail=False)]
        plan = plan_detail_fetch(jobs, {}, now=self.NOW)
        assert plan.fetch_count == 1
        assert plan.reasons["xmu:1"] == REASON_NEW

    # ---- 规则 6：无需重抓 ----
    def test_unchanged_job_is_skipped_and_backfilled(self, ods):
        states = self._states(ods, [make_job("1", major="数学类")])
        fresh = make_job("1", detail=False)          # 列表阶段，字段一样
        plan = plan_detail_fetch([fresh], states, now=self.NOW)

        assert plan.fetch_count == 0, "无变化就不该再发详情请求"
        assert plan.skipped_count == 1
        assert plan.reasons["xmu:1"] == REASON_UNCHANGED

        apply_backfill(plan)
        assert fresh.major_requirement == "数学类", "必须回填，否则字段被抹空"
        assert fresh.detail_fetched is False

    def test_skipped_job_restores_company_fields(self, ods):
        """
        ★★ 本次改造最关键的一条回归：进了内容指纹的字段，必须能回填。

        场景：第一次抓到详情，第二次增量跳过详情。
        单位性质只有详情页有，跳过时它在内存里是空的。若不做还原，
        同一条岗位会算出两个不同的内容指纹——表征是 change_count
        每次跑都虚增、--ods-changes 里堆满假变化，而且全程不报错。

        这条用例同时锁三件事：
          ① 跳过详情的岗位能拿回单位性质
          ② 还原后的内容指纹 == 抓过详情那条的指纹（即「不漂移」）
          ③ 写回 ODS 后 change_count 保持 0（端到端验证，不只是对象层面）
        """
        full = make_job("1", detail=True)
        ods.save_snapshots("b1", [full])

        fresh = make_job("1", detail=False)          # 列表阶段：单位性质为空
        assert fresh.company_nature == "", "前提：列表页拿不到单位性质"

        plan = plan_detail_fetch([fresh], ods.latest_states(), now=self.NOW)
        assert plan.skipped_count == 1, "内容没变就该跳过详情"

        apply_backfill(plan)
        assert fresh.company_nature == "国有企业", "单位性质必须从历史快照回填"
        assert content_fingerprint(fresh) == content_fingerprint(full), \
            "回填后内容指纹必须与上次一致，否则每次增量跑都虚报一次内容变化"

        ods.save_snapshots("b2", [fresh])
        assert ods.latest_states()["xmu:1"].change_count == 0, \
            "跳过详情的第二次抓取不应被记成一次内容变更"

    def test_list_derived_company_fields_are_not_overwritten_by_history(self, ods):
        """
        列表页本次拿到的行业/规模优先，不被历史快照的旧值盖回去。

        行业的权威来源是列表页（详情页也有，但当两处不一致时，
        以本次抓到的为准）。回填只负责补空，不负责改写。
        """
        ods.save_snapshots("b1", [make_job("1", industry="制造业")])
        fresh = make_job("1", detail=False, industry="科学研究和技术服务业")

        plan = plan_detail_fetch([fresh], ods.latest_states(), now=self.NOW)
        apply_backfill(plan)

        assert fresh.industry == "科学研究和技术服务业", \
            "本次列表页已拿到新值，不该被历史值覆盖"

    # ---- 规则 3：列表字段有变化 ----
    def test_list_change_triggers_refetch(self, ods):
        states = self._states(ods, [make_job("1", salary="8000-12000")])
        changed = make_job("1", salary="12000-20000", detail=False)
        plan = plan_detail_fetch([changed], states, now=self.NOW)
        assert plan.fetch_count == 1
        assert plan.reasons["xmu:1"] == REASON_CHANGED

    def test_list_change_check_can_be_disabled(self, ods):
        states = self._states(ods, [make_job("1", salary="8000-12000")])
        changed = make_job("1", salary="12000-20000", detail=False)
        plan = plan_detail_fetch([changed], states, now=self.NOW,
                                 refresh_on_change=False)
        assert plan.fetch_count == 0, "关掉该项后应走 unchanged"

    # ---- 规则 4：历史上从未抓到过详情 ----
    def test_job_without_detail_history_is_refetched(self, ods):
        states = self._states(ods, [make_job("1", detail=False)])
        again = make_job("1", detail=False)
        plan = plan_detail_fetch([again], states, now=self.NOW)
        assert plan.fetch_count == 1
        assert plan.reasons["xmu:1"] == REASON_NO_DETAIL, \
            "上次没抓到详情，这次要补，否则专业字段永远是空的"

    # ---- 规则 5：详情数据超期 ----
    def test_stale_detail_is_refreshed(self, ods):
        old = datetime(2026, 9, 1, 10, 0, 0).strftime("%Y-%m-%d %H:%M:%S")
        ods.save_snapshots("b1", [make_job("1")], crawled_at=old)
        states = ods.latest_states()

        plan = plan_detail_fetch([make_job("1", detail=False)], states,
                                 ttl_days=7, now=self.NOW)
        assert plan.fetch_count == 1
        assert plan.reasons["xmu:1"] == REASON_STALE

    def test_fresh_detail_is_not_refreshed(self, ods):
        recent = (self.NOW - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        ods.save_snapshots("b1", [make_job("1")], crawled_at=recent)
        plan = plan_detail_fetch([make_job("1", detail=False)],
                                 ods.latest_states(), ttl_days=7, now=self.NOW)
        assert plan.fetch_count == 0

    # ---- 规则 1：增量关闭 ----
    def test_disabled_incremental_fetches_everything(self, ods):
        states = self._states(ods, [make_job("1"), make_job("2")])
        plan = plan_detail_fetch(
            [make_job("1", detail=False), make_job("2", detail=False)],
            states, enabled=False, now=self.NOW)
        assert plan.fetch_count == 2
        assert all(r == REASON_FORCED for r in plan.reasons.values())

    # ---- 其他 ----
    def test_missing_keys_reported(self, ods):
        """本批列表未再出现的已知岗位要被报出来（供下架判定）"""
        states = self._states(ods, [make_job("1"), make_job("2"), make_job("3")])
        plan = plan_detail_fetch([make_job("1", detail=False)], states,
                                 now=self.NOW)
        assert plan.missing_keys == {"xmu:2", "xmu:3"}

    def test_duplicate_keys_fetched_once(self, ods):
        """同一批次里重复出现的主键只应抓一次"""
        dup = [make_job("1", detail=False), make_job("1", detail=False)]
        plan = plan_detail_fetch(dup, {}, now=self.NOW)
        assert plan.fetch_count == 1

    def test_job_without_key_is_conservatively_fetched(self, ods):
        """没有主键就无法判断历史，宁可多抓一次"""
        orphan = Job(company="A", title="B")
        plan = plan_detail_fetch([orphan], {}, now=self.NOW)
        assert plan.fetch_count == 1

    def test_summary_is_readable(self, ods):
        states = self._states(ods, [make_job("1")])
        plan = plan_detail_fetch(
            [make_job("1", detail=False), make_job("9", detail=False)],
            states, now=self.NOW)
        text = "\n".join(summarize_plan(plan))
        assert "需抓详情 1 条" in text
        assert "新岗位=1" in text


# ===================================================================
# 4. 最新状态层
# ===================================================================
OLD_TABLE_SQL = """
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT NOT NULL, title TEXT NOT NULL, city TEXT, salary TEXT,
    education TEXT, major_requirement TEXT, apply_method TEXT, deadline TEXT,
    source TEXT, url TEXT, match_score INTEGER DEFAULT 0, match_level TEXT,
    match_label TEXT, hit_keywords TEXT, crawl_time TEXT,
    UNIQUE(company, title)
)
"""


class TestStorageMigration:
    """
    老库自动升级。库里的数据是抓 8 分钟换来的，不能因为改表结构就要求清库。
    """

    def _make_old_db(self, path):
        conn = sqlite3.connect(str(path))
        conn.execute(OLD_TABLE_SQL)
        conn.execute(
            "INSERT INTO jobs (company, title, url, city) VALUES (?, ?, ?, ?)",
            ("厦门点触科技", "游戏数值策划",
             "https://jy.xmu.edu.cn/job/view/id/2401083", "福建省厦门市"),
        )
        conn.commit()
        conn.close()

    def test_adds_missing_columns(self, tmp_path):
        db = tmp_path / "old.db"
        self._make_old_db(db)

        storage = JobStorage(db_path=db)

        assert "job_key" in storage.migrated_columns
        assert "first_seen_at" in storage.migrated_columns
        assert "change_count" in storage.migrated_columns
        assert storage.count() == 1, "迁移不能丢数据"

    def test_backfills_job_key_from_url(self, tmp_path):
        """★ 老记录必须补上主键，否则新老记录会互相不认识而重复入库"""
        db = tmp_path / "old.db"
        self._make_old_db(db)

        storage = JobStorage(db_path=db)
        rows = storage.query_jobs()
        assert rows[0]["job_key"] == "xmu:2401083"
        assert rows[0]["source_job_id"] == "2401083"

    def test_adds_company_attribute_columns(self, tmp_path):
        """
        ★ 老库必须自动补上三个单位属性列。

        库里的 284 条岗位是抓 8 分钟换来的。加字段就要求清库重建，
        等于每次改表都把资产归零——用户的实际选择会变成「不升级」。
        所以这里锁死「自动迁移」这件事本身。
        """
        db = tmp_path / "old.db"
        self._make_old_db(db)

        storage = JobStorage(db_path=db)

        for col in ("industry", "company_nature", "company_scale"):
            assert col in storage.migrated_columns, f"{col} 未自动补列"
        assert storage.count() == 1, "迁移不能丢数据"

    def test_migrated_row_is_recognised_as_existing(self, tmp_path):
        """
        迁移后重新抓到这个岗位，必须走「更新」而不是「新增」。
        这正是补 job_key 的意义。
        """
        db = tmp_path / "old.db"
        self._make_old_db(db)
        storage = JobStorage(db_path=db)

        job = make_job("2401083", company="厦门点触科技", title="游戏数值策划")
        stats = storage.save_jobs_batch("b1", [job])

        assert stats["inserted"] == 0, "老记录应被识别为已存在"
        assert stats["updated"] == 1
        assert storage.count() == 1


class TestStorageLifecycle:

    def test_insert_then_update_counts(self, storage):
        first = storage.save_jobs_batch("b1", [make_job("1")])
        assert first == {"inserted": 1, "updated": 0, "changed": 0}

        second = storage.save_jobs_batch("b2", [make_job("1")])
        assert second == {"inserted": 0, "updated": 1, "changed": 0}, \
            "内容没变就不该计一次变更"

    def test_change_count_increases_only_on_content_change(self, storage):
        storage.save_jobs_batch("b1", [make_job("1", salary="8000-12000")])
        storage.save_jobs_batch("b2", [make_job("1", salary="8000-12000")])
        storage.save_jobs_batch("b3", [make_job("1", salary="12000-20000")])

        row = storage.query_jobs()[0]
        assert row["change_count"] == 1
        assert row["salary"] == "12000-20000"

    def test_first_seen_is_not_overwritten(self, storage):
        """first_seen_at 是「第一次见到」，后续批次不能覆盖它"""
        storage.save_jobs_batch("b1", [make_job("1")])
        first = storage.query_jobs()[0]["first_seen_at"]

        storage.save_jobs_batch("b2", [make_job("1")])
        row = storage.query_jobs()[0]
        assert row["first_seen_at"] == first
        assert row["last_batch_id"] == "b2", "但 last_batch 要跟着更新"

    def test_mark_missing_inactive(self, storage):
        storage.save_jobs_batch("b1", [make_job("1"), make_job("2")])
        marked = storage.mark_missing_inactive("b2", ["xmu:1"])

        assert marked == 1
        assert storage.count_active() == 1
        rows = {r["job_key"]: r for r in storage.query_jobs()}
        assert rows["xmu:1"]["is_active"] == 0
        assert rows["xmu:2"]["is_active"] == 1

    def test_reappearing_job_becomes_active_again(self, storage):
        """之前判失效、后来又在列表里出现 → 必须恢复在架"""
        storage.save_jobs_batch("b1", [make_job("1")])
        storage.mark_missing_inactive("b2", ["xmu:1"])
        assert storage.count_active() == 0

        storage.save_jobs_batch("b3", [make_job("1")])
        assert storage.count_active() == 1

    def test_active_only_filter(self, storage):
        storage.save_jobs_batch("b1", [make_job("1"), make_job("2")])
        storage.mark_missing_inactive("b2", ["xmu:1"])

        assert len(storage.query_jobs()) == 2, "默认包含失效岗位（信息缺失不等于不该投）"
        assert len(storage.query_jobs(include_inactive=False)) == 1

    def test_lifecycle_stats(self, storage):
        storage.save_jobs_batch("b1", [make_job("1"), make_job("2")])
        storage.mark_missing_inactive("b2", ["xmu:2"])

        stats = storage.lifecycle_stats(days=7)
        assert stats["new_in_days"] == 2
        assert stats["inactive"] == 1


class TestStorageCompanyAttributes:
    """
    单位属性在最新状态层的写入 / 更新 / 读取（2026-09-23 新增）。

    这三列是 P0 补采的源字段，落库链路上有三处容易漏：
        建表语句、MIGRATION_COLUMNS、INSERT/UPDATE 的列清单。
    漏掉任何一处都不会报错，只会得到「字段永远为空」。
    """

    def test_insert_and_read_back(self, storage):
        storage.save_jobs_batch("b1", [make_job("1")])

        row = storage.query_jobs()[0]
        assert row["industry"] == "制造业"
        assert row["company_nature"] == "国有企业"
        assert row["company_scale"] == "10000人以上"

    def test_empty_value_does_not_wipe_existing(self, storage):
        """
        ★ 空值不能覆盖已有的值。

        第二次是「跳过详情」的增量跑，company_nature 在内存里是空的
        （历史回填也没命中时）。若 UPDATE 无条件赋值，库里的单位性质
        就被一次「没抓到」擦掉了——而信息缺失不等于信息为空。
        """
        storage.save_jobs_batch("b1", [make_job("1")])
        storage.save_jobs_batch("b2", [make_job("1", detail=False)])

        row = storage.query_jobs()[0]
        assert row["company_nature"] == "国有企业", "空值把已有的单位性质抹掉了"
        assert row["industry"] == "制造业"

    def test_load_all_jobs_restores_company_fields(self, storage):
        """重打分走的是 load_all_jobs，字段还原不了会在写回时被抹空"""
        storage.save_jobs_batch("b1", [make_job("1")])

        job = storage.load_all_jobs()[0]
        assert job.industry == "制造业"
        assert job.company_nature == "国有企业"
        assert job.company_scale == "10000人以上"


class TestRebuildFromOds:
    """
    「ODS 是稳定数据源」的核心断言：
    ★删除最新状态层后，能只靠 ODS 把它一模一样地重建出来。
    """
    def test_rebuild_restores_everything(self, ods, storage):
        ods.open_batch("b1", "incremental")
        ods.save_snapshots("b1", [make_job("1"), make_job("2")])
        storage.save_jobs_batch("b1", [make_job("1"), make_job("2")])
        assert storage.count() == 2

        # 模拟「派生层被误删」
        storage.clear()
        assert storage.count() == 0

        result = storage.rebuild_from_ods(ods, batch_id="rebuild")
        assert result["snapshots"] == 2
        assert storage.count() == 2

        rows = {r["job_key"]: r for r in storage.query_jobs()}
        assert rows["xmu:1"]["salary"] == "8000-12000"
        assert rows["xmu:1"]["major_requirement"] == "数学类、信息与计算科学"
        assert rows["xmu:1"]["first_seen_at"], "首见时间应能从 ODS 还原"
        # 单位属性也要能重放出来，否则 --rebuild-state 会静默削掉三列数据
        assert rows["xmu:1"]["industry"] == "制造业"
        assert rows["xmu:1"]["company_nature"] == "国有企业"
        assert rows["xmu:1"]["company_scale"] == "10000人以上"

    def test_rebuild_restores_change_count(self, ods, storage):
        """变更次数只有 ODS 知道，重建时要能还原"""
        ods.save_snapshots("b1", [make_job("1", salary="8000-12000")])
        ods.save_snapshots("b2", [make_job("1", salary="12000-20000")])

        storage.rebuild_from_ods(ods)
        row = storage.query_jobs()[0]
        assert row["salary"] == "12000-20000", "应取最新快照"
        assert row["change_count"] == 1


# ===================================================================
# 5. 端到端：连续三次增量跑
# ===================================================================
class TestIncrementalEndToEnd:
    """
    模拟真实使用节奏，这是本次改造要保证的核心场景：

        第 1 次   全新抓取        -> 全部岗位都要抓详情
        第 2 次   第二天再跑      -> 全部无变化，一条详情都不抓
        第 3 次   市场上新增岗位  -> 只抓新增的那几个
        第 4 次   某个岗位改薪资  -> 只抓那一个

    改造前这四次跑每次都要发 284 次详情请求（约 7.6 分钟）；
    改造后第 2、3、4 次分别只需要 0 / N / 1 次。
    """

    NOW = datetime(2026, 9, 21, 12, 0, 0)

    @staticmethod
    def _run_batch(ods, storage, batch_id, list_jobs, now, **kw):
        """跑一个批次的完整流程（与 main.run_pipeline 的顺序一致）"""
        ods.open_batch(batch_id, "incremental")
        plan = plan_detail_fetch(list_jobs, ods.latest_states(), now=now, **kw)

        # 模拟「只对 plan.to_fetch 里的岗位抓详情」
        for job in plan.to_fetch:
            job.major_requirement = "数学类、信息与计算科学"
            job.deadline = "2026-10-01"
            job.detail_fetched = True
        apply_backfill(plan)

        ods.save_snapshots(batch_id, list_jobs)
        storage.save_jobs_batch(batch_id, list_jobs)
        ods.close_batch(batch_id, status="ok",
                        new_jobs=plan.count_by_reason().get(REASON_NEW, 0),
                        changed_jobs=plan.count_by_reason().get(REASON_CHANGED, 0),
                        detail_requests=plan.fetch_count,
                        detail_skipped=plan.skipped_count)
        return plan

    def test_four_consecutive_runs(self, ods, storage):
        # ---- 第 1 次：全新 ----
        batch1 = [make_job(str(i), detail=False) for i in (1, 2, 3)]
        plan1 = self._run_batch(ods, storage, "b1", batch1, self.NOW)
        assert plan1.fetch_count == 3, "首批全部要抓详情"
        assert storage.count() == 3

        # ---- 第 2 次：一模一样 ----
        batch2 = [make_job(str(i), detail=False) for i in (1, 2, 3)]
        plan2 = self._run_batch(ods, storage, "b2", batch2,
                                self.NOW + timedelta(days=1))
        assert plan2.fetch_count == 0, "★ 无变化时一条详情都不该抓"
        assert plan2.skipped_count == 3
        assert storage.count() == 3, "也不该产生重复岗位"
        # 落库的专业字段必须还在（来自回填）
        assert all(r["major_requirement"] for r in storage.query_jobs())

        # ---- 第 3 次：新增一个岗位 ----
        batch3 = [make_job(str(i), detail=False) for i in (1, 2, 3, 4)]
        plan3 = self._run_batch(ods, storage, "b3", batch3,
                                self.NOW + timedelta(days=2))
        assert plan3.fetch_count == 1, "★ 只抓新增的那一个"
        assert plan3.reasons["xmu:4"] == REASON_NEW
        assert storage.count() == 4

        # ---- 第 4 次：2 号岗位涨薪 ----
        batch4 = [make_job(str(i), detail=False) for i in (1, 2, 3, 4)]
        batch4[1].salary = "20000-30000"
        plan4 = self._run_batch(ods, storage, "b4", batch4,
                                self.NOW + timedelta(days=3))
        assert plan4.fetch_count == 1, "★ 只有变化的那条要重抓详情"
        assert plan4.reasons["xmu:2"] == REASON_CHANGED

        row = {r["job_key"]: r for r in storage.query_jobs()}["xmu:2"]
        assert row["salary"] == "20000-30000"
        assert row["change_count"] == 1

        # ---- ODS 侧：所有历史都在 ----
        assert ods.count_snapshots() == 3 + 3 + 4 + 4, "每批次每岗位各留一条快照"
        assert ods.count_jobs() == 4
        assert len(ods.history_of("xmu:2")) == 4, "2 号岗位能看到 4 次抓取记录"

    def test_skipped_jobs_keep_major_field(self, ods, storage):
        """
        ★ 回归：增量跳过详情时，专业字段不能被抹空。

        改造中最容易犯的错——跳过详情 → major 为空 → 写库把原值覆盖成空。
        """
        first = [make_job("1", detail=False)]
        self._run_batch(ods, storage, "b1", first, self.NOW)
        assert storage.query_jobs()[0]["major_requirement"]

        second = [make_job("1", detail=False)]
        self._run_batch(ods, storage, "b2", second, self.NOW + timedelta(days=1))

        row = storage.query_jobs()[0]
        assert row["major_requirement"] == "数学类、信息与计算科学", \
            "跳过详情的岗位必须保留原专业字段"
        assert row["deadline"] == "2026-10-01"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
