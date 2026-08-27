"""测试根 conftest：在导入任何 app 模块前把 DB 指向集成测试专用 schema。

app/core/database.py 的 engine 是模块级单例，连接串在 import 时锁定。统一
指向 smart_procurement_test（P7.3 集成测试），确保测试进程绝不触碰演示主库
smart_procurement。单元测试用 mock 不真连库，指向 test schema 无副作用。

与 tests/integration/conftest.py 保持同源（root 账号密码来自 .env）。
"""

from __future__ import annotations

import os

_MYSQL_USER = "smart"
_MYSQL_PASSWORD = "smart_procurement_dev"
_MYSQL_HOST = "localhost"
_MYSQL_PORT = "33061"
TEST_DATABASE = "smart_procurement_test"

os.environ.setdefault(
    "MYSQL_URL",
    f"mysql+asyncmy://{_MYSQL_USER}:{_MYSQL_PASSWORD}@{_MYSQL_HOST}:{_MYSQL_PORT}/{TEST_DATABASE}",
)
os.environ.setdefault("MYSQL_DATABASE", TEST_DATABASE)
