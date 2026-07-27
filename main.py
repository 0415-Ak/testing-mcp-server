from fastmcp import FastMCP
import os
import json
from datetime import datetime, timezone
from typing import Optional
import asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")
CATEGORIES_PATH = os.path.join(os.path.dirname(__file__), "categories.json")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Create a .env file (locally) or a DATABASE_URL "
        "environment variable (on FastMCP Cloud) with your Supabase connection string."
    )

mcp = FastMCP("ExpenseTracker")

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    '''Lazily create the connection pool on first use, inside the server's real event loop,
    and ensure the schema exists. asyncpg pools are bound to the event loop they're created
    on, so this must NOT be created at import time - only from inside an async tool call.'''
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=5,
            statement_cache_size=0,  # avoids known prepared-statement issues with Supabase's pooler
        )
        await ensure_schema(_pool)
    return _pool


async def ensure_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as c:
        await c.execute("""
            CREATE TABLE IF NOT EXISTS expenses(
                id SERIAL PRIMARY KEY,
                date TEXT NOT NULL,
                amount REAL NOT NULL,
                category TEXT NOT NULL,
                subcategory TEXT DEFAULT '',
                note TEXT DEFAULT '',
                created_at TEXT DEFAULT '',
                updated_at TEXT DEFAULT ''
            )
        """)
        await c.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS created_at TEXT DEFAULT ''")
        await c.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS updated_at TEXT DEFAULT ''")
    print("Database schema ready (Supabase/Postgres)")


def qmarks_to_dollars(query: str) -> str:
    '''Convert our SQLite-style "?" placeholders to Postgres-style "$1, $2, ..." in order.'''
    out = []
    n = 0
    for ch in query:
        if ch == "?":
            n += 1
            out.append(f"${n}")
        else:
            out.append(ch)
    return "".join(out)


def load_categories() -> dict:
    try:
        with open(CATEGORIES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"misc": ["uncategorized", "other"]}


CATEGORIES = load_categories()


def validate_category(category: str, subcategory: str = "") -> Optional[str]:
    if category not in CATEGORIES:
        valid = ", ".join(sorted(CATEGORIES.keys()))
        return f"Invalid category '{category}'. Valid categories: {valid}"
    if subcategory and subcategory not in CATEGORIES[category]:
        valid = ", ".join(CATEGORIES[category])
        return f"Invalid subcategory '{subcategory}' for category '{category}'. Valid subcategories: {valid}"
    return None


def validate_date(date_str: str) -> Optional[str]:
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return None
    except (ValueError, TypeError):
        return f"Invalid date '{date_str}'. Use YYYY-MM-DD format."


def validate_amount(amount) -> Optional[str]:
    if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0:
        return "Amount must be a positive number."
    return None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@mcp.tool()
async def add_expense(
    date: str,
    amount: float,
    category: str,
    subcategory: str = "",
    note: str = "",
) -> dict:
    '''Add a new expense entry. date must be YYYY-MM-DD, amount must be a positive number, and category/subcategory must match the expense:///categories resource.'''
    for err in (validate_date(date), validate_amount(amount), validate_category(category, subcategory)):
        if err:
            return {"status": "error", "message": err}
    try:
        pool = await get_pool()
        ts = now_iso()
        async with pool.acquire() as c:
            row = await c.fetchrow(
                """INSERT INTO expenses(date, amount, category, subcategory, note, created_at, updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id""",
                date, amount, category, subcategory, note, ts, ts,
            )
            return {"status": "success", "data": {"id": row["id"]}}
    except Exception as e:
        return {"status": "error", "message": f"Database error: {str(e)}"}


@mcp.tool()
async def get_expense(id: int) -> dict:
    '''Fetch a single expense entry by its id.'''
    try:
        pool = await get_pool()
        async with pool.acquire() as c:
            row = await c.fetchrow("SELECT * FROM expenses WHERE id = $1", id)
            if not row:
                return {"status": "error", "message": f"No expense found with id {id}"}
            return {"status": "success", "data": dict(row)}
    except Exception as e:
        return {"status": "error", "message": f"Database error: {str(e)}"}


