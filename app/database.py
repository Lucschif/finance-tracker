from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime

from sqlalchemy import Boolean, Column, Date, DateTime, Float, Integer, String, Text, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy import create_engine

from app import config


# ── Engine ────────────────────────────────────────────────────────────────────

_connect_args = {"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(config.DATABASE_URL, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Forecast(Base):
    __tablename__ = "forecasts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String, nullable=False)          # ticker or 'TOTAL'
    computed_at = Column(DateTime, default=datetime.utcnow)
    forecast_dates = Column(Text, nullable=False)    # JSON ["2026-05-25", ...]
    point_forecast = Column(Text, nullable=False)    # JSON [12345.0, ...]
    q10_forecast = Column(Text, nullable=False)      # JSON [11000.0, ...]
    q90_forecast = Column(Text, nullable=False)      # JSON [14000.0, ...]


class Holding(Base):
    __tablename__ = "holdings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String, unique=True, nullable=False)   # e.g. VWCE.DE / BTC-EUR
    name = Column(String, nullable=True)                   # friendly label
    quantity = Column(Float, nullable=False)
    asset_type = Column(String, nullable=False, default="etf")  # etf | stock | crypto
    created_at = Column(DateTime, default=datetime.utcnow)


# ── Models ────────────────────────────────────────────────────────────────────

class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, unique=True, nullable=False)
    initial_balance = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    amount = Column(Float, nullable=False)
    type = Column(String, nullable=False)      # income | expense | transfer
    category = Column(String, nullable=False)
    note = Column(String, nullable=True)
    date = Column(Date, default=date.today)
    created_at = Column(DateTime, default=datetime.utcnow)
    is_impulse = Column(Boolean, default=False)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _as_date(val) -> date:
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    try:
        return date.fromisoformat(str(val)[:10])
    except Exception:
        return date.today()


# ── Init / migrations ─────────────────────────────────────────────────────────

def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _run_migrations()


def _run_migrations() -> None:
    with engine.connect() as conn:
        is_pg = "postgresql" in str(engine.url)
        if is_pg:
            conn.execute(text(
                "ALTER TABLE transactions "
                "ADD COLUMN IF NOT EXISTS is_impulse BOOLEAN DEFAULT FALSE"
            ))
        else:
            try:
                conn.execute(text(
                    "ALTER TABLE transactions ADD COLUMN is_impulse BOOLEAN DEFAULT FALSE"
                ))
            except Exception:
                pass
        conn.commit()


# ── Session context manager ───────────────────────────────────────────────────

@contextmanager
def get_db():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ── CRUD ──────────────────────────────────────────────────────────────────────

def add_transaction(
    db: Session,
    *,
    amount: float,
    type_: str,
    category: str,
    note: str = "",
    is_impulse: bool = False,
    txn_date: date | None = None,
) -> Transaction:
    t = Transaction(
        amount=amount,
        type=type_,
        category=category,
        note=note,
        date=txn_date or date.today(),
        is_impulse=is_impulse,
    )
    db.add(t)
    db.flush()
    return t


def _active_filter(query):
    return query.filter(
        (Transaction.note == None) | ~Transaction.note.contains("[UNDONE]")  # noqa: E711
    )


def get_active_transactions(db: Session) -> list[Transaction]:
    return (
        _active_filter(db.query(Transaction))
        .order_by(Transaction.date.asc(), Transaction.created_at.asc())
        .all()
    )


def get_all_transactions(db: Session) -> list[Transaction]:
    return (
        db.query(Transaction)
        .order_by(Transaction.date.asc(), Transaction.created_at.asc())
        .all()
    )


def get_recent_transactions(db: Session, limit: int = 30) -> list[Transaction]:
    return (
        db.query(Transaction)
        .order_by(Transaction.created_at.desc())
        .limit(limit)
        .all()
    )


def get_last_transaction(db: Session) -> Transaction | None:
    return (
        _active_filter(db.query(Transaction))
        .order_by(Transaction.created_at.desc())
        .first()
    )


def undo_last_transaction(db: Session) -> Transaction | None:
    t = get_last_transaction(db)
    if t:
        t.note = f"[UNDONE] {t.note or ''}".strip()
    return t


def get_accounts(db: Session) -> list[Account]:
    return db.query(Account).all()


def get_holdings(db: Session) -> list[Holding]:
    return db.query(Holding).order_by(Holding.asset_type, Holding.symbol).all()


def upsert_holding(db: Session, symbol: str, quantity: float,
                   asset_type: str = "etf", name: str | None = None) -> Holding:
    h = db.query(Holding).filter(Holding.symbol == symbol.upper()).first()
    if h:
        h.quantity = quantity
        if name:
            h.name = name
    else:
        h = Holding(symbol=symbol.upper(), quantity=quantity,
                    asset_type=asset_type, name=name)
        db.add(h)
    db.flush()
    return h


def delete_holding(db: Session, symbol: str) -> bool:
    h = db.query(Holding).filter(Holding.symbol == symbol.upper()).first()
    if h:
        db.delete(h)
        db.flush()
        return True
    return False


def set_account_balance(db: Session, name: str, balance: float) -> Account:
    account = db.query(Account).filter(Account.name == name).first()
    if account:
        account.initial_balance = balance
    else:
        account = Account(name=name, initial_balance=balance)
        db.add(account)
    db.flush()
    return account
