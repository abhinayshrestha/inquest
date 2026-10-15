# Import every ORM model here so Base.metadata is complete for Alembic autogenerate.
from src.db.base import Base

__all__ = ["Base"]
