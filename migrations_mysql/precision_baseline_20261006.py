"""Frozen revision-06 contract: keep SQLite/Python confidence precision."""
from sqlalchemy.dialects.mysql import DOUBLE
from migrations_mysql.business_import_baseline_20261006 import build_business_import_metadata


def build_precision_metadata():
    metadata = build_business_import_metadata()
    # DOUBLE defaults to Decimal with ten fractional places in SQLAlchemy.
    # SQLite REAL and the public repository contract both use Python float.
    metadata.tables["memory_facts"].c.confidence.type = DOUBLE(asdecimal=False)
    return metadata
