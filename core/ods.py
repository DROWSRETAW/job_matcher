# -*- coding: utf-8 -*-
"""
ODS 贴源层（Operational Data Store）
====================================

一句话职责：**如实记录每一次抓到了什么，只追加，不修改，不删除。**

────────────────────────────────────────────────────────────────
【为什么要有这一层】
────────────────────────────────────────────────────────────────
改造前抓到的数据直接写进 jobs 表，用 UNIQUE(company,title) 做
INSERT OR IGNORE。这套做法的本质是「幂等覆盖」——同一个岗位重复抓到就跳过，
库里永远只有一份「第一次见到时的样子」。由此产生三个问题：

  1. **历史不可追**：岗位改薪资、改截止时间、甚至下架，库里看不出来。
     你只能看到「现在有这条」，看不到「它从 8000 变成了 12000」。
  2. **增量无从判断**：判断不了某条岗位是新出现的还是三天前就有的，
     所以每次都得把几百个详情页重抓一遍。
  3. **无法重算**：一旦发现某次抓取解析出错，原始数据已经没了，
     只能重新联网抓一遍（约 8 分钟）。

ODS 把「抓到的事实」和「加工的结果」彻底分开：

    ods_job_snapshot   每次抓取的原始结果，一条快照一行
    ods_crawl_batch    抓取批次台账（这次跑了什么条件、耗时、抓到多少）

────────────────────────────────────────────────────────────────
【三条设计约定，越界就失去 ODS 的意义】
────────────────────────────────────────────────────────────────
1. **只追加**（append-only）
   同一岗位多次抓取就存多行快照，绝不 UPDATE / DELETE 历史行。
   想清理只能按批次整批删（并明确知道自己在丢弃历史）。

2. **不存派生字段**
   匹配分、匹配等级、命中关键词**一律不写进 ODS**。
   它们是本地算法加工的结果——改了 config 的权重表就要重算。
   若写进快照，同一岗位的历史快照里会并存多套互相矛盾的分数，
   而「贴源」的语义要求这里只有站点给的原始事实。
   派生字段属于 jobs（最新状态层），可以随时重算、随时丢弃。

3. **快照必须自洽**
   每条快照写入时算出两个指纹（见 core/models.py）：
       list_hash     列表字段指纹 —— 增量判定用
       content_hash  全字段指纹   —— 变更检测用
   指纹的意义在于：判断「有没有变」变成一次字符串比较，
   而不是逐字段比对（那样每加一个字段都要改一遍比对逻辑）。

────────────────────────────────────────────────────────────────
【它同时是增量抓取的字段来源】
────────────────────────────────────────────────────────────────
增量模式下，没变化的岗位不重新抓详情页。但详情页才有「需求专业」，
不抓就意味着这个字段是空的——直接写库会把数据弄残缺。
所以跳过详情时，从 ODS 里取该岗位**最近一次真正抓到详情的快照**
回填专业与截止时间（latest_states() 的 detail_at 那一组字段）。
只有 append-only 才能做到这一点：历次抓到的事实都还在。
"""
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from config import DB_PATH, ODS_SOURCE_KEY, ORPHAN_BATCH_HOURS
from core.dbutil import connect, ensure_columns
from core.models import Job, list_fingerprint, content_fingerprint


# ===================================================================
# 建表语句
# ===================================================================
CREATE_SNAPSHOT_SQL = """
CREATE TABLE IF NOT EXISTS ods_job_snapshot (
    snapshot_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key           TEXT NOT NULL,        -- 稳定主键，如 xmu:2401083
    source            TEXT,                 -- 数据源名称
    source_job_id     TEXT,                 -- 站点侧职位 ID
    batch_id          TEXT NOT NULL,        -- 所属抓取批次
    crawled_at        TEXT NOT NULL,        -- 本批次抓取时刻
    -- ---- 以下为站点原样字段，不做任何业务加工 ----
    company           TEXT,
    title             TEXT,
    city              TEXT,
    salary            TEXT,
    education         TEXT,
    major_requirement TEXT,
    apply_method      TEXT,
    deadline          TEXT,
    publish_date      TEXT,
    url               TEXT,
    -- ---- 单位属性（2026-09-23 新增，站点原样值，不做归一化）----
    -- 归一化属于 DWD 的职责，ODS 只如实记录页面怎么写的
    industry          TEXT,
    company_nature    TEXT,
    company_scale     TEXT,
    -- ---- 指纹与状态标记 ----
    list_hash         TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    detail_fetched    INTEGER NOT NULL DEFAULT 0,   -- 本条是否真抓了详情页
    -- 同一批次同一岗位只需留一条快照
    UNIQUE(job_key, batch_id)
);
"""

