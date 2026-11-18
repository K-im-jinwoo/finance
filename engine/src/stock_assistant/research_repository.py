"""Read-only access for audits and proposal evaluations, without schema creation."""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3

from .repository import StockRepository


class ReadOnlyResearchRepository(StockRepository):
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve(strict=True)
        if not self.database_path.is_file():
            raise ValueError("research source must be an existing database file")

    @contextmanager
    def _connect(self):
        with closing(sqlite3.connect(self.database_path.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            yield connection

