"""Ticket07 schema for retained manual profiles and auditable imports."""

from sqlalchemy import Column, ForeignKey, String, Table
from sqlalchemy.dialects.mysql import LONGTEXT

from migrations_mysql.itinerary_baseline_20261006 import build_itinerary_metadata


def build_business_import_metadata():
    metadata = build_itinerary_metadata()
    options = dict(mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_0900_bin")
    Table(
        "user_profiles", metadata,
        Column("user_id", String(191), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        Column("attraction_prefs", LONGTEXT), Column("food_prefs", LONGTEXT),
        Column("habit_prefs", LONGTEXT), Column("visited_destinations", LONGTEXT),
        Column("updated_at", String(40), nullable=False), **options,
    )
    Table(
        "business_import_ledger", metadata,
        Column("source_id", String(64), primary_key=True),
        Column("source_digest", String(64), nullable=False),
        Column("manifest_json", LONGTEXT, nullable=False),
        Column("applied_at", String(40), nullable=False), **options,
    )
    return metadata