CREATE_BATCH_SQL = """
CREATE TABLE IF NOT EXISTS ods_crawl_batch (
    batch_id        TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    mode            TEXT,                   -- incremental / full
    profile_json    TEXT,                   -- 本次检索条件（可复现）
    status          TEXT,                   -- running / ok / partial / failed
    list_pages      INTEGER DEFAULT 0,
    list_items      INTEGER DEFAULT 0,
    new_jobs        INTEGER DEFAULT 0,      -- 首次见到的职位数
    changed_jobs    INTEGER DEFAULT 0,      -- 列表字段有变化的职位数
    unchanged_jobs  INTEGER DEFAULT 0,      -- 无需重抓详情的职位数
    missing_jobs    INTEGER DEFAULT 0,      -- 本批未再出现的已知职位数
    detail_requests INTEGER DEFAULT 0,      -- 本批实际发出的详情请求数
    detail_skipped  INTEGER DEFAULT 0,      -- 因增量省下的详情请求数
    note            TEXT
);
"""

CREATE_INDEX_SQL = [
    # 取「某岗位的最近快照」是最高频的查询，按 (job_key, snapshot_id) 建索引
    "CREATE INDEX IF NOT EXISTS idx_ods_job ON ods_job_snapshot(job_key, snapshot_id DESC);",
    "CREATE INDEX IF NOT EXISTS idx_ods_batch ON ods_job_snapshot(batch_id);",
    "CREATE INDEX IF NOT EXISTS idx_ods_crawled ON ods_job_snapshot(crawled_at);",
]

# 老库升级：ODS 表若已存在但缺列，自动补上（不删数据）
SNAPSHOT_MIGRATION_COLUMNS = {
    "publish_date": "TEXT",
    "list_hash": "TEXT",
    "content_hash": "TEXT",
    "detail_fetched": "INTEGER NOT NULL DEFAULT 0",
    "industry": "TEXT",
    "company_nature": "TEXT",
    "company_scale": "TEXT",
}


# ===================================================================
# 职位在 ODS 中的已知状态（增量判定的输入）
# ===================================================================
@dataclass
class JobState:
    """
    一个职位在 ODS 里的「已知全部信息」。

    注意 detail_at / major_requirement / deadline 三个字段的语义：
    它们来自**最近一次真正抓到详情的快照**，而不是最近一条快照。
    因为增量跑时最新快照可能是「跳过详情」写下的，
    此时这三个字段就是增量回填的来源。
    """
    job_key: str = ""
    source_job_id: str = ""
    company: str = ""
    title: str = ""
    list_hash: str = ""            # 最近快照的列表指纹
    content_hash: str = ""         # 最近快照的全字段指纹
    first_seen_at: str = ""        # 第一次见到这个岗位的时刻
    last_seen_at: str = ""         # 最近一次出现在列表里的时刻
    last_batch_id: str = ""
    detail_at: str = ""            # 最近一次真正抓到详情的时刻（"" = 从未）
    major_requirement: str = ""    # 来自 detail_at 那条快照
    deadline: str = ""
    # 单位属性同理，来自 detail_at 那条快照：
    # company_nature 只有详情页有，是这三项里真正会被用到的回填来源；
    # industry / company_scale 列表页本来就有，这里是列表页偶发缺值时的兜底。
    industry: str = ""
    company_nature: str = ""
    company_scale: str = ""
    change_count: int = 0          # 历史上内容指纹发生变化的次数

    @property
    def has_detail(self) -> bool:
        """是否已有可回填的详情数据"""
        return bool(self.detail_at)


# ===================================================================
# 查询语句
# ===================================================================
# 每个 job_key 的最新快照。
# 用 MAX(snapshot_id) 而不是 MAX(crawled_at)：同一批次内所有快照的
# crawled_at 是同一个值，比不出先后；snapshot_id 是自增主键，单调递增，
# 天然表达插入顺序。
LATEST_SQL = """
SELECT s.* FROM ods_job_snapshot s
  JOIN (SELECT job_key, MAX(snapshot_id) AS sid
          FROM ods_job_snapshot GROUP BY job_key) t
    ON s.snapshot_id = t.sid
"""

# 每个 job_key 最近一次「真正抓到详情」的快照
LATEST_DETAIL_SQL = """
SELECT s.* FROM ods_job_snapshot s
  JOIN (SELECT job_key, MAX(snapshot_id) AS sid
          FROM ods_job_snapshot WHERE detail_fetched = 1 GROUP BY job_key) t
    ON s.snapshot_id = t.sid
"""

