from fastmcp import FastMCP
import os
import json
from datetime import datetime, timezone
from typing import Optional
import aiosqlite

DB_PATH = os.path.join(os.path.dirname(__file__), "expenses.db")
CATEGORIES_PATH = os.path.join(os.path.dirname(__file__), "categories.json")

print(f"Database path: {DB_PATH}")

mcp = FastMCP("ExpenseTracker")


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


def rows_to_dicts(cursor, rows) -> list:
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in rows]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db():
    try:
        import sqlite3
        with sqlite3.connect(DB_PATH) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("""
                CREATE TABLE IF NOT EXISTS expenses(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    amount REAL NOT NULL,
                    category TEXT NOT NULL,
                    subcategory TEXT DEFAULT '',
                    note TEXT DEFAULT '',
                    created_at TEXT DEFAULT '',
                    updated_at TEXT DEFAULT ''
                )
            """)
            existing_cols = {row[1] for row in c.execute("PRAGMA table_info(expenses)")}
            if "created_at" not in existing_cols:
                c.execute("ALTER TABLE expenses ADD COLUMN created_at TEXT DEFAULT ''")
            if "updated_at" not in existing_cols:
                c.execute("ALTER TABLE expenses ADD COLUMN updated_at TEXT DEFAULT ''")
            print("Database initialized successfully with write access")
    except Exception as e:
        print(f"Database initialization error: {e}")
        raise


init_db()


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
        ts = now_iso()
        async with aiosqlite.connect(DB_PATH) as c:
            cur = await c.execute(
                """INSERT INTO expenses(date, amount, category, subcategory, note, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (date, amount, category, subcategory, note, ts, ts),
            )
            await c.commit()
            return {"status": "success", "data": {"id": cur.lastrowid}}
    except Exception as e:
        return {"status": "error", "message": f"Database error: {str(e)}"}


@mcp.tool()
async def get_expense(id: int) -> dict:
    '''Fetch a single expense entry by its id.'''
    try:
        async with aiosqlite.connect(DB_PATH) as c:
            cur = await c.execute("SELECT * FROM expenses WHERE id = ?", (id,))
            rows = await cur.fetchall()
            results = rows_to_dicts(cur, rows)
            if not results:
                return {"status": "error", "message": f"No expense found with id {id}"}
            return {"status": "success", "data": results[0]}
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
        async with aiosqlite.connect(DB_PATH) as c:
            cur = await c.execute("SELECT * FROM expenses WHERE id = ?", (id,))
            rows = await cur.fetchall()
            existing = rows_to_dicts(cur, rows)
            if not existing:
                return {"status": "error", "message": f"No expense found with id {id}"}
            current = existing[0]

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
                   SET date=?, amount=?, category=?, subcategory=?, note=?, updated_at=?
                   WHERE id=?""",
                (new_date, new_amount, new_category, new_subcategory, new_note, now_iso(), id),
            )
            await c.commit()
            return {"status": "success", "data": {"id": id}}
    except Exception as e:
        return {"status": "error", "message": f"Database error: {str(e)}"}


@mcp.tool()
async def delete_expense(id: int) -> dict:
    '''Delete an expense entry by id.'''
    try:
        async with aiosqlite.connect(DB_PATH) as c:
            cur = await c.execute("DELETE FROM expenses WHERE id = ?", (id,))
            await c.commit()
            if cur.rowcount == 0:
                return {"status": "error", "message": f"No expense found with id {id}"}
            return {"status": "success", "data": {"id": id}}
    except Exception as e:
        return {"status": "error", "message": f"Database error: {str(e)}"}


@mcp.tool()
async def list_expenses(
    start_date: str,
    end_date: str,
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
    min_amount: Optional[float] = None,
    max_amount: Optional[float] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    '''List expenses within an inclusive date range. Supports optional filters (category, subcategory, min_amount, max_amount, search over notes) and pagination via limit/offset.'''
    date_err = validate_date(start_date) or validate_date(end_date)
    if date_err:
        return {"status": "error", "message": date_err}
    try:
        query = "SELECT * FROM expenses WHERE date BETWEEN ? AND ?"
        params = [start_date, end_date]

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

        async with aiosqlite.connect(DB_PATH) as c:
            cur = await c.execute(query, params)
            rows = await cur.fetchall()
            return {"status": "success", "data": rows_to_dicts(cur, rows)}
    except Exception as e:
        return {"status": "error", "message": f"Error listing expenses: {str(e)}"}


@mcp.tool()
async def summarize(start_date: str, end_date: str, category: Optional[str] = None) -> dict:
    '''Summarize total spend and entry count grouped by category, within an inclusive date range. Pass category to restrict to one category.'''
    date_err = validate_date(start_date) or validate_date(end_date)
    if date_err:
        return {"status": "error", "message": date_err}
    try:
        query = """
            SELECT category, SUM(amount) AS total_amount, COUNT(*) as count
            FROM expenses
            WHERE date BETWEEN ? AND ?
        """
        params = [start_date, end_date]

        if category:
            query += " AND category = ?"
            params.append(category)

        query += " GROUP BY category ORDER BY total_amount DESC"

        async with aiosqlite.connect(DB_PATH) as c:
            cur = await c.execute(query, params)
            rows = await cur.fetchall()
            return {"status": "success", "data": rows_to_dicts(cur, rows)}
    except Exception as e:
        return {"status": "error", "message": f"Error summarizing expenses: {str(e)}"}


@mcp.resource("expense:///categories", mime_type="application/json")
def categories() -> str:
    return json.dumps(CATEGORIES, indent=2)


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000)
