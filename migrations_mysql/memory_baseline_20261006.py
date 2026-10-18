"""Frozen Ticket04 MySQL memory and conversation ownership schema."""
from sqlalchemy import (
    CheckConstraint, Column, ForeignKey, Index, Integer, String,
    Table, Text, UniqueConstraint,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from migrations_mysql.identity_baseline_20261006 import build_metadata


def build_memory_metadata():
    metadata = build_metadata()
    options = dict(mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_0900_bin")
    conversations = Table(
        "conversations", metadata,
        Column("id", String(191), primary_key=True),
        Column("user_id", String(191), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        Column("title", LONGTEXT, nullable=False),
        Column("status", String(16), nullable=False, server_default="active"),
        Column("archived_at", String(40)),
        Column("last_viewed_at", String(40)),
        Column("created_at", String(40), nullable=False),
        Column("updated_at", String(40), nullable=False),
        CheckConstraint("status IN ('active','archived')", name="ck_conversation_status"),
        **options,
    )
    Index("idx_conversations_owner_updated", conversations.c.user_id, conversations.c.updated_at, conversations.c.id)
    states = Table(
        "conversation_memory_states", metadata,
        Column("conversation_id", String(191), ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True),
        Column("user_id", String(191), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        Column("profile_revision", Integer, nullable=False, server_default="0"),
        Column("profile_snapshot_json", LONGTEXT, nullable=False),
        Column("summary_json", LONGTEXT),
        Column("summarized_through_sequence", Integer, nullable=False, server_default="0"),
        Column("summary_count", Integer, nullable=False, server_default="0"),
        Column("estimated_context_tokens", Integer, nullable=False, server_default="0"),
        Column("finalization_status", String(16), nullable=False, server_default="none"),
        Column("finalized_at", String(40)),
        Column("last_error_code", String(128)),
        Column("created_at", String(40), nullable=False),
        Column("updated_at", String(40), nullable=False),
        CheckConstraint("finalization_status IN ('none','pending','succeeded','failed')", name="ck_memory_finalization"),
        **options,
    )
    jobs = Table(
        "memory_extraction_jobs", metadata,
        Column("id", String(191), primary_key=True),
        Column("conversation_id", String(191), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        Column("user_id", String(191), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        Column("kind", String(24), nullable=False),
        Column("from_sequence", Integer, nullable=False),
        Column("through_sequence", Integer, nullable=False),
        Column("status", String(16), nullable=False, server_default="pending"),
        Column("attempts", Integer, nullable=False, server_default="0"),
        Column("next_attempt_at", String(40)),
        Column("last_error_code", String(128)),
        Column("created_at", String(40), nullable=False),
        Column("updated_at", String(40), nullable=False),
        Column("finished_at", String(40)),
        UniqueConstraint("conversation_id", "kind", "from_sequence", "through_sequence", name="uq_memory_job_window"),
        CheckConstraint("kind IN ('pre_summary','archive')", name="ck_memory_job_kind"),
        CheckConstraint("status IN ('pending','running','succeeded','failed')", name="ck_memory_job_status"),
        **options,
    )
    Index("idx_memory_jobs_claim", jobs.c.status, jobs.c.next_attempt_at, jobs.c.created_at)
    return metadata
