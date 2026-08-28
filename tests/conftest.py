"""测试根 conftest：在导入任何 app 模块前把 DB 指向集成测试专用 schema。

app/core/database.py 的 engine 是模块级单例，连接串在 import 时锁定。统一
指向 smart_procurement_test（P7.3 集成测试），确保测试进程绝不触碰演示主库
smart_procurement。单元测试用 mock 不真连库，指向 test schema 无副作用。

与 tests/integration/conftest.py 保持同源（root 账号密码来自 .env）。
"""

from __future__ import annotations

import os

import pytest

# 外部依赖地址参数化：本地默认指向共享 infra（宿主 33061），CI 用 GitHub Actions
# services 时经 env 覆盖（MYSQL_HOST=localhost / MYSQL_PORT=3306，或直接设 MYSQL_URL
# 全串）。e2e 的宿主端口重映射（13306）也在 e2e/conftest 单独参数化，互不干扰。
_MYSQL_USER = os.environ.get("MYSQL_USER", "smart")
_MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "smart_procurement_dev")
_MYSQL_HOST = os.environ.get("MYSQL_HOST", "localhost")
_MYSQL_PORT = os.environ.get("MYSQL_PORT", "33061")
TEST_DATABASE = os.environ.get("MYSQL_DATABASE", "smart_procurement_test")

os.environ.setdefault(
    "MYSQL_URL",
    f"mysql+asyncmy://{_MYSQL_USER}:{_MYSQL_PASSWORD}@{_MYSQL_HOST}:{_MYSQL_PORT}/{TEST_DATABASE}",
)
os.environ.setdefault("MYSQL_DATABASE", TEST_DATABASE)


def pytest_collection_modifyitems(items):
    """集成/E2E 测试自动标记 external（依赖真实外部服务）。

    CI 门禁 L1 用 `pytest tests/unit -m "not external"` 过滤出离线 unit 子集；
    integration/e2e 走真实 MySQL/Neo4j/Milvus/MinIO/Redis，统一标 external 让
    `-m "not external"` 语义完整（L1 跳过、L2 全量执行），并防御将来 CI 改为
    整目录扫描时误把依赖外部服务的测试混入 L1 门禁。
    """
    for item in items:
        p = item.path.as_posix()
        if "/integration/" in p or "/e2e/" in p:
            item.add_marker(pytest.mark.external)
