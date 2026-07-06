"""
Database Client — supports IRIS (REST API) and MySQL.
No ODBC/JDBC drivers needed for IRIS.
"""

import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent.parent / "configs" / "database.json"


def _sql_debug_enabled() -> bool:
    return str(os.environ.get("MEDICAL_QUERY_DEBUG", "")).lower() in {"1", "true", "yes", "on"}


def _sql_debug(sql: str, limit: int = 500) -> None:
    if _sql_debug_enabled():
        print(f"[SQL][debug] {sql[:limit]}", flush=True)


def load_config() -> dict:
    if not _CONFIG_PATH.exists():
        return {"type": "iris"}
    return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))


class IrisClient:
    """IRIS database via REST API."""

    def __init__(self, base_url: str, namespace: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.namespace = namespace
        self.auth = (username, password)

    def execute(self, sql: str) -> dict:
        """Execute SQL and return results."""
        import requests
        _sql_debug(sql, 400)
        url = f"{self.base_url}/api/atelier/v1/{self.namespace}/action/query"
        resp = requests.post(url, json={"query": sql}, auth=self.auth, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status", {}).get("errors"):
            raise Exception(f"SQL Error: {data['status']['errors']}")
        return data.get("result", {}).get("content", [])

    def insert(self, table: str, row: dict):
        """Insert a row into a table. Returns (ok: bool, error_msg: str)."""
        columns = list(row.keys())
        values = []
        for col in columns:
            val = row.get(col)
            if val is None or val == "":
                values.append("''")
            elif isinstance(val, (int, float)):
                values.append(str(val))
            else:
                s = str(val) if val is not None else ""
                escaped = s.replace("'", "''").replace("\n", " ").replace("\r", "")
                values.append(f"'{escaped}'")

        sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(values)})"
        _sql_debug(sql, 500)
        try:
            self.execute(sql)
            logger.info(f"INSERT {table}: OK")
            return True, ""
        except Exception as e:
            logger.error(f"INSERT failed: {e}")
            return False, ""

    def upsert(self, table: str, row: dict, key_col: str = "emr_hosdocid"):
        """Insert or update using IRIS native INSERT OR UPDATE. Returns (ok, error_msg)."""
        columns = list(row.keys())
        values = []
        for col in columns:
            val = row.get(col)
            if val is None or val == "":
                values.append("''")
            elif isinstance(val, (int, float)):
                values.append(str(val))
            else:
                s = str(val) if val is not None else ""
                escaped = s.replace("'", "''").replace("\n", " ").replace("\r", "")
                values.append(f"'{escaped}'")
        sql = f"INSERT OR UPDATE {table} ({', '.join(columns)}) VALUES ({', '.join(values)})"
        _sql_debug(sql, 500)
        try:
            self.execute(sql)
            logger.info(f"UPSERT {table}: OK")
            return True, ""
        except Exception as e:
            logger.error(f"UPSERT failed: {e}")
            return False, str(e)[:200]

    def test(self) -> bool:
        try:
            result = self.execute("SELECT 1 as test")
            return len(result) > 0
        except Exception as e:
            logger.debug(f"Database test failed: {e}")
            return False


class MySQLClient:
    """MySQL database via pymysql."""

    def __init__(self, host: str, port: int, database: str, user: str, password: str):
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        self._conn = None
        self._local = threading.local()

    def _connect(self):
        import pymysql
        return pymysql.connect(
            host=self.host,
            port=self.port,
            database=self.database,
            user=self.user,
            password=self.password,
            charset="utf8mb4",
            autocommit=False,
            connect_timeout=10,
            read_timeout=60,
            write_timeout=60,
        )

    @property
    def conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        else:
            try:
                conn.ping(reconnect=True)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                conn = self._connect()
                self._local.conn = conn
        return conn

    def _reset_thread_conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    def execute(self, sql: str) -> list:
        import pymysql
        _sql_debug(sql, 400)
        try:
            with self.conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(sql)
                return cur.fetchall()
        except Exception:
            self._reset_thread_conn()
            raise

    def insert(self, table: str, row: dict):
        """Insert a row. Returns (ok, error_msg)."""
        try:
            columns = list(row.keys())
            placeholders = ", ".join(["%s"] * len(columns))
            values = [row[c] for c in columns]
            sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
            with self.conn.cursor() as cur:
                cur.execute(sql, values)
            self.conn.commit()
            return True, ""
        except Exception as e:
            self._reset_thread_conn()
            logger.error(f"MySQL INSERT failed: {e}")
            return False, str(e)[:200]

    def upsert(self, table: str, row: dict, key_col: str = "emr_hosdocid"):
        """Insert or update. Returns (ok, error_msg)."""
        key_val = row.get(key_col)
        if not key_val and key_val != 0:
            return False, f"Missing key column: {key_col}"
        try:
            columns = list(row.keys())
            placeholders = ", ".join(["%s"] * len(columns))
            updates = ", ".join(f"{c}=VALUES({c})" for c in columns if c != key_col)
            sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {updates}"
            with self.conn.cursor() as cur:
                cur.execute(sql, [row[c] for c in columns])
            self.conn.commit()
            return True, ""
        except Exception as e:
            self._reset_thread_conn()
            logger.error(f"MySQL UPSERT failed: {e}")
            return False, str(e)[:200]

    def test(self) -> bool:
        try:
            self.execute("SELECT 1")
            return True
        except Exception as e:
            logger.debug(f"Database test failed: {e}")
            return False


class DatabaseClient:
    """Unified database interface."""

    def __init__(self, config: dict = None):
        if config is None:
            config = load_config()
        self.config = config
        self._client = None

    @property
    def client(self):
        if self._client is None:
            db_type = self.config.get("type", "iris")
            if db_type == "iris":
                cfg = self.config.get("iris", {})
                self._client = IrisClient(
                    base_url=cfg.get("base_url", ""),
                    namespace=cfg.get("namespace", "HDCV2DEV"),
                    username=cfg.get("username", "_system"),
                    password=cfg.get("password", ""),
                )
            else:
                cfg = self.config.get("mysql", {})
                self._client = MySQLClient(
                    host=cfg.get("host", "127.0.0.1"),
                    port=cfg.get("port", 3306),
                    database=cfg.get("database", ""),
                    user=cfg.get("user", "root"),
                    password=cfg.get("password", ""),
                )
        return self._client

    def insert(self, table: str, row: dict):
        return self.client.upsert(table, row)

    def test(self) -> bool:
        return self.client.test()


# Global instance
_db: Optional[DatabaseClient] = None


def get_db() -> DatabaseClient:
    global _db
    if _db is None:
        _db = DatabaseClient()
    return _db
