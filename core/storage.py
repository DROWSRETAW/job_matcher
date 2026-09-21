# -*- coding: utf-8 -*-
"""
数据存储模块（最新状态层 / DWD）
--------------------------------
基于 SQLite 的轻量级持久化层，存**每个岗位当前长什么样**。

【它在分层里的位置（2026-09-21 改造）】
    ODS  贴源层    core/ods.py    每次抓取的原始快照，只追加
    DWD  本模块    jobs 表        每个岗位一条最新状态 + 生命周期字段
    ADS  output/   按条件导出的 Excel

为什么本模块不再承担「原始数据」的角色：
    改造前抓到的数据直接写这里，用 UNIQUE(company, title) + INSERT OR IGNORE
    去重——本质是幂等覆盖，重复记录被直接跳过，导致岗位改了薪资也看不出来、
    下架了也不知道。原始事实现在归 ODS 管，这里只存「最新状态」。

【生命周期字段】
    first_seen_at   第一次见到这个岗位的时间
    last_seen_at    最近一次在列表里看到它的时间
    last_batch_id   最近一次见到它的批次
    content_hash    当前内容的指纹
    change_count    内容相对上一次快照变过几次（薪资涨了、专业要求改了）
    is_active       是否仍在招聘（按检索口径可判定时才更新）

这几个字段合起来能回答以前答不了的问题：
    「这个岗位是不是新出现的」「上次看到它是什么时候」「它变过没有」。

【去重的演进】
    旧：UNIQUE(company, title) —— 站点把岗位名从「27届校招-X」改成「X」，
        就会被当成新岗位重复入库（实际发生过）。
    新：以 job_key（= xmu:职位ID）为准，标题改名不会产生重复。
        UNIQUE(company, title) 约束仍然保留，只作兜底。
"""
import re
from datetime import datetime, timedelta
from typing import Dict, List

from config import DB_PATH
from core.dbutil import connect, ensure_columns, table_exists
from core.models import Job, content_fingerprint, make_job_key


# 建表语句
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company         TEXT NOT NULL,
    title           TEXT NOT NULL,
    city            TEXT,
    salary          TEXT,
    education       TEXT,
    major_requirement TEXT,
    apply_method    TEXT,
    deadline        TEXT,
    source          TEXT,
    url             TEXT,
    match_score     INTEGER DEFAULT 0,
    match_level     TEXT,
    match_label     TEXT,
    hit_keywords    TEXT,
    crawl_time      TEXT,
    -- ---- 数据血缘与生命周期（2026-09-21 新增）----
    job_key         TEXT,
    source_job_id   TEXT,
    publish_date    TEXT,
    first_seen_at   TEXT,
    last_seen_at    TEXT,
    last_batch_id   TEXT,
    content_hash    TEXT,
    change_count    INTEGER NOT NULL DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    -- 去重约束：同一公司同一岗位只保留一条（兜底，主键见 idx_jobs_job_key）
    UNIQUE(company, title)
);
"""

CREATE_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_city ON jobs(city);",
    "CREATE INDEX IF NOT EXISTS idx_score ON jobs(match_score DESC);",
    "CREATE INDEX IF NOT EXISTS idx_company ON jobs(company);",
    # 稳定业务主键。SQLite 的唯一索引允许多个 NULL 并存，
    # 所以老记录在 job_key 回填完成前也不会互相冲突。
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_job_key ON jobs(job_key);",
    "CREATE INDEX IF NOT EXISTS idx_active ON jobs(is_active);",
]

# 老库自动升级用：表已存在但缺以下列时补上（不删数据）
MIGRATION_COLUMNS = {
    "job_key": "TEXT",
    "source_job_id": "TEXT",
    "publish_date": "TEXT",
    "first_seen_at": "TEXT",
    "last_seen_at": "TEXT",
    "last_batch_id": "TEXT",
    "content_hash": "TEXT",
    "change_count": "INTEGER NOT NULL DEFAULT 0",
    "is_active": "INTEGER NOT NULL DEFAULT 1",
}


INSERT_SQL = """
INSERT INTO jobs
    (company, title, city, salary, education, major_requirement,
     apply_method, deadline, source, url, match_score,
     match_level, match_label, hit_keywords, crawl_time,
     job_key, source_job_id, publish_date,
     first_seen_at, last_seen_at, last_batch_id,
     content_hash, change_count, is_active)
