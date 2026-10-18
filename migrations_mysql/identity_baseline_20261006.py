"""Frozen Ticket03 MySQL identity/profile expansion; later tickets add revisions."""
from sqlalchemy import (MetaData, Table, Column, String, Text, Integer, Float,
                        ForeignKey, UniqueConstraint, CheckConstraint)
from sqlalchemy.dialects.mysql import LONGTEXT, BINARY

def build_metadata():
    metadata = MetaData()
    _options = dict(mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_0900_bin")
    def c(name, kind=Text, **kw):
        return Column(name, kind, **kw)
    users = Table("users", metadata,
        c("id", String(191), primary_key=True), c("username", LONGTEXT, nullable=False),
        c("username_digest", BINARY(32), nullable=False),
        c("password_hash", Text, nullable=False), c("created_at", String(40), nullable=False),
        UniqueConstraint("username_digest", name="uq_users_username_digest"), **_options)
    # Complete current itinerary row shape supports profile count; itinerary APIs
    # themselves remain Ticket06. No create_all or application auto-initialization.
    itineraries = Table("itineraries", metadata,
        c("id", String(191), primary_key=True),
        Column("user_id", String(191), ForeignKey("users.id"), nullable=False),
        c("parent_id", String(191)), c("root_id", String(191)),
        c("version", Integer, nullable=False, server_default="1"), c("query", LONGTEXT, nullable=False),
        c("modification_notes", LONGTEXT), c("destination", Text),
        c("start_date", String(40)), c("end_date", String(40)),
        c("plan_json", LONGTEXT, nullable=False), c("planner_state_json", LONGTEXT),
        c("lock_version", Integer, nullable=False, server_default="1"),
        c("created_at", String(40), nullable=False),
        CheckConstraint("lock_version >= 1", name="ck_itinerary_lock_version"), **_options)
    user_memory_states = Table("user_memory_states", metadata,
        Column("user_id", String(191), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        c("revision", Integer, nullable=False, server_default="0"),
        c("updated_at", String(40), nullable=False), **_options)
    # Conversation rows/foreign key are supplied in Ticket04/05 before import.
    # source_conversation_id remains nullable provenance, not an invented object.
    memory_facts = Table("memory_facts", metadata,
        c("id", String(191), primary_key=True),
        Column("user_id", String(191), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        c("category", String(64), nullable=False), c("value_text", Text, nullable=False),
        c("normalized_value", Text, nullable=False), c("polarity", String(16), nullable=False),
        c("scope_type", String(32), nullable=False), c("scope_key", LONGTEXT, nullable=False),
        c("status", String(16), nullable=False), c("source_kind", String(32), nullable=False),
        c("sensitivity", String(16), nullable=False, server_default="normal"),
        c("source_conversation_id", String(191)), c("evidence_sequences_json", Text, nullable=False),
        c("confidence", Float, nullable=False, server_default="1"),
        Column("supersedes_id", String(191), ForeignKey("memory_facts.id")),
        c("fingerprint", String(64), nullable=False), c("created_at", String(40), nullable=False),
        c("updated_at", String(40), nullable=False), c("deleted_at", String(40)),
        UniqueConstraint("user_id", "fingerprint", name="uq_memory_fingerprint"),
        CheckConstraint("category IN ('attraction_preference','food_preference','dietary_requirement','travel_pace','budget_style','transport_preference','accommodation_preference','schedule_preference','companion_context','accessibility_need','destination_history','other_travel_preference')", name="ck_memory_category"),
        CheckConstraint("polarity IN ('prefer','avoid','require','fact')", name="ck_memory_polarity"),
        CheckConstraint("scope_type IN ('global','destination','companion','destination_companion')", name="ck_memory_scope"),
        CheckConstraint("status IN ('active','candidate','superseded','deleted')", name="ck_memory_status"),
        CheckConstraint("source_kind IN ('explicit_chat','inferred_chat','manual','legacy')", name="ck_memory_source"),
        CheckConstraint("sensitivity IN ('normal','protected')", name="ck_memory_sensitivity"), **_options)
    return metadata
