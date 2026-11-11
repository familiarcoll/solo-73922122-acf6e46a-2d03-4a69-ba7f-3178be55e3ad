"""pytest 公共夹具：隔离的临时 SQLite、关闭进程内 worker。"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

# 必须在导入应用配置前设置环境变量。
_TMP = tempfile.mkdtemp(prefix="delivery-tests-")
os.environ["DELIVERY_DB_PATH"] = str(Path(_TMP) / "delivery.db")
os.environ["LIMS_DB_PATH"] = str(Path(_TMP) / "lims.db")
os.environ["ENABLE_INPROCESS_WORKER"] = "false"
os.environ["LIMS_BASE_URL"] = "http://lims-test"
os.environ["LEASE_SECONDS"] = "2"
os.environ["POLL_INTERVAL_SECONDS"] = "0.05"
os.environ["BACKOFF_BASE_SECONDS"] = "0.01"
os.environ["LIMS_TIMEOUT_SECONDS"] = "2"

from app import lims as lims_package  # noqa: E402
from app.delivery import config as delivery_config  # noqa: E402
from app.delivery import db as delivery_db  # noqa: E402
from app.lims import db as lims_db  # noqa: E402


@pytest.fixture()
def delivery_db_path(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "delivery.db"
    delivery_db.initialize_db(path)
    yield path


@pytest.fixture()
def lims_db_path(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "lims.db"
    lims_db.initialize_db(path)
    yield path


@pytest.fixture(autouse=True)
def _isolate_default_dbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """让默认路径（未显式传 db_path 的代码）也指向每个测试独立的临时目录。"""
    delivery_default = tmp_path / "default-delivery.db"
    monkeypatch.setattr(delivery_config, "DB_PATH", delivery_default)
    delivery_db.initialize_db(delivery_default)

    lims_default = tmp_path / "default-lims.db"
    monkeypatch.setattr(lims_package, "LIMS_DB_PATH", lims_default, raising=False)
    monkeypatch.setattr(lims_db, "LIMS_DB_PATH", lims_default, raising=False)
    lims_db.initialize_db(lims_default)
