# 创建统一数据库连接池
#!/usr/bin/env python3
"""统一数据库连接池——所有模块共用，避免每次新建连接"""

from contextlib import contextmanager
from psycopg2.pool import SimpleConnectionPool
from psycopg2.extras import RealDictCursor

PG_CONFIG = {
    "dbname": "memory_system",
    "user": "memory_user",
    "password": "memory_pass_2026",
    "host": "localhost",
    "port": 5432,
}

# 全局连接池：最小 5 个连接，最大 20 个
_pool = None

def get_pool():
    """获取全局连接池（单例模式）"""
    global _pool
    if _pool is None:
        _pool = SimpleConnectionPool(5, 20, **PG_CONFIG)
    return _pool

@contextmanager
def get_conn():
    """从连接池获取一个连接，用完后自动归还"""
    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
    finally:
        pool.putconn(conn)

@contextmanager
def get_cursor():
    """获取一个使用 RealDictCursor 的游标"""
    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        try:
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()

# 测试用例在 终端pythonEOF脚本集.md 中。