@mcp.tool()
async def update_expense(
    id: int,
    date: Optional[str] = None,
    amount: Optional[float] = None,
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
    note: Optional[str] = None,
) -> dict:
    '''Update one or more fields of an existing expense by id. Only the fields you provide are changed; everything else is left as-is.'''
    try:
        pool = await get_pool()
        async with pool.acquire() as c:
            current = await c.fetchrow("SELECT * FROM expenses WHERE id = $1", id)
            if not current:
                return {"status": "error", "message": f"No expense found with id {id}"}

            new_date = date if date is not None else current["date"]
            new_amount = amount if amount is not None else current["amount"]
            new_category = category if category is not None else current["category"]
            new_subcategory = subcategory if subcategory is not None else current["subcategory"]
            new_note = note if note is not None else current["note"]

            for err in (
                validate_date(new_date),
                validate_amount(new_amount),
                validate_category(new_category, new_subcategory),
            ):
                if err:
                    return {"status": "error", "message": err}

            await c.execute(
                """UPDATE expenses
                   SET date=$1, amount=$2, category=$3, subcategory=$4, note=$5, updated_at=$6
                   WHERE id=$7""",
                new_date, new_amount, new_category, new_subcategory, new_note, now_iso(), id,
            )
            return {"status": "success", "data": {"id": id}}
    except Exception as e:
        return {"status": "error", "message": f"Database error: {str(e)}"}


@mcp.tool()
async def delete_expense(id: int) -> dict:
    '''Delete an expense entry by id.'''
    try:
        pool = await get_pool()
        async with pool.acquire() as c:
            row = await c.fetchrow("DELETE FROM expenses WHERE id = $1 RETURNING id", id)
            if not row:
                return {"status": "error", "message": f"No expense found with id {id}"}
            return {"status": "success", "data": {"id": id}}
    except Exception as e:
        return {"status": "error", "message": f"Database error: {str(e)}"}


@mcp.tool()
async def list_expenses(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
    min_amount: Optional[float] = None,
    max_amount: Optional[float] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    '''List expenses. Leave start_date and end_date empty to list all expenses ever recorded; provide either or both to restrict to a date range (inclusive). Supports optional filters (category, subcategory, min_amount, max_amount, search over notes) and pagination via limit/offset.'''
    date_err = (validate_date(start_date) if start_date else None) or (validate_date(end_date) if end_date else None)
    if date_err:
        return {"status": "error", "message": date_err}
    try:
        query = "SELECT * FROM expenses WHERE 1=1"
        params = []

        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        if category:
            query += " AND category = ?"
            params.append(category)
        if subcategory:
            query += " AND subcategory = ?"
            params.append(subcategory)
        if min_amount is not None:
            query += " AND amount >= ?"
            params.append(min_amount)
        if max_amount is not None:
            query += " AND amount <= ?"
            params.append(max_amount)
        if search:
            query += " AND note LIKE ?"
            params.append(f"%{search}%")

        query += " ORDER BY date DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        pool = await get_pool()
        async with pool.acquire() as c:
            rows = await c.fetch(qmarks_to_dollars(query), *params)
            return {"status": "success", "data": [dict(r) for r in rows]}
    except Exception as e:
        return {"status": "error", "message": f"Error listing expenses: {str(e)}"}


@mcp.tool()
async def summarize(start_date: Optional[str] = None, end_date: Optional[str] = None, category: Optional[str] = None) -> dict:
    '''Summarize total spend and entry count grouped by category. Leave start_date and end_date empty to summarize all expenses ever recorded; provide either or both to restrict to a date range (inclusive). Pass category to restrict to one category.'''
    date_err = (validate_date(start_date) if start_date else None) or (validate_date(end_date) if end_date else None)
    if date_err:
        return {"status": "error", "message": date_err}
    try:
        query = """
            SELECT category, SUM(amount) AS total_amount, COUNT(*) as count
            FROM expenses
            WHERE 1=1
        """
        params = []

        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        if category:
            query += " AND category = ?"
            params.append(category)

        query += " GROUP BY category ORDER BY total_amount DESC"

        pool = await get_pool()
        async with pool.acquire() as c:
            rows = await c.fetch(qmarks_to_dollars(query), *params)
            return {"status": "success", "data": [dict(r) for r in rows]}
    except Exception as e:
        return {"status": "error", "message": f"Error summarizing expenses: {str(e)}"}


@mcp.resource("expense:///categories", mime_type="application/json")
def categories() -> str:
    return json.dumps(CATEGORIES, indent=2)


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000)
