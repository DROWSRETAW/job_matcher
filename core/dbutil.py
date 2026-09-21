# -*- coding: utf-8 -*-
"""
SQLite 公共工具
---------------
被 ODS 贴源层（core/ods.py）与最新状态层（core/storage.py）共用。

抽出来的原因：
    分层之后，两张表要各自管一个 SQLite 连接，「开连接 / 提交 / 回滚 /
    关连接」这套样板代码本来要写两遍（storage 原本已有一份）。多一层
    就多抄一份，将来加 DWS 层还要再抄。这里统一一次，语义只有一处定义。

不引入 ORM 的理由：
    本项目的查询都是「整表取回 + 内存里筛」，SQL 短且直白，
    用 ORM 反而要多维护一套模型映射。规模在 10^5 行以内时，
    原生 sqlite3 足够。
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Dict


@contextmanager
def connect(db_path):
    """
    打开一个 SQLite 连接，自动提交 / 回滚 / 关闭。

    用法：
        with connect(path) as conn:
            conn.execute(...)

    提交时机：with 块正常结束时提交。块内抛异常则回滚并向上抛——
    保证「写 ODS 快照 + 刷新最新状态」这类跨表操作要么全成要么全不成。
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """表是否已存在"""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table: str) -> set:
    """查表当前有哪些列（表不存在则返回空集合）"""
    if not table_exists(conn, table):
        return set()
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def ensure_columns(conn: sqlite3.Connection, table: str,
                   columns: Dict[str, str]) -> list:
    """
    给已存在的表补齐缺失的列（自动迁移）。

    :param columns: {列名: 列定义}，如 {"job_key": "TEXT"}
    :return: 本次实际新增的列名列表

    【为什么必须做自动迁移，而不是让用户删库重建】
        库里的数据是「抓一次要 8 分钟」换来的，还带着历史。
        升级代码就要求清库，等于每次改表都把资产归零——
        用户的实际选择会是「不升级」，那分层就白做了。
        SQLite 支持 ALTER TABLE ADD COLUMN（老版本也支持），
        新增列在旧记录上自动为 NULL，代价几乎为零。

    【注意】
        ADD COLUMN 不能带 UNIQUE / PRIMARY KEY 约束，也不能设 NOT NULL
        之外的有趣约束。所以唯一索引一律用 CREATE UNIQUE INDEX 单独建，
        不要混进列定义里。
    """
    existing = table_columns(conn, table)
    if not existing:
        return []

    added = []
    for name, ddl in columns.items():
        if name in existing:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        added.append(name)
    return added


def ensure_dir(path):
    """确保某个路径的父目录存在（写库 / 导出前调用）"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
