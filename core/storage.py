# -*- coding: utf-8 -*-
"""
数据存储模块
------------
基于 SQLite 的轻量级持久化层。
核心设计：用 (company, title) 建唯一索引实现 INSERT OR IGNORE 去重。
"""
import sqlite3
from typing import List
from contextlib import contextmanager

from config import DB_PATH
from core.models import Job


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
    -- 去重约束：同一公司同一岗位只保留一条
    UNIQUE(company, title)
);
"""

CREATE_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_city ON jobs(city);",
    "CREATE INDEX IF NOT EXISTS idx_score ON jobs(match_score DESC);",
    "CREATE INDEX IF NOT EXISTS idx_company ON jobs(company);",
]


class JobStorage:
    """岗位数据存储管理器"""

    def __init__(self, db_path=None):
        self.db_path = str(db_path or DB_PATH)
        self._init_db()

    @contextmanager
    def _conn(self):
        """上下文管理器：自动提交/回滚/关闭连接"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        """初始化表结构与索引"""
        with self._conn() as conn:
            conn.execute(CREATE_TABLE_SQL)
            for sql in CREATE_INDEX_SQL:
                conn.execute(sql)

    def save_jobs(self, jobs: List[Job]) -> int:
        """
        批量写入岗位，自动去重。

        :return: 实际新增的条数（被去重忽略的不计入）
        """
        if not jobs:
            return 0

        inserted = 0
        with self._conn() as conn:
            for job in jobs:
                d = job.to_dict()
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO jobs
                    (company, title, city, salary, education, major_requirement,
                     apply_method, deadline, source, url, match_score,
                     match_level, match_label, hit_keywords, crawl_time)
                    VALUES (:company, :title, :city, :salary, :education,
                            :major_requirement, :apply_method, :deadline,
                            :source, :url, :match_score, :match_level,
                            :match_label, :hit_keywords, :crawl_time)
                    """,
                    d,
                )
                inserted += cur.rowcount
        return inserted

    def query_jobs(
        self,
        job_filter=None,
        limit: int = None,
    ) -> List[dict]:
        """
        读取岗位数据，可选地套用一组筛选条件。

        :param job_filter: core.filters.JobFilter 实例；不传则返回全部
        :param limit: 返回条数上限（在筛选之后生效）
        :return: 岗位字典列表，按「目标城市优先 -> 匹配分降序」排列

        实现说明：SQL 只负责取全量并按分数降序；具体条件交给 JobFilter 在
        内存里筛。这样一套条件可以被单独单元测试、存成 JSON 复用，而不用
        把七八个条件拼成动态 SQL。数据量到 10^5 量级时再考虑条件下推。
        """
        sql = "SELECT * FROM jobs ORDER BY match_score DESC, company ASC"

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
            ))
        return jobs

    def update_match_scores(self, jobs: List[Job]) -> int:
        """
        按 (company, title) 更新已有记录的匹配分与等级。

        为什么需要这个方法（重要）：
            save_jobs 用 INSERT OR IGNORE 去重，重复记录会被**跳过**。
            这意味着改了打分规则（config 的权重表）之后再跑一次，
            库中已有记录的分数**不会更新**——必须走这里，或者清库重抓
            （后者要多花 9 分钟重新联网抓一遍，代价大得多）。

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
                     WHERE company = :company AND title = :title
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