VALUES
    (:company, :title, :city, :salary, :education, :major_requirement,
     :apply_method, :deadline, :source, :url, :match_score,
     :match_level, :match_label, :hit_keywords, :crawl_time,
     :job_key, :source_job_id, :publish_date,
     :now, :now, :last_batch_id,
     :content_hash, 0, 1)
"""

# 更新已有岗位。
#   · first_seen_at 不动 —— 它是「第一次见到」，不能被覆盖
#   · change_count 只在内容指纹变了时 +1（:delta 由调用方算好）
#   · is_active 置回 1 —— 它又出现在列表里了，说明还在招
#   · job_key 用 COALESCE 兜底赋值，让老记录在首次被重新抓到时补上主键
UPDATE_SQL = """
UPDATE jobs SET
    company           = :company,
    title             = :title,
    city              = :city,
    salary            = :salary,
    education         = :education,
    major_requirement = :major_requirement,
    apply_method      = :apply_method,
    deadline          = :deadline,
    source            = :source,
    url               = :url,
    match_score       = :match_score,
    match_level       = :match_level,
    match_label       = :match_label,
    hit_keywords      = :hit_keywords,
    crawl_time        = :crawl_time,
    job_key           = COALESCE(:job_key, job_key),
    source_job_id     = COALESCE(NULLIF(:source_job_id, ''), source_job_id),
    publish_date      = COALESCE(NULLIF(:publish_date, ''), publish_date),
    last_seen_at      = :now,
    last_batch_id     = :last_batch_id,
    content_hash      = :content_hash,
    change_count      = change_count + :delta,
    is_active         = 1