# 内容发生变化的历史记录。
# 窗口函数 LAG 取「同一职位上一条快照的指纹」，与当前比，不同即为一次变更。
# 需要 SQLite ≥ 3.25（Python 3.8 起自带的版本已满足）。
CHANGES_SQL = """
WITH ordered AS (
    SELECT s.*,
           LAG(s.content_hash) OVER (
               PARTITION BY s.job_key ORDER BY s.snapshot_id
           ) AS prev_hash
      FROM ods_job_snapshot s
)
SELECT * FROM ordered
 WHERE prev_hash IS NOT NULL
   AND prev_hash <> content_hash
   AND crawled_at >= ?
 ORDER BY crawled_at DESC, job_key ASC
"""

# 每个 job_key 第一次出现的时刻（= 首见时间，只有 ODS 知道）
FIRST_SEEN_SQL = """
SELECT job_key, MIN(crawled_at) AS first_seen
  FROM ods_job_snapshot GROUP BY job_key
"""

# 变更次数统计（同样的 LAG 思路，只做聚合）
CHANGE_COUNTS_SQL = """
WITH ordered AS (
    SELECT s.job_key,
           LAG(s.content_hash) OVER (
               PARTITION BY s.job_key ORDER BY s.snapshot_id
           ) AS prev_hash,
           s.content_hash
      FROM ods_job_snapshot s
)
SELECT job_key, COUNT(*) AS changes FROM ordered
 WHERE prev_hash IS NOT NULL AND prev_hash <> content_hash
 GROUP BY job_key
"""


def new_batch_id(when: Optional[datetime] = None) -> str:
    """生成批次号（精确到秒，同一秒不会开两个批次）"""
    return (when or datetime.now()).strftime("%Y%m%d_%H%M%S")


