"""Small journal for watchlist state and external-agent review rounds."""

from contextlib import contextmanager

from sqlalchemy import select

from src.storage import DatabaseManager, WatchlistReview


class WatchlistRepository:
    def __init__(self, db_manager=None):
        self.db = db_manager or DatabaseManager.get_instance()

    @contextmanager
    def session(self, *, write=False):
        with self.db.get_session() as session:
            if write:
                # Match the existing portfolio ledger's SQLite writer discipline.
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            yield session
            if write:
                session.commit()

    @staticmethod
    def latest(session):
        return session.execute(
            select(WatchlistReview).order_by(WatchlistReview.id.desc()).limit(1)
        ).scalar_one_or_none()

    @staticmethod
    def get(session, run_id):
        return session.execute(
            select(WatchlistReview).where(WatchlistReview.run_id == run_id)
        ).scalar_one_or_none()

    @staticmethod
    def save(session, *, run_id, request_json, result_json):
        row = WatchlistReview(run_id=run_id, request_json=request_json, result_json=result_json)
        session.add(row)
        session.flush()
        return row

    def history(self, limit=20):
        with self.session() as session:
            return list(session.execute(
                select(WatchlistReview).order_by(WatchlistReview.id.desc()).limit(limit)
            ).scalars())
