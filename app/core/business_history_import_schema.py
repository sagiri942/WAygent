"""Frozen SQLite source inventory and MySQL import-only tables."""

from migrations_mysql.precision_baseline_20261006 import build_precision_metadata as build_business_import_metadata

SOURCE_TABLES = frozenset({
    "users", "user_profiles", "itineraries", "itinerary_enrichments",
    "optimizer_shadow_runs", "conversations", "messages", "planning_briefs",
    "runs", "run_events", "run_interaction_responses", "user_memory_states",
    "memory_facts", "conversation_memory_states", "memory_extraction_jobs",
})
IMPORT_TABLES = tuple(sorted(SOURCE_TABLES))
import_ledger = build_business_import_metadata().tables["business_import_ledger"]
