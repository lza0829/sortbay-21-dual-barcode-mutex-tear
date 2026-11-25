import os
import tempfile

import pytest

# 必须在导入 app 之前指定独立临时库，避免污染开发库 data/sortbay.db
_TMP = tempfile.TemporaryDirectory()
os.environ["SORTBAY_DB"] = os.path.join(_TMP.name, "test_sortbay.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.db import connect  # noqa: E402


class ApiClient:
    """所有路由挂在 /api 前缀下，统一在此补前缀，用例保持业务路径可读。"""

    def __init__(self, raw: TestClient):
        self._raw = raw

    def get(self, path, params=None):
        return self._raw.get("/api" + path, params=params)

    def post(self, path, json=None, params=None):
        return self._raw.post("/api" + path, json=json, params=params)


@pytest.fixture()
def client():
    # 每个用例重建库，保证互相隔离
    path = os.environ["SORTBAY_DB"]
    if os.path.exists(path):
        os.remove(path)
    for suffix in ("-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            os.remove(p)
    from app.seed import seed_if_empty
    seed_if_empty()
    # 清掉种子里的事务数据（保留分拣线/口门/路由表/班次），
    # 让桶计数、扫描册行数等断言从确定的零点开始。
    from app.db import db as _db
    with _db() as conn:
        conn.execute("DELETE FROM route_events")
        conn.execute("DELETE FROM scans")
        conn.execute("DELETE FROM audit_events")
        conn.execute("DELETE FROM bags")
        conn.execute("DELETE FROM divert_batches")
    with TestClient(app) as c:
        yield ApiClient(c)


@pytest.fixture()
def raw_conn():
    """绕过 HTTP 的裸连接，用于并发夹具与库级断言。"""
    conn = connect()
    yield conn
    conn.close()
