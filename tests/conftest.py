"""测试根 conftest：只负责给依赖外部服务的用例打 external 标记。

**不再在此处设置 MYSQL_URL**（2026-09-22 改）。原先用 os.environ.setdefault
把 DB 指向集成测试专用 schema，而 setdefault **只在未预置 MYSQL_URL 时生效**：
本仓 .env 的 MYSQL_URL 指向真实库 smart_procurement，调用方一旦 source .env
（原 scripts/run_e2e.sh 就是这么干的），守卫即失效，integration 的
_reset_state 会 TRUNCATE 演示主库。

路由权已下放到各子目录 conftest：integration 强制指向 *_test 库并断言库名后缀，
不允许被外部环境改写。单元测试用 mock 不真连库，不设亦无副作用。

（注：e2e/conftest.py 的连库参数走 MYSQL_PORT + MYSQL_ROOT_PASSWORD，**不读
MYSQL_URL**，故本次改动与 E2E 无关；该文件里那个名为 MYSQL_DSN 的变量是死变量，
全仓无读取方。）
"""

from __future__ import annotations

import pytest



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