WHERE id = :row_id
"""


class JobStorage:
    """岗位数据存储管理器（最新状态层）"""

    def __init__(self, db_path=None):
        self.db_path = str(db_path or DB_PATH)
        # 本次初始化补过哪些列（供 --stats 之类的入口提示「已自动升级老库」）
        self.migrated_columns: List[str] = []
        self._init_db()

    def _conn(self):
        """打开连接（提交/回滚/关闭的语义见 core/dbutil.connect）"""
        return connect(self.db_path)

    def _init_db(self):
        """初始化表结构、索引，并自动升级老库"""
        with self._conn() as conn:
            existed = table_exists(conn, "jobs")
            conn.execute(CREATE_TABLE_SQL)

            # 缺列就补（ALTER TABLE ADD COLUMN），不删数据
            self.migrated_columns = ensure_columns(conn, "jobs", MIGRATION_COLUMNS)

            # ★ 顺序关键：必须先补完列，再建依赖新列的索引。
            # 反过来会报 "no such column: job_key"。
            backfilled = 0
            if existed:
                backfilled = self._backfill_job_key(conn)

            for sql in CREATE_INDEX_SQL:
                conn.execute(sql)

        self.backfilled_job_keys = backfilled

    @staticmethod
    def _backfill_job_key(conn) -> int:
        """
        给老记录补上 job_key（从 url 里解析职位 ID）。

        为什么必须做：
            老库里的记录没有 job_key，而新的去重逻辑以 job_key 为准。
            不补的话，同一条岗位会因为「一边有 key 一边没有」而被重复入库。
        """
        rows = conn.execute(
            "SELECT id, url FROM jobs WHERE job_key IS NULL OR job_key = ''"
        ).fetchall()

        filled = 0
        for row in rows:
            m = re.search(r"/job/view/id/(\d+)", row["url"] or "")
            if not m:
                # 解析不出职位 ID 的老记录（如早期按 ID 枚举抓的脏数据）
                # 保持 job_key 为空，由公司+岗位名兜底匹配。
                continue
            jid = m.group(1)
            conn.execute(
                "UPDATE jobs SET job_key = ?, source_job_id = ? WHERE id = ?",
                (make_job_key(jid), jid, row["id"]),
            )
            filled += 1
        return filled

    # ===============================================================
    # 写入
    # ===============================================================
    def save_jobs(self, jobs: List[Job]) -> int:
        """
        批量写入岗位（最新状态层），自动按 job_key 去重。

        保留这个签名是为了向后兼容；需要拿到「新增/更新/内容变化」的
        细分明细时，用 save_jobs_batch()。

        :return: 实际新增的条数
        """
        return self.save_jobs_batch("", jobs)["inserted"]

    def save_jobs_batch(self, batch_id: str, jobs: List[Job]) -> Dict[str, int]:
        """
        按 job_key 更新最新状态层，并维护生命周期字段。

        :param batch_id: 本批抓取批次号（写进 last_batch_id）
        :return: {"inserted": 新岗位, "updated": 已有岗位, "changed": 内容变化数}

        为什么用「先查后写」而不是 INSERT ... ON CONFLICT：
            本表同时存在 UNIQUE(company, title) 这个老约束。若一条新记录
            job_key 没撞上、却撞上了公司+岗位名，SQLite 的 UPSERT 会因
            冲突目标不匹配而直接抛 IntegrityError。显式查一次再决定
            INSERT 还是 UPDATE，行为可预测，也能顺带处理老记录补主键。
            批量只有几百条，这点开销可以忽略。
        """
        stats = {"inserted": 0, "updated": 0, "changed": 0}
        if not jobs:
            return stats

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with self._conn() as conn:
            for job in jobs:
                data = job.to_dict()
                # ★ 空主键必须写成 NULL，不能写成空字符串。
                # 原因：SQLite 的唯一索引里 NULL 之间不算冲突（可以有任意多个），
                # 但空字符串是**普通值**，第二条空主键记录就会撞唯一约束报
                # IntegrityError。没有职位 ID 的记录（手工造的数据、早期脏
                # 数据）会走到这条路，写成 NULL 才能并存。
                key = job.job_key or None
                fingerprint = content_fingerprint(job)

                existing = None
                if key:
                    existing = conn.execute(
                        "SELECT id, content_hash FROM jobs WHERE job_key = ?", (key,)
                    ).fetchone()
                if existing is None:
                    existing = conn.execute(
                        "SELECT id, content_hash FROM jobs "
                        "WHERE company = ? AND title = ?",
                        (job.company, job.title),
                    ).fetchone()

                if existing is None:
                    conn.execute(INSERT_SQL, {
                        **data,
                        "job_key": key,
                        "source_job_id": job.source_job_id or "",
                        "publish_date": job.publish_date or "",
                        "last_batch_id": batch_id,
                        "content_hash": fingerprint,
                        "now": now,
                    })
                    stats["inserted"] += 1
                    continue

                # 内容指纹没变就不算一次变更——增量跑时绝大多数走这条路，
                # change_count 保持不动才是对的。
                changed = (existing["content_hash"] or "") != fingerprint
                conn.execute(UPDATE_SQL, {
                    **data,
                    "job_key": key,
                    "source_job_id": job.source_job_id or "",
                    "publish_date": job.publish_date or "",
                    "last_batch_id": batch_id,
                    "content_hash": fingerprint,
                    "delta": 1 if changed else 0,
                    "row_id": existing["id"],
                    "now": now,
                })
                stats["updated"] += 1
                if changed:
                    stats["changed"] += 1

        return stats

    def mark_missing_inactive(self, batch_id: str, missing_keys) -> int:
        """
        把「本批列表里没再出现」的岗位标记为失效（is_active = 0）。

        :return: 本次被标记的条数

        前置条件由调用方保证：只有当检索条件为「发布时间不限」、
        且翻页没有触顶时，「没出现」才等价于「下架」。
        若检索条件是「近1周」，老岗位不出现在列表里是正常现象，
        调用方必须先判断这个前提（见 main.py 的 _can_judge_missing）。
        """
        keys = [k for k in (missing_keys or []) if k]
        if not keys:
            return 0

        marked = 0
        with self._conn() as conn:
            for key in keys:
                cur = conn.execute(
                    "UPDATE jobs SET is_active = 0, last_batch_id = ? "
                    "WHERE job_key = ? AND is_active = 1",
                    (batch_id, key),
                )
                marked += cur.rowcount
        return marked

    def rebuild_from_ods(self, ods, batch_id: str = "rebuild") -> Dict[str, int]:
        """
        从 ODS 历史重放，重建最新状态层。

        这是「ODS 是稳定数据源」最直接的体现：
            jobs 表坏掉了、被误删了、或者改了字段口径，都可以从 ODS
            重新算出来，不需要重新联网抓一遍（那是 8 分钟起步）。

        :param ods: core.ods.OdsRepository 实例
        :return: {"snapshots": 重放的快照数, ...save_jobs_batch 的统计}
        """
        from core.models import Job as _Job

        snapshots = ods.latest_snapshots()
        states = ods.latest_states()

        jobs: List[Job] = []
        for row in snapshots:
            key = row["job_key"]
            state = states.get(key)
            jobs.append(_Job(
                company=row["company"] or "",
                title=row["title"] or "",
                city=row["city"] or "",
                salary=row["salary"] or "",
                education=row["education"] or "",
                major_requirement=row["major_requirement"] or "",
                apply_method=row["apply_method"] or "",
                deadline=row["deadline"] or "",
                publish_date=row["publish_date"] or "",
                source=row["source"] or "",
                url=row["url"] or "",
                job_key=key,
                source_job_id=row["source_job_id"] or "",
                crawl_time=row["crawled_at"] or "",
            ))
            # 重放时把 lifecycle 也带过去（save_jobs_batch 只维护 last_seen）
            setattr(jobs[-1], "_ods_state", state)

        self.clear()
        result = self.save_jobs_batch(batch_id, jobs)

        # 回填首见时间与变更次数：这两项只有 ODS 知道
        with self._conn() as conn:
            for job in jobs:
                state = getattr(job, "_ods_state", None)
                if state is None:
                    continue
                conn.execute(
                    "UPDATE jobs SET first_seen_at = ?, last_seen_at = ?, "
                    "change_count = ? WHERE job_key = ?",
                    (state.first_seen_at or state.last_seen_at,
                     state.last_seen_at, state.change_count, job.job_key),
                )

        result["snapshots"] = len(snapshots)
        return result

    def query_jobs(
        self,
        job_filter=None,
        limit: int = None,
        include_inactive: bool = True,
    ) -> List[dict]:
        """
        读取岗位数据，可选地套用一组筛选条件。

        :param job_filter: core.filters.JobFilter 实例；不传则返回全部
        :param limit: 返回条数上限（在筛选之后生效）
        :param include_inactive: 是否包含已下架岗位。
            默认为 True —— 「失效」只是「本批列表里没再出现」，
            信息缺失不等于该岗位不该投，宁可多给一条。要只看在架的
            传 False。
        :return: 岗位字典列表，按「目标城市优先 -> 匹配分降序」排列

        实现说明：SQL 只负责取全量并按分数降序；具体条件交给 JobFilter 在
        内存里筛。这样一套条件可以被单独单元测试、存成 JSON 复用，而不用
        把七八个条件拼成动态 SQL。数据量到 10^5 量级时再考虑条件下推。
        """
        sql = "SELECT * FROM jobs ORDER BY match_score DESC, company ASC"
        if not include_inactive:
            sql = ("SELECT * FROM jobs WHERE is_active = 1 "
                   "ORDER BY match_score DESC, company ASC")

        with self._conn() as conn:
            rows = [dict(r) for r in conn.execute(sql).fetchall()]

        if job_filter is not None:
            rows = job_filter.apply(rows)
        if limit:
            rows = rows[:limit]
        return rows

    def count(self) -> int:
        """统计库中岗位总数"""
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    def count_active(self) -> int:
        """统计仍在架（最近一次抓取时仍出现在列表里）的岗位数"""
        with self._conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE is_active = 1").fetchone()[0]

    def lifecycle_stats(self, days: int = 7) -> Dict[str, int]:
        """
        生命周期概览：这是分层之后才答得出来的问题。

        · 最近 N 天新增了多少岗位（看 first_seen_at）
        · 有多少岗位的抓取内容发生过变化（change_count > 0）
        · 有多少岗位已标记失效（is_active = 0）
        """
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as conn:
            new_in_days = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE first_seen_at >= ?", (since,)
            ).fetchone()[0]
            changed = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE change_count > 0").fetchone()[0]
            inactive = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE is_active = 0").fetchone()[0]
            first_seen = conn.execute(
                "SELECT MIN(first_seen_at) FROM jobs").fetchone()[0]
            last_seen = conn.execute(
                "SELECT MAX(last_seen_at) FROM jobs").fetchone()[0]

        return {
            "new_in_days": new_in_days,
            "changed": changed,
            "inactive": inactive,
            "first_seen_at": first_seen or "",
            "last_seen_at": last_seen or "",
        }

    def clear(self) -> int:
        """
        清空岗位表，返回删除条数。

        用途：旧版爬虫用「职位 ID 枚举」抓到的数据城市分布随机、
        覆盖度极低，与检索接口抓到的数据混在一张表里会污染筛选结果
        （实测出现「深圳 11 条、北京 6 条」混在厦门清单里）。
        切换抓取策略后建议清一次库重抓。

        注意：只删数据，不删表结构与索引。
        """
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM jobs")
            return cur.rowcount

    def load_all_jobs(self) -> List[Job]:
        """
        把库中全部记录还原成 Job 对象（用于重新打分）。

        与 query_jobs 的区别：query_jobs 返回 dict（给筛选/导出用），
        这里返回 Job 对象（给打分器用）。
        """
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM jobs").fetchall()

        jobs = []
        for r in rows:
            kw = r["hit_keywords"] or ""
            jobs.append(Job(
                company=r["company"], title=r["title"],
                city=r["city"], salary=r["salary"],
                education=r["education"],
                major_requirement=r["major_requirement"],
                apply_method=r["apply_method"], deadline=r["deadline"],
                source=r["source"], url=r["url"],
                match_score=r["match_score"], match_level=r["match_level"],
                match_label=r["match_label"],
                hit_keywords=[k for k in kw.split(",") if k],
                crawl_time=r["crawl_time"],
                # 血缘字段也要还原：否则重算后写回时会把主键抹掉，
                # 下一次增量抓取就会把这些岗位当成全新的重新抓一遍。
                job_key=r["job_key"] or "",
                source_job_id=r["source_job_id"] or "",
                publish_date=r["publish_date"] or "",
            ))
        return jobs

    def update_match_scores(self, jobs: List[Job]) -> int:
        """
        更新已有记录的匹配分与等级。

        为什么需要这个方法（重要）：
            改了打分规则（config 的权重表）之后再跑一次，库中已有记录的
            分数不会自动更新——要么走这里，要么清库重抓（后者要多花
            8 分钟重新联网抓一遍，代价大得多）。
            这是「数据与算法解耦」的直接收益：原始数据不动，只重算派生字段。

        匹配方式：优先用 job_key（稳定主键），退回到 公司+岗位名。
            老记录可能还没有 job_key（url 解析不出职位 ID），
            所以两条路都要留着。

        :return: 实际更新的条数
        """
        if not jobs:
            return 0

        updated = 0
        with self._conn() as conn:
            for job in jobs:
                d = job.to_dict()
                cur = conn.execute(
                    """
                    UPDATE jobs
                       SET match_score  = :match_score,
                           match_level  = :match_level,
                           match_label  = :match_label,
                           hit_keywords = :hit_keywords
                     WHERE (:job_key IS NOT NULL AND job_key = :job_key)
                        OR (company = :company AND title = :title)
                    """,
                    d,
                )
                updated += cur.rowcount
        return updated

    def count_by_level(self) -> dict:
        """按匹配等级统计数量"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT match_level, COUNT(*) AS cnt FROM jobs "
                "GROUP BY match_level ORDER BY match_level"
            ).fetchall()
        return {r["match_level"]: r["cnt"] for r in rows}

    def count_by_city(self, limit: int = 8) -> List[tuple]:
        """按城市统计数量（取前 N），用于快速判断数据分布"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT city, COUNT(*) AS cnt FROM jobs "
                "GROUP BY city ORDER BY cnt DESC, city ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [(r["city"], r["cnt"]) for r in rows]
