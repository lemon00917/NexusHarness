"""
External Services Module
========================
Integrates external medical APIs into the query pipeline
alongside DB-based medical records.
"""
from microharness.services.service_catalog import load_services, match_services
from microharness.services.http_client import call_service, call_service_as_binding
