"""
Text-to-SQL agent on the Vendor Sales database (inventory.db).

Flow (LangGraph):
    get_schema -> write_sql -> execute -> (error/empty? retry write_sql, max N) -> answer

Run:  python agent_sql.py "Which 5 vendors have the highest total freight cost?"
"""
import os
import re
import sqlite3
import sys
import time
from typing import Optional, TypedDict

from pydantic import BaseModel, Field
from langgraph.graph import StateGraph, START, END

DB_PATH = os.getenv("DB_PATH", "inventory.db")
MAX_ATTEMPTS = 3
MAX_ROWS = 50          # rows returned to the LLM
QUERY_TIMEOUT_S = 60   # sales has 12.8M rows, stop runaway queries

# ----------------------------------------------------------------------------
# Pydantic models
# ----------------------------------------------------------------------------
class SQLQuery(BaseModel):
    reasoning: str = Field(description="One or two lines: which tables/columns and why")
    sql: str = Field(description="A single SQLite SELECT statement")


class FinalAnswer(BaseModel):
    answer: str = Field(description="Short plain-English answer using only the SQL result")


# ----------------------------------------------------------------------------
# Tools (plain functions first; we bind them to the LLM in step 2)
# ----------------------------------------------------------------------------
SCHEMA_NOTES = """
NOTES ABOUT THIS DATABASE (read carefully):
- Prices/amounts are in dollars. Date columns are TEXT in 'YYYY-MM-DD' format.
- final_summary is ALREADY aggregated per (VendorNumber, Brand). Prefer it for vendor/brand
  questions (profit margin, total sales, freight, stock turnover). It has no VendorName column;
  join to purchase_prices or vendor_invoice on VendorNumber to get names.
- Column names differ between tables: sales uses VendorNo, purchases/vendor_invoice/
  purchase_prices use VendorNumber.
- NEVER join sales directly to purchases (row-level joins fan out and inflate totals by
  hundreds of times). Aggregate each table in a subquery first, then join the results.
- sales (12.8M rows) and purchases (2.4M rows) are big: always filter or aggregate, never SELECT *.
- SQLite dialect only: use LIMIT, no ILIKE, no QUALIFY.
""".strip()


def get_schema() -> str:
    """Return CREATE TABLE statements plus row counts and usage notes."""
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        parts = []
        for name, sql in con.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table'"
        ).fetchall():
            n = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            parts.append(f"{sql}\n-- rows: {n:,}")
        return "\n\n".join(parts) + "\n\n" + SCHEMA_NOTES
    finally:
        con.close()


_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|reindex)\b",
    re.IGNORECASE,
)


def is_safe_select(query: str) -> Optional[str]:
    """Return an error message if the query is not a single read-only SELECT, else None."""
    q = query.strip().rstrip(";").strip()
    if ";" in q:
        return "Only one statement is allowed."
    if not re.match(r"^(select|with)\b", q, re.IGNORECASE):
        return "Only SELECT queries are allowed."
    if _FORBIDDEN.search(q):
        return "Query contains a forbidden keyword. Read-only SELECT only."
    return None


def run_sql(query: str) -> dict:
    """Run a read-only SELECT. Returns {'columns', 'rows', 'error'}."""
    problem = is_safe_select(query)
    if problem:
        return {"columns": [], "rows": [], "error": problem}

    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)  # read-only at DB level too
    start = time.time()
    # abort runaway queries
    con.set_progress_handler(lambda: 1 if time.time() - start > QUERY_TIMEOUT_S else 0, 100_000)
    try:
        cur = con.execute(query.strip().rstrip(";"))
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(MAX_ROWS)
        return {"columns": cols, "rows": rows, "error": None}
    except Exception as e:  # sqlite3.Error, timeout (OperationalError: interrupted), etc.
        return {"columns": [], "rows": [], "error": f"{type(e).__name__}: {e}"}
    finally:
        con.close()


# ----------------------------------------------------------------------------
# LangGraph
# ----------------------------------------------------------------------------
class AgentState(TypedDict):
    question: str
    schema: str
    sql: str
    reasoning: str
    columns: list
    rows: list
    error: Optional[str]
    attempts: int
    history: list          # [(sql, error_or_note), ...] of failed attempts
    answer: str


def build_graph(llm):
    """llm must support .with_structured_output(PydanticModel).invoke(prompt)."""
    sql_llm = llm.with_structured_output(SQLQuery)
    answer_llm = llm.with_structured_output(FinalAnswer)

    def schema_node(state: AgentState):
        return {"schema": get_schema(), "attempts": 0, "history": []}

    def write_sql_node(state: AgentState):
        prompt = (
            "You write SQLite queries for the database below.\n\n"
            f"{state['schema']}\n\n"
            f"Question: {state['question']}\n"
        )
        if state["history"]:
            prompt += "\nPrevious attempts FAILED. Fix them, do not repeat the same mistake:\n"
            for old_sql, why in state["history"]:
                prompt += f"- SQL: {old_sql}\n  Problem: {why}\n"
        out = sql_llm.invoke(prompt)
        return {"sql": out.sql.strip(), "reasoning": out.reasoning, "attempts": state["attempts"] + 1}

    def execute_node(state: AgentState):
        res = run_sql(state["sql"])
        update = {"columns": res["columns"], "rows": res["rows"], "error": res["error"]}
        # validator: an empty result is suspicious too, give the model one more look
        if res["error"] is None and not res["rows"]:
            update["error"] = "Query ran but returned 0 rows. Check filters, joins and column names."
        if update["error"]:
            update["history"] = state["history"] + [(state["sql"], update["error"])]
        return update

    def route(state: AgentState):
        if state["error"] and state["attempts"] < MAX_ATTEMPTS:
            return "write_sql"
        return "answer"

    def answer_node(state: AgentState):
        if state["error"]:
            return {"answer": f"Could not answer after {state['attempts']} attempts. Last error: {state['error']}"}
        table = [state["columns"]] + [list(r) for r in state["rows"]]
        prompt = (
            f"Question: {state['question']}\n"
            f"SQL used: {state['sql']}\n"
            f"Result (first {MAX_ROWS} rows max): {table}\n\n"
            "Answer the question in 1 to 3 sentences using ONLY this result. "
            "Do not invent numbers that are not in the result."
        )
        return {"answer": answer_llm.invoke(prompt).answer}

    g = StateGraph(AgentState)
    g.add_node("get_schema", schema_node)
    g.add_node("write_sql", write_sql_node)
    g.add_node("execute", execute_node)
    g.add_node("answer", answer_node)

    g.add_edge(START, "get_schema")
    g.add_edge("get_schema", "write_sql")
    g.add_edge("write_sql", "execute")
    g.add_conditional_edges("execute", route, {"write_sql": "write_sql", "answer": "answer"})
    g.add_edge("answer", END)
    return g.compile()


def ask(app, question: str) -> AgentState:
    return app.invoke({"question": question})


if __name__ == "__main__":
    from dotenv import load_dotenv
    from langchain_google_genai import ChatGoogleGenerativeAI

    load_dotenv()
    # Use the same model name you already use in risk_analyser.py
    llm = ChatGoogleGenerativeAI(model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
    app = build_graph(llm)

    q = " ".join(sys.argv[1:]) or "Which 5 vendors have the highest total freight cost?"
    out = ask(app, q)
    print("\nSQL     :", out["sql"])
    print("Attempts:", out["attempts"])
    print("Answer  :", out["answer"])
