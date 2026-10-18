"""Ticket05's additive MySQL Runtime schema; revisions 01 and 02 stay frozen."""
from sqlalchemy import (
    CheckConstraint, Column, ForeignKey, Index, Integer, String,
    Table, Text, UniqueConstraint,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from migrations_mysql.memory_baseline_20261006 import build_memory_metadata


def build_runtime_metadata():
    metadata = build_memory_metadata()
    options = dict(mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_0900_bin")
    conversations = metadata.tables["conversations"]
    users = metadata.tables["users"]
    itineraries = metadata.tables["itineraries"]
    messages = Table(
        "messages", metadata,
        Column("id", String(191), primary_key=True),
        Column("conversation_id", String(191), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        Column("user_id", String(191), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        Column("role", String(16), nullable=False), Column("content", LONGTEXT, nullable=False),
        Column("artifacts_json", LONGTEXT, nullable=False), Column("sequence", Integer, nullable=False),
        Column("related_run_id", String(191)), Column("related_itinerary_id", String(191)),
        Column("created_at", String(40), nullable=False),
        UniqueConstraint("conversation_id", "sequence", name="uq_messages_conversation_sequence"),
        CheckConstraint("role IN ('user','assistant','system')", name="ck_message_role"),
        **options,
    )
    Index("idx_messages_conversation_sequence", messages.c.conversation_id, messages.c.sequence)
    briefs = Table(
        "planning_briefs", metadata,
        Column("id", String(191), primary_key=True),
        Column("conversation_id", String(191), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        Column("user_id", String(191), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        Column("status", String(16), nullable=False), Column("data_json", LONGTEXT, nullable=False),
        Column("missing_fields_json", LONGTEXT, nullable=False), Column("submission_snapshot_json", LONGTEXT),
        Column("submitted_run_id", String(191)), Column("memory_projection_json", LONGTEXT),
        Column("memory_match_status", String(16), nullable=False, server_default="none"),
        Column("memory_match_error_code", String(128)), Column("memory_context_fingerprint", String(191)),
        Column("memory_matched_at", String(40)), Column("created_at", String(40), nullable=False),
        Column("updated_at", String(40), nullable=False), Column("submitted_at", String(40)),
        CheckConstraint("status IN ('collecting','ready','submitted','discarded')", name="ck_brief_status"),
        **options,
    )
    Index("idx_briefs_conversation_status", briefs.c.conversation_id, briefs.c.status, briefs.c.updated_at)
    active_brief_slots = Table(
        "active_brief_slots", metadata,
        Column("conversation_id", String(191), ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True),
        Column("brief_id", String(191), ForeignKey("planning_briefs.id", ondelete="CASCADE"), nullable=False),
        UniqueConstraint("brief_id", name="uq_active_brief_slot_brief"),
        **options,
    )
    runs = Table(
        "runs", metadata,
        Column("id", String(191), primary_key=True), Column("user_id", String(191), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        Column("conversation_id", String(191), ForeignKey("conversations.id", ondelete="SET NULL")),
        Column("kind", String(24), nullable=False), Column("status", String(24), nullable=False),
        Column("concurrency_key", String(255), nullable=False), Column("request_snapshot_json", LONGTEXT, nullable=False),
        Column("disconnect_policy", String(16), nullable=False, server_default="continue"),
        # Kept as IDs without cross-lifecycle FKs: retry history remains
        # readable even before itinerary/history cutover in Ticket06.
        Column("retry_of_run_id", String(191)),
        Column("result_itinerary_id", String(191)),
        Column("error_public_json", LONGTEXT), Column("error_internal", LONGTEXT),
        Column("outstanding_interaction_id", String(191)), Column("created_at", String(40), nullable=False),
        Column("queued_at", String(40), nullable=False), Column("started_at", String(40)),
        Column("finished_at", String(40)), Column("updated_at", String(40), nullable=False),
        CheckConstraint("kind IN ('chat','travel_plan','revision','spot_tips')", name="ck_run_kind"),
        CheckConstraint("status IN ('queued','running','waiting_user','succeeded','failed','cancelled')", name="ck_run_status"),
        CheckConstraint("disconnect_policy IN ('continue','cancel')", name="ck_run_disconnect_policy"),
        **options,
    )
    # SQLite deliberately leaves these references unenforced to allow same-transaction inserts;
    # MySQL uses nullable FK values, populated only after both rows exist.
    Index("idx_runs_queue", runs.c.status, runs.c.queued_at, runs.c.id)
    Index("idx_runs_owner_active", runs.c.user_id, runs.c.status, runs.c.updated_at)
    Index("idx_runs_conversation", runs.c.conversation_id, runs.c.created_at, runs.c.id)
    interactions = Table(
        "run_interaction_responses", metadata,
        Column("run_id", String(191), ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True),
        Column("interaction_id", String(191), primary_key=True), Column("message_id", String(191), ForeignKey("messages.id", ondelete="SET NULL")),
        Column("value_json", LONGTEXT, nullable=False), Column("created_at", String(40), nullable=False), **options,
    )
    Index("idx_run_interaction_message", interactions.c.message_id)
    events = Table(
        "run_events", metadata,
        Column("id", String(191), primary_key=True), Column("run_id", String(191), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        Column("sequence", Integer, nullable=False), Column("kind", String(64), nullable=False),
        Column("payload_json", LONGTEXT, nullable=False), Column("durable", Integer, nullable=False, server_default="1"),
        Column("created_at", String(40), nullable=False), UniqueConstraint("run_id", "sequence", name="uq_run_events_sequence"), **options,
    )
    Index("idx_run_events_replay", events.c.run_id, events.c.sequence)
    return metadata