class OdsRepository:
    """ODS 贴源层读写入口"""

    def __init__(self, db_path=None):
        self.db_path = str(db_path or DB_PATH)
        self._init_db()

    def _init_db(self):
        with connect(self.db_path) as conn:
            conn.execute(CREATE_SNAPSHOT_SQL)
            conn.execute(CREATE_BATCH_SQL)
            # 兼容老库：表已存在但结构较旧时补齐缺失列
            ensure_columns(conn, "ods_job_snapshot", SNAPSHOT_MIGRATION_COLUMNS)
            for sql in CREATE_INDEX_SQL:
                conn.execute(sql)

    # ===============================================================
    # 批次台账
    # ===============================================================
    def open_batch(self, batch_id: str, mode: str,
                   profile_json: str = "") -> str:
        """
        开一个抓取批次。

        为什么批次要单独记账：
            只有知道「这一批是什么条件、什么时候跑的」，ODS 的历史
            才有解读口径。否则一堆快照堆在一起，无法回答
            「上周按近1周条件抓的那批结果在哪」。
        """
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO ods_crawl_batch
                    (batch_id, started_at, mode, profile_json, status)
                VALUES (?, ?, ?, ?, 'running')
                """,
                (batch_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 mode, profile_json),
            )
        return batch_id

    def orphan_batches(self, stale_hours: float = ORPHAN_BATCH_HOURS) -> List[dict]:
        """
        找出「挂着 running 但其实早就死了」的批次。

        为什么需要：进程被强杀（工具超时、断网、直接关窗口）时，
        close_batch 根本没机会执行，批次就永远停在 running。
        ODS 台账里出现「永远在跑」的批次是一种假账——监控会误判，
        人工排查时也分不清「真的在跑」还是「死了没埋」。

        :param stale_hours: 只认开始时间早于这么久之前的批次，
                            避免误伤正在进行中的任务
        """
        if stale_hours is None or stale_hours < 0:
            stale_hours = ORPHAN_BATCH_HOURS
        cutoff = (datetime.now() - timedelta(hours=stale_hours)) \
            .strftime("%Y-%m-%d %H:%M:%S")
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM ods_crawl_batch
                 WHERE status = 'running'
                   AND finished_at IS NULL
                   AND (started_at IS NULL OR started_at < ?)
                 ORDER BY started_at
                """,
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    def close_orphan_batches(self, stale_hours: float = ORPHAN_BATCH_HOURS,
                             note: str = "") -> List[str]:
        """
        把孤儿批次收尾为 failed。

        :return: 被收尾的批次号列表
        """
        orphans = self.orphan_batches(stale_hours)
        if not orphans:
            return []

        note = note or f"进程被外部中断，未正常收尾（于 {datetime.now():%Y-%m-%d %H:%M} 补记）"
        closed = []
        with connect(self.db_path) as conn:
            for b in orphans:
                conn.execute(
                    "UPDATE ods_crawl_batch SET status = 'failed', "
                    "finished_at = ?, note = ? WHERE batch_id = ?",
                    (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), note,
                     b["batch_id"]),
                )
                closed.append(b["batch_id"])
        return closed

    def close_batch(self, batch_id: str, status: str = "ok",
                    note: str = "", **counters) -> None:
        """
        收批次：写入结束时间、状态与各项计数。

        :param counters: list_pages / list_items / new_jobs / changed_jobs /
                         unchanged_jobs / missing_jobs /
                         detail_requests / detail_skipped
        """
        allowed = {
            "list_pages", "list_items", "new_jobs", "changed_jobs",
            "unchanged_jobs", "missing_jobs", "detail_requests",
            "detail_skipped",
        }
        sets = ["finished_at = ?", "status = ?", "note = ?"]
        params = [datetime.now().strftime("%Y-%m-%d %H:%M:%S"), status, note]

        for key, value in counters.items():
            if key in allowed and value is not None:
                sets.append(f"{key} = ?")
                params.append(int(value))

        params.append(batch_id)
        with connect(self.db_path) as conn:
            conn.execute(
                f"UPDATE ods_crawl_batch SET {', '.join(sets)} WHERE batch_id = ?",
                params,
            )

    def get_batch(self, batch_id: str) -> Optional[dict]:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM ods_crawl_batch WHERE batch_id = ?", (batch_id,)
            ).fetchone()
        return dict(row) if row else None

    def recent_batches(self, limit: int = 10) -> List[dict]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM ods_crawl_batch ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ===============================================================
    # 写快照
    # ===============================================================
    def save_snapshots(self, batch_id: str, jobs: List[Job],
                       crawled_at: Optional[str] = None,
                       source: str = "") -> int:
        """
        把本批次抓到的岗位原样写入快照表。

        :return: 实际写入的快照条数（无 job_key 的记录会被跳过）

        同一批次同一岗位只留一条（UNIQUE(job_key, batch_id) + OR IGNORE），
        所以重跑同一批次号不会产生重复快照。
        """
        if not jobs:
            return 0

        stamp = crawled_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        inserted = 0
        skipped = 0

        with connect(self.db_path) as conn:
            for job in jobs:
                key = job.job_key
                if not key:
                    # 拿不到稳定主键的记录不进 ODS：
                    # 没有主键就无法追溯同一条岗位的历史，写进来只是噪音。
                    skipped += 1
                    continue

                conn.execute(
                    """
                    INSERT OR IGNORE INTO ods_job_snapshot
                        (job_key, source, source_job_id, batch_id, crawled_at,
                         company, title, city, salary, education,
                         major_requirement, apply_method, deadline, publish_date,
                         url, industry, company_nature, company_scale,
                         list_hash, content_hash, detail_fetched)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        source or job.source or "",
                        job.source_job_id or "",
                        batch_id,
                        stamp,
                        job.company, job.title, job.city, job.salary,
                        job.education, job.major_requirement,
                        job.apply_method, job.deadline, job.publish_date,
                        job.url, job.industry, job.company_nature,
                        job.company_scale,
                        list_fingerprint(job),
                        content_fingerprint(job),
                        1 if job.detail_fetched else 0,
                    ),
                )
                inserted += conn.execute("SELECT changes()").fetchone()[0]

        self._last_skipped = skipped
        return inserted

    # ===============================================================
    # 读快照 —— 增量判定的输入
    # ===============================================================
    def latest_states(self) -> Dict[str, JobState]:
        """
        返回每个职位的已知状态（增量判定的输入）。

        两趟查询合并：
            第 1 趟拿「最新快照」 —— 列表指纹、内容指纹、最近出现时间
            第 2 趟拿「最新含详情的快照」 —— 可回填的专业与截止时间
        分开查的原因：最新快照可能是跳过详情写下的（detail_fetched=0），
        此时它的专业字段是空的，不能拿来回填。
        """
        with connect(self.db_path) as conn:
            latest = {r["job_key"]: r for r in conn.execute(LATEST_SQL)}
            details = {r["job_key"]: r for r in conn.execute(LATEST_DETAIL_SQL)}
            changes = self._change_counts(conn)
            first_seen = {r["job_key"]: r["first_seen"]
                          for r in conn.execute(FIRST_SEEN_SQL)}

        states: Dict[str, JobState] = {}
        for key, row in latest.items():
            detail = details.get(key)
            states[key] = JobState(
                job_key=key,
                source_job_id=row["source_job_id"] or "",
                company=row["company"] or "",
                title=row["title"] or "",
                list_hash=row["list_hash"] or "",
                content_hash=row["content_hash"] or "",
                first_seen_at=first_seen.get(key) or "",
                last_seen_at=row["crawled_at"] or "",
                last_batch_id=row["batch_id"] or "",
                detail_at=(detail["crawled_at"] if detail else ""),
                major_requirement=((detail["major_requirement"] if detail else "") or ""),
                deadline=((detail["deadline"] if detail else "") or ""),
                industry=((detail["industry"] if detail else "") or ""),
                company_nature=((detail["company_nature"] if detail else "") or ""),
                company_scale=((detail["company_scale"] if detail else "") or ""),
                change_count=changes.get(key, 0),
            )
        return states

    @staticmethod
    def _change_counts(conn) -> Dict[str, int]:
        """统计每个职位的内容变更次数"""
        rows = conn.execute(CHANGE_COUNTS_SQL).fetchall()
        return {r["job_key"]: r["changes"] for r in rows}

    def state_of(self, job_key: str) -> Optional[JobState]:
        """单个职位的已知状态"""
        return self.latest_states().get(job_key)

    def latest_snapshots(self) -> List[dict]:
        """
        每个职位的最新快照（完整字段）。

        与 latest_states() 的区别：那个返回整理过的 JobState（够增量判定用），
        这个返回原始行，供「从 ODS 重建最新状态层」使用——重建需要拿到
        每一个字段，而不能只有指纹和状态。
        """
        with connect(self.db_path) as conn:
            rows = conn.execute(LATEST_SQL).fetchall()
        return [dict(r) for r in rows]

    # ===============================================================
    # 变更历史
    # ===============================================================
    def history_of(self, job_key: str) -> List[dict]:
        """某职位的全部快照（按时间升序），用于查看它怎么变的"""
        with connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM ods_job_snapshot WHERE job_key = ? "
                "ORDER BY snapshot_id ASC",
                (job_key,),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_changes(self, days: int = 7) -> List[dict]:
        """
        最近 N 天内内容发生变化（相对上一条快照）的快照记录。

        这就是 ODS 相对旧方案的直接价值：能回答
        「这个岗位的薪资/专业要求是什么时候变的」。
        """
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        with connect(self.db_path) as conn:
            rows = conn.execute(CHANGES_SQL, (since,)).fetchall()
        return [dict(r) for r in rows]

    # ===============================================================
    # 统计
    # ===============================================================
    def count_snapshots(self) -> int:
        with connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM ods_job_snapshot").fetchone()[0]

    def count_jobs(self) -> int:
        """ODS 里出现过多少个不同职位"""
        with connect(self.db_path) as conn:
            return conn.execute(
                "SELECT COUNT(DISTINCT job_key) FROM ods_job_snapshot"
            ).fetchone()[0]

    def stats(self) -> dict:
        """ODS 概览（供 --ods-stats 使用）"""
        with connect(self.db_path) as conn:
            snapshots = conn.execute(
                "SELECT COUNT(*) FROM ods_job_snapshot").fetchone()[0]
            jobs = conn.execute(
                "SELECT COUNT(DISTINCT job_key) FROM ods_job_snapshot").fetchone()[0]
            batches = conn.execute(
                "SELECT COUNT(*) FROM ods_crawl_batch").fetchone()[0]
            with_detail = conn.execute(
                "SELECT COUNT(*) FROM ods_job_snapshot WHERE detail_fetched = 1"
            ).fetchone()[0]
            first_seen = conn.execute(
                "SELECT MIN(crawled_at) FROM ods_job_snapshot").fetchone()[0]
            last_seen = conn.execute(
                "SELECT MAX(crawled_at) FROM ods_job_snapshot").fetchone()[0]

        rules = {}
        for rule in ("SN:same",):
            pass
        # 变更统计：有多少职位出现过内容变化
        changed = {r["job_key"] for r in self.recent_changes(days=3650)}

        return {
            "snapshots": snapshots,
            "jobs": jobs,
            "batches": batches,
            "snapshots_with_detail": with_detail,
            "changed_jobs": len(changed),
            "first_batch_at": first_seen or "",
            "last_batch_at": last_seen or "",
            "source_key": ODS_SOURCE_KEY,
        }

    def clear(self) -> int:
        """
        清空 ODS 全部快照与批次台账，返回删除的快照条数。

        这是**丢弃历史**的操作，只应在明确要重来时使用
        （例如站点改版导致历史解析结果不可信）。
        """
        with connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM ods_job_snapshot").fetchone()[0]
            conn.execute("DELETE FROM ods_job_snapshot")
            conn.execute("DELETE FROM ods_crawl_batch")
        return count
