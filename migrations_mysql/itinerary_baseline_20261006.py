"""Ticket06 additive itinerary enrichment and shadow-result schema."""

from sqlalchemy import CheckConstraint, Column, ForeignKey, Index, String, Table, UniqueConstraint
from sqlalchemy.dialects.mysql import LONGTEXT

from migrations_mysql.runtime_baseline_20261006 import build_runtime_metadata


def build_itinerary_metadata():
    metadata = build_runtime_metadata()
    options = dict(mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_0900_bin")
    enrichments = Table(
        "itinerary_enrichments", metadata,
        Column("id", String(191), primary_key=True),
        Column("itinerary_id", String(191), ForeignKey("itineraries.id", ondelete="CASCADE"), nullable=False),
        Column("user_id", String(191), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        Column("kind", String(32), nullable=False),
        Column("route_fingerprint", String(191), nullable=False),
        Column("payload_json", LONGTEXT, nullable=False),
        Column("created_at", String(40), nullable=False),
        Column("updated_at", String(40), nullable=False),
        UniqueConstraint("itinerary_id", "kind", "route_fingerprint", name="uq_itinerary_enrichment_route"),
        CheckConstraint("kind IN ('restaurant','spot_tips')", name="ck_itinerary_enrichment_kind"),
        **options,
    )
    Index("idx_itinerary_enrichments_route", enrichments.c.itinerary_id, enrichments.c.kind, enrichments.c.route_fingerprint)
    shadows = Table(
        "optimizer_shadow_runs", metadata,
        Column("id", String(191), primary_key=True),
        Column("itinerary_id", String(191), ForeignKey("itineraries.id", ondelete="CASCADE"), nullable=False),
        Column("user_id", String(191), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        Column("profile_version", String(191), nullable=False),
        Column("candidate_pool_fingerprint", String(191), nullable=False),
        Column("status", String(16), nullable=False),
        Column("result_json", LONGTEXT),
        Column("error_text", LONGTEXT),
        Column("first_user_edit_at", String(40)),
        Column("edited_route_fingerprint", String(191)),
        Column("created_at", String(40), nullable=False),
        Column("updated_at", String(40), nullable=False),
        UniqueConstraint("itinerary_id", "profile_version", "candidate_pool_fingerprint", name="uq_shadow_itinerary_profile_pool"),
        CheckConstraint("status IN ('running','succeeded','failed')", name="ck_shadow_run_status"),
        **options,
    )
    Index("idx_shadow_runs_itinerary", shadows.c.itinerary_id, shadows.c.created_at)
    return metadata
