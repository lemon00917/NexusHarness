"""
Database module for NexusHarness medical records.
Supports IRIS (REST API) and MySQL.
"""
from microharness.database.db_client import DatabaseClient, get_db
from microharness.database.field_mapper import map_bindings_to_row, get_table_for_doc
