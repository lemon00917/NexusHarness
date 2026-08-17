"""Repository for template-binding master data and reviewed bindings."""

from __future__ import annotations

import re
import os
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from .config import TemplateBindingDatabaseConfig, load_database_config


class TemplateBindingRepositoryError(RuntimeError):
    """Raised when a repository operation fails."""


class TemplateBindingConflictError(TemplateBindingRepositoryError):
    """Raised when a reviewed binding conflicts with current database state."""


class TemplateBindingRepository:
    _TABLES = {
        "html_category": "doc_html_category",
        "html_template": "doc_html_template",
        "standard_category": "doc_standard_category",
        "standard_template": "doc_standard_template",
        "standard_node": "doc_standard_template_node",
        "template_mapping": "doc_template_mapping",
        "node_mapping": "doc_fhir_node_mapping",
    }

    def __init__(self, config: TemplateBindingDatabaseConfig | None = None) -> None:
        self.config = config or load_database_config()
        self._pool = None
        self._pool_lock = threading.Lock()

    def _qualified(self, key: str) -> str:
        return f'"{self.config.schema}"."{self._TABLES[key]}"'

    def _safe_error(self, exc: Exception) -> str:
        message = str(exc) or exc.__class__.__name__
        if self.config.password:
            message = message.replace(self.config.password, "***")
        return re.sub(r"(?i)(password\s*=\s*)[^\s]+", r"\1***", message)

    def _get_pool(self):
        if self._pool is None:
            with self._pool_lock:
                if self._pool is None:
                    try:
                        from psycopg2.pool import ThreadedConnectionPool

                        self._pool = ThreadedConnectionPool(
                            self.config.pool_min,
                            self.config.pool_max,
                            **self.config.connection_kwargs,
                        )
                    except Exception as exc:
                        raise TemplateBindingRepositoryError(
                            f"database pool initialization failed: {self._safe_error(exc)}"
                        ) from exc
        return self._pool

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        pool = self._get_pool()
        conn = None
        try:
            conn = pool.getconn()
            conn.set_session(readonly=True, autocommit=True)
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, false)",
                    (str(self.config.statement_timeout_seconds * 1000),),
                )
            yield conn
        except Exception as exc:
            raise TemplateBindingRepositoryError(self._safe_error(exc)) from exc
        finally:
            if conn is not None:
                pool.putconn(conn)

    @contextmanager
    def _write_connection(self) -> Iterator[Any]:
        pool = self._get_pool()
        conn = None
        try:
            conn = pool.getconn()
            conn.set_session(readonly=False, autocommit=False)
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(self.config.statement_timeout_seconds * 1000),),
                )
            yield conn
            conn.commit()
        except TemplateBindingConflictError:
            if conn is not None:
                conn.rollback()
            raise
        except Exception as exc:
            if conn is not None:
                conn.rollback()
            raise TemplateBindingRepositoryError(self._safe_error(exc)) from exc
        finally:
            if conn is not None:
                pool.putconn(conn)

    def close(self) -> None:
        with self._pool_lock:
            pool = self._pool
            self._pool = None
        if pool is not None:
            pool.closeall()

    def _fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        from psycopg2.extras import RealDictCursor

        with self._connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]

    def _fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = self._fetch_all(sql, params)
        return rows[0] if rows else None

    @staticmethod
    def _page(page: int, page_size: int) -> tuple[int, int]:
        safe_page = max(1, int(page))
        safe_size = min(200, max(1, int(page_size)))
        return safe_size, (safe_page - 1) * safe_size

    def test_connection(self) -> dict[str, Any]:
        row = self._fetch_one("SELECT current_database() AS database_name, current_schema() AS schema_name")
        return {
            "connected": True,
            "database": row.get("database_name") if row else self.config.database,
            "schema": self.config.schema,
            "host": self.config.host,
            "port": self.config.port,
            "read_only": False,
        }

    def list_html_categories(
        self, *, search: str = "", parent_id: str | None = None, page: int = 1, page_size: int = 50
    ) -> dict[str, Any]:
        limit, offset = self._page(page, page_size)
        where = ["1=1"]
        params: list[Any] = []
        if search:
            where.append("(category_name ILIKE %s OR category_id ILIKE %s)")
            params.extend([f"%{search}%", f"%{search}%"])
        if parent_id is not None:
            where.append("COALESCE(parent_id, '') = %s")
            params.append(parent_id)
        table = self._qualified("html_category")
        clause = " AND ".join(where)
        total = self._fetch_one(f"SELECT COUNT(*) AS total FROM {table} WHERE {clause}", tuple(params))
        rows = self._fetch_all(
            f"""SELECT category_id, category_name, version, parent_id, category_seq,
                       create_time, update_time
                FROM {table} WHERE {clause}
                ORDER BY category_seq NULLS LAST, category_name, category_id
                LIMIT %s OFFSET %s""",
            tuple([*params, limit, offset]),
        )
        return {"items": rows, "total": int((total or {}).get("total", 0)), "page": page, "page_size": limit}

    def list_html_templates(
        self, *, category_id: str | None = None, search: str = "", page: int = 1, page_size: int = 50
    ) -> dict[str, Any]:
        limit, offset = self._page(page, page_size)
        where = ["1=1"]
        params: list[Any] = []
        if category_id:
            where.append("t.print_template_category_id = %s")
            params.append(category_id)
        if search:
            where.append("(t.html_name ILIKE %s OR t.template_id ILIKE %s)")
            params.extend([f"%{search}%", f"%{search}%"])
        table = self._qualified("html_template")
        category_table = self._qualified("html_category")
        mapping_table = self._qualified("template_mapping")
        node_mapping_table = self._qualified("node_mapping")
        clause = " AND ".join(where)
        total = self._fetch_one(f"SELECT COUNT(*) AS total FROM {table} t WHERE {clause}", tuple(params))
        rows = self._fetch_all(
            f"""SELECT CAST(t.template_id AS TEXT) AS template_id,
                       CAST(t.print_template_category_id AS TEXT) AS print_template_category_id,
                       c.category_name,
                       t.html_name, t.xml_id, t.xml_version, t.is_show,
                       t.template_bdmcate_code, t.template_bdmcate_name,
                       t.create_time, t.update_time,
                       COALESCE(tm.template_mapping_count, 0) AS template_mapping_count,
                       COALESCE(nm.node_mapping_count, 0) AS node_mapping_count,
                       CASE WHEN t.html_info IS NULL THEN 0 ELSE LENGTH(t.html_info) END AS html_info_length
                FROM {table} t
                LEFT JOIN {category_table} c ON c.category_id = t.print_template_category_id
                LEFT JOIN (
                    SELECT html_id, COUNT(*) AS template_mapping_count
                    FROM {mapping_table} GROUP BY html_id
                ) tm ON tm.html_id = t.template_id
                LEFT JOIN (
                    SELECT html_template_id, COUNT(*) AS node_mapping_count
                    FROM {node_mapping_table} GROUP BY html_template_id
                ) nm ON nm.html_template_id = t.template_id
                WHERE {clause}
                ORDER BY c.category_name NULLS LAST, t.html_name, t.template_id
                LIMIT %s OFFSET %s""",
            tuple([*params, limit, offset]),
        )
        return {"items": rows, "total": int((total or {}).get("total", 0)), "page": page, "page_size": limit}

    def get_html_template(self, template_id: str, category_id: str, *, include_html: bool = False) -> dict[str, Any] | None:
        html_field = ", t.html_info" if include_html else ""
        return self._fetch_one(
            f"""SELECT CAST(t.template_id AS TEXT) AS template_id,
                       CAST(t.print_template_category_id AS TEXT) AS print_template_category_id,
                       c.category_name,
                       t.html_name, t.xml_id, t.print_template_id, t.xml_version,
                       t.is_show, t.instance_data_count, t.template_bdmcate_code,
                       t.template_bdmcate_name, t.create_time, t.update_time,
                       CASE WHEN t.html_info IS NULL THEN 0 ELSE LENGTH(t.html_info) END AS html_info_length
                       {html_field}
                FROM {self._qualified('html_template')} t
                LEFT JOIN {self._qualified('html_category')} c
                  ON c.category_id = t.print_template_category_id
                WHERE t.template_id = %s AND t.print_template_category_id = %s""",
            (template_id, category_id),
        )

    def list_html_template_variants(self, template_id: str) -> list[dict[str, Any]]:
        """Return every category-owned row sharing an HTML template ID."""
        return self._fetch_all(
            f"""SELECT CAST(t.template_id AS TEXT) AS template_id,
                       CAST(t.print_template_category_id AS TEXT) AS print_template_category_id,
                       c.category_name, t.html_name, t.html_info
                FROM {self._qualified('html_template')} t
                LEFT JOIN {self._qualified('html_category')} c
                  ON c.category_id = t.print_template_category_id
                WHERE t.template_id = %s
                ORDER BY t.print_template_category_id""",
            (template_id,),
        )

    def list_standard_categories(self, *, search: str = "", page: int = 1, page_size: int = 100) -> dict[str, Any]:
        limit, offset = self._page(page, page_size)
        params: list[Any] = ["3"]
        where = ["type = %s"]
        if search:
            where.append("(category_name ILIKE %s OR category_id ILIKE %s)")
            params.extend([f"%{search}%", f"%{search}%"])
        table = self._qualified("standard_category")
        clause = " AND ".join(where)
        total = self._fetch_one(f"SELECT COUNT(*) AS total FROM {table} WHERE {clause}", tuple(params))
        rows = self._fetch_all(
            f"""SELECT category_id, category_name, type, cda_type_code, status,
                       create_time, update_time
                FROM {table} WHERE {clause}
                ORDER BY category_name, category_id LIMIT %s OFFSET %s""",
            tuple([*params, limit, offset]),
        )
        return {"items": rows, "total": int((total or {}).get("total", 0)), "page": page, "page_size": limit}

    def list_standard_templates(
        self, *, category_id: str | None = None, search: str = "", page: int = 1, page_size: int = 100
    ) -> dict[str, Any]:
        limit, offset = self._page(page, page_size)
        where = ["c.type = %s", "t.status = %s"]
        params: list[Any] = ["3", 1]
        if category_id:
            where.append("t.category_id = %s")
            params.append(category_id)
        if search:
            where.append("(t.name ILIKE %s OR CAST(t.id AS TEXT) ILIKE %s OR c.category_name ILIKE %s)")
            params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
        table = self._qualified("standard_template")
        category_table = self._qualified("standard_category")
        mapping_table = self._qualified("template_mapping")
        node_table = self._qualified("standard_node")
        clause = " AND ".join(where)
        total = self._fetch_one(
            f"SELECT COUNT(*) AS total FROM {table} t JOIN {category_table} c ON c.category_id=t.category_id WHERE {clause}",
            tuple(params),
        )
        rows = self._fetch_all(
            f"""SELECT CAST(t.id AS TEXT) AS id, t.name, t.desc, t.category_id, c.category_name,
                       c.type AS category_type,
                       t.status, t.create_time, t.update_time,
                       COALESCE(sn.node_count, 0) AS node_count,
                       COALESCE(tm.mapped_html_count, 0) AS mapped_html_count
                 FROM {table} t JOIN {category_table} c ON c.category_id=t.category_id
                 LEFT JOIN (
                     SELECT CAST(standard_xml_id AS TEXT) AS standard_xml_id, COUNT(*) AS node_count
                     FROM {node_table} GROUP BY standard_xml_id
                 ) sn ON sn.standard_xml_id = CAST(t.id AS TEXT)
                 LEFT JOIN (
                    SELECT standard_xml_id, COUNT(*) AS mapped_html_count
                    FROM {mapping_table} GROUP BY standard_xml_id
                ) tm ON tm.standard_xml_id = t.id
                WHERE {clause}
                ORDER BY c.category_name, t.name, t.id LIMIT %s OFFSET %s""",
            tuple([*params, limit, offset]),
        )
        return {"items": rows, "total": int((total or {}).get("total", 0)), "page": page, "page_size": limit}

    def get_standard_template(self, template_id: str) -> dict[str, Any] | None:
        return self._fetch_one(
            f"""SELECT CAST(t.id AS TEXT) AS id, t.name, t.desc, t.category_id, c.category_name,
                       c.type AS category_type, t.status, t.create_time, t.update_time,
                       COALESCE(sn.node_count, 0) AS node_count
                 FROM {self._qualified('standard_template')} t
                 JOIN {self._qualified('standard_category')} c ON c.category_id=t.category_id
                 LEFT JOIN (
                     SELECT CAST(standard_xml_id AS TEXT) AS standard_xml_id, COUNT(*) AS node_count
                     FROM {self._qualified('standard_node')} GROUP BY standard_xml_id
                 ) sn ON sn.standard_xml_id = CAST(t.id AS TEXT)
                 WHERE t.id = %s AND c.type = %s AND t.status = %s""",
            (template_id, "3", 1),
        )

    def list_standard_nodes(self, template_id: str) -> list[dict[str, Any]]:
        return self._fetch_all(
            f"""SELECT CAST(id AS TEXT) AS id,
                       CAST(standard_xml_id AS TEXT) AS standard_xml_id,
                       node_en, node_cn, node_attr, node_value,
                       mapping_value, CAST(pid AS TEXT) AS pid,
                       CAST(pid_new AS TEXT) AS pid_new,
                       seq_no, show_status, node_remark,
                       create_time, update_time
                FROM {self._qualified('standard_node')}
                WHERE standard_xml_id = %s
                ORDER BY seq_no NULLS LAST, id""",
            (str(template_id),),
        )

    def get_existing_mappings(self, html_template_id: str) -> dict[str, Any]:
        standard_table = self._qualified("standard_template")
        template_rows = self._fetch_all(
            f"""SELECT CAST(m.mapping_id AS TEXT) AS mapping_id,
                       CAST(m.standard_xml_id AS TEXT) AS standard_xml_id,
                       m.standard_category_id, m.standard_xml_name,
                       CAST(m.html_id AS TEXT) AS html_id, m.html_name, m.html_version,
                       m.mapping_state, m.switch_state, m.create_time, m.update_time,
                       t.status AS standard_template_status
                 FROM {self._qualified('template_mapping')} m
                 LEFT JOIN {standard_table} t
                   ON CAST(t.id AS TEXT) = CAST(m.standard_xml_id AS TEXT)
                 WHERE m.html_id = %s
                 ORDER BY m.update_time DESC NULLS LAST, m.create_time DESC NULLS LAST""",
            (html_template_id,),
        )
        node_rows = self._fetch_all(
            f"""SELECT CAST(id AS TEXT) AS id, standard_category_id,
                       CAST(standard_template_id AS TEXT) AS standard_template_id,
                       CAST(standard_node_id AS TEXT) AS standard_node_id,
                       CAST(html_template_id AS TEXT) AS html_template_id,
                       html_node_code, CAST(html_node_id AS TEXT) AS html_node_id,
                       mapping_values, mapping_type,
                       create_time, update_time
                FROM {self._qualified('node_mapping')}
                WHERE html_template_id = %s
                ORDER BY standard_template_id, standard_node_id, id""",
            (html_template_id,),
        )
        node_counts: dict[str, int] = {}
        for row in node_rows:
            standard_template_id = str(row.get("standard_template_id") or "")
            if standard_template_id:
                node_counts[standard_template_id] = node_counts.get(standard_template_id, 0) + 1
        for row in template_rows:
            standard_template_id = str(row.get("standard_xml_id") or "")
            row["node_mapping_count"] = node_counts.get(standard_template_id, 0)
        return {
            "html_template_id": html_template_id,
            "template_mappings": template_rows,
            "node_mappings": node_rows,
            "template_mapping_count": len(template_rows),
            "node_mapping_count": len(node_rows),
        }

    def save_reviewed_bindings(
        self,
        *,
        mapping_id: str,
        html_template_id: str,
        html_category_id: str,
        standard_template_id: str,
        expected_update_time: str | None,
        node_mappings: list[dict[str, Any]],
        id_provider: Any,
    ) -> dict[str, Any]:
        """Persist reviewed mappings atomically using PATCH semantics."""
        from psycopg2.extras import RealDictCursor

        html_table = self._qualified("html_template")
        standard_table = self._qualified("standard_template")
        category_table = self._qualified("standard_category")
        standard_node_table = self._qualified("standard_node")
        template_mapping_table = self._qualified("template_mapping")
        node_mapping_table = self._qualified("node_mapping")

        with self._write_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cursor:
            mapping_state = os.getenv("TEMPLATE_BINDING_MAPPING_STATE", "0")
            switch_state = os.getenv("TEMPLATE_BINDING_SWITCH_STATE", "0")
            audit_user = os.getenv("TEMPLATE_BINDING_AUDIT_USER", "template-binding")
            if not audit_user.strip():
                raise TemplateBindingConflictError("TEMPLATE_BINDING_AUDIT_USER cannot be empty")
            cursor.execute(
                f"""SELECT CAST(template_id AS TEXT) AS template_id,
                           CAST(print_template_category_id AS TEXT) AS category_id,
                           html_name, xml_version
                    FROM {html_table}
                    WHERE template_id=%s AND print_template_category_id=%s
                    FOR UPDATE""",
                (html_template_id, html_category_id),
            )
            html_template = cursor.fetchone()
            if html_template is None:
                raise TemplateBindingConflictError("HTML template no longer exists for the composite key")

            cursor.execute(
                f"""SELECT CAST(t.id AS TEXT) AS id, t.name, t.category_id, c.category_name, t.status
                    FROM {standard_table} t
                    JOIN {category_table} c ON c.category_id=t.category_id
                    WHERE t.id=%s AND c.type=%s AND t.status=%s
                    FOR SHARE OF t""",
                (standard_template_id, "3", 1),
            )
            standard_template = cursor.fetchone()
            if standard_template is None:
                raise TemplateBindingConflictError(
                    "standard template no longer exists or is not a clinical document template"
                )

            standard_node_ids = [str(item["standard_node_id"]) for item in node_mappings]
            if standard_node_ids:
                cursor.execute(
                    f"""SELECT CAST(id AS TEXT) AS id
                        FROM {standard_node_table}
                        WHERE standard_xml_id=%s AND CAST(id AS TEXT)=ANY(%s)
                        FOR SHARE""",
                    (standard_template_id, standard_node_ids),
                )
                found_node_ids = {str(row["id"]) for row in cursor.fetchall()}
                missing = sorted(set(standard_node_ids) - found_node_ids)
                if missing:
                    raise TemplateBindingConflictError(
                        "standard nodes no longer belong to the selected template: " + ", ".join(missing)
                    )

            cursor.execute(
                f"""SELECT CAST(m.mapping_id AS TEXT) AS mapping_id,
                           CAST(m.standard_xml_id AS TEXT) AS standard_xml_id,
                           t.status AS standard_template_status,
                           CAST(m.update_time AS TEXT) AS update_time,
                           CAST(m.create_time AS TEXT) AS create_time
                    FROM {template_mapping_table} m
                    LEFT JOIN {standard_table} t
                      ON CAST(t.id AS TEXT) = CAST(m.standard_xml_id AS TEXT)
                    WHERE m.html_id=%s
                    FOR UPDATE OF m""",
                (html_template_id,),
            )
            existing_templates = [dict(row) for row in cursor.fetchall()]
            if len(existing_templates) > 1:
                raise TemplateBindingConflictError(
                    "multiple template mappings already exist for this HTML template; manual cleanup is required"
                )
            existing_template = existing_templates[0] if existing_templates else None
            inactive_existing = bool(
                existing_template
                and str(existing_template.get("standard_xml_id") or "") != standard_template_id
                and str(existing_template.get("standard_template_status") or "").strip() not in {"", "1"}
            )
            if (
                existing_template
                and str(existing_template["standard_xml_id"]) != standard_template_id
                and not inactive_existing
            ):
                raise TemplateBindingConflictError(
                    "HTML template is already bound to a different standard template"
                )
            if existing_template and expected_update_time:
                actual = str(existing_template.get("update_time") or existing_template.get("create_time") or "")
                if self._timestamp_token(actual) != self._timestamp_token(expected_update_time):
                    raise TemplateBindingConflictError(
                        "template mapping was modified by another request; reload before saving"
                    )

            template_created = existing_template is None
            if template_created:
                standard_name = "-".join(
                    item for item in (standard_template.get("category_name"), standard_template.get("name")) if item
                )
                cursor.execute(
                    f"""INSERT INTO {template_mapping_table}
                        (mapping_id, standard_xml_id, standard_category_id, standard_xml_name,
                         html_id, html_name, html_version, mapping_state, switch_state,
                         create_by, create_time, update_by, update_time)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, CURRENT_TIMESTAMP, %s, CURRENT_TIMESTAMP)""",
                    (
                        mapping_id,
                        standard_template_id,
                        standard_template["category_id"],
                        standard_name,
                        html_template_id,
                        html_template.get("html_name"),
                        html_template.get("xml_version"),
                        mapping_state,
                        switch_state,
                        audit_user,
                        audit_user,
                    ),
                )
            elif inactive_existing:
                old_standard_template_id = str(existing_template["standard_xml_id"])
                mapping_id = str(existing_template["mapping_id"])
                standard_name = "-".join(
                    item for item in (standard_template.get("category_name"), standard_template.get("name")) if item
                )
                cursor.execute(
                    f"""UPDATE {template_mapping_table}
                        SET standard_xml_id=%s, standard_category_id=%s, standard_xml_name=%s,
                            html_name=%s, html_version=%s, update_by=%s, update_time=CURRENT_TIMESTAMP
                        WHERE mapping_id=%s""",
                    (
                        standard_template_id,
                        standard_template["category_id"],
                        standard_name,
                        html_template.get("html_name"),
                        html_template.get("xml_version"),
                        audit_user,
                        existing_template["mapping_id"],
                    ),
                )
                cursor.execute(
                    f"""DELETE FROM {node_mapping_table}
                        WHERE html_template_id=%s AND standard_template_id=%s""",
                    (html_template_id, old_standard_template_id),
                )
            else:
                mapping_id = str(existing_template["mapping_id"])

            cursor.execute(
                f"""SELECT CAST(id AS TEXT) AS id, CAST(standard_node_id AS TEXT) AS standard_node_id,
                           html_node_code, CAST(html_node_id AS TEXT) AS html_node_id, mapping_values
                    FROM {node_mapping_table}
                    WHERE html_template_id=%s AND standard_template_id=%s
                    FOR UPDATE""",
                (html_template_id, standard_template_id),
            )
            existing_nodes: dict[str, dict[str, Any]] = {}
            for row in cursor.fetchall():
                standard_node_id = str(row["standard_node_id"])
                if standard_node_id in existing_nodes:
                    raise TemplateBindingConflictError(
                        f"duplicate node mappings already exist for standard node {standard_node_id}"
                    )
                existing_nodes[standard_node_id] = dict(row)

            inserted = 0
            updated = 0
            unchanged = 0
            for item in node_mappings:
                standard_node_id = str(item["standard_node_id"])
                values = (
                    str(item.get("html_node_code") or ""),
                    str(item.get("html_node_id") or ""),
                    str(item.get("mapping_values") or ""),
                )
                existing = existing_nodes.get(standard_node_id)
                if existing:
                    current = (
                        str(existing.get("html_node_code") or ""),
                        str(existing.get("html_node_id") or ""),
                        str(existing.get("mapping_values") or ""),
                    )
                    if current == values:
                        unchanged += 1
                        continue
                    cursor.execute(
                        f"""UPDATE {node_mapping_table}
                            SET html_node_code=%s, html_node_id=%s, mapping_values=%s,
                                update_by=%s, update_time=CURRENT_TIMESTAMP
                            WHERE id=%s""",
                        (*values, audit_user, existing["id"]),
                    )
                    updated += 1
                    continue
                cursor.execute(
                    f"""INSERT INTO {node_mapping_table}
                        (id, standard_category_id, standard_template_id, standard_node_id,
                         html_template_id, html_node_code, html_node_id, mapping_values,
                         create_by, create_time, update_by, update_time)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                                %s, CURRENT_TIMESTAMP, %s, CURRENT_TIMESTAMP)""",
                    (
                        int(id_provider.next_id()),
                        standard_template["category_id"],
                        standard_template_id,
                        standard_node_id,
                        html_template_id,
                        *values,
                        audit_user,
                        audit_user,
                    ),
                )
                inserted += 1

            return {
                "mapping_id": mapping_id,
                "template_created": template_created,
                "node_inserted": inserted,
                "node_updated": updated,
                "node_unchanged": unchanged,
                "node_submitted": len(node_mappings),
            }

    @staticmethod
    def _timestamp_token(value: object) -> str:
        return str(value or "").strip().replace("T", " ").replace("Z", "+00:00")
