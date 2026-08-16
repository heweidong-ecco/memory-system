# conftest.py — pytest 全局配置
# 统一管理数据库连接与测试数据清理，目录下所有测试文件共享。
# 数据库连接统一走 db_pool.get_cursor()（连接池），测试无需单独建连。
import pytest

from db_pool import get_cursor
from cache_manager import redis_client


def _cleanup_test_data():
    """删除测试用户（user_test_%）在 ledger / user_profile / Redis 中的数据"""
    # 1. Ledger（append-only，临时禁用触发器才能 DELETE）
    with get_cursor() as cur:
        cur.execute("ALTER TABLE ledger DISABLE TRIGGER ledger_append_only_trigger")
        cur.execute("DELETE FROM ledger WHERE user_id LIKE 'user_test_%'")
        cur.execute("ALTER TABLE ledger ENABLE TRIGGER ledger_append_only_trigger")
    # 2. user_profile（KV 层）
    with get_cursor() as cur:
        cur.execute("DELETE FROM user_profile WHERE user_id LIKE 'user_test_%'")
    # 3. Redis 缓存
    for key in redis_client.scan_iter("kv:user_test_*"):
        redis_client.delete(key)
    # 4. 技能候选向量 + 技能快照（skill_candidate_store / skill_finalizer 测试会写，带 user_id）
    with get_cursor() as cur:
        cur.execute("DELETE FROM skill_candidate_vectors WHERE user_id LIKE 'user_test_%'")
        cur.execute("DELETE FROM skill_snapshots WHERE user_id LIKE 'user_test_%'")
    # 5. 向量语义（vector_semantic 测试会写 memory_embedding，带 user_id）
    with get_cursor() as cur:
        cur.execute("DELETE FROM memory_embedding WHERE user_id LIKE 'user_test_%'")
    # 6. 外部知识库测试行（rag_knowledge 测试用 source='test-*' 标识）
    with get_cursor() as cur:
        cur.execute("DELETE FROM rag_knowledge_base WHERE source LIKE 'test-%'")
    # 如果还有其他表（summary、vector、skills 等）也需清理，在这里补充

@pytest.fixture(autouse=True)
def clean_db():
    """每个测试前后自动清理测试用户数据，保证测试确定性、互不干扰"""
    _cleanup_test_data()
    yield
    _cleanup_test_data()
