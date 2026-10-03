"""
Text-to-SQL agent v2: the LLM decides which tool to call (tool calling).

Graph:
    START -> agent -> (tool calls?) -> tools -> agent -> ... -> END
                 └-> (no tool calls, never ran SQL?) -> nudge -> agent
Retry is now agentic: a failed run_sql returns the error as a ToolMessage and the
model fixes its own query. MAX_STEPS stops runaway loops.

Needs agent_sql.py (v1) in the same folder; we reuse its DB helpers.
Run:  python agent_sql_v2.py "Which 5 vendors have the highest total freight cost?"
"""
import ast
import operator
import os
import sys
import time
from functools import lru_cache
from typing import Annotated, Optional, TypedDict

import sqlite3
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel

import agent_sql as base  # v1 helpers: get_schema, run_sql, DB_PATH, ...

MAX_STEPS = 8  # max LLM turns per question

SYSTEM_PROMPT = """You are a careful data analyst agent for a SQLite vendor/inventory database.

Tools: get_schema, describe_table, run_sql, calculate. Rules:
1. Call get_schema first if you do not already know the tables. Never guess column names.
2. Answer ONLY from run_sql results. Never invent numbers. Do not answer before running SQL.
3. If run_sql returns an error or 0 rows, read the message, fix the query and try again.
4. Use calculate for arithmetic instead of doing math yourself.
5. When ranking by a ratio (Profitmargin, StockTurnover, SalesPurchaseRatio), exclude rows with
   zero purchases or negligible volume, and prefer weighted ratios
   (SUM(GrossProfit)/SUM(TotalSalesDollars)) over AVG of the ratio.
6. Be efficient: usually 1 or 2 run_sql calls are enough. As soon as a result answers the question,
   STOP calling tools and write the final answer. Do not re-run queries just to double-check,
   reformat or clean names.
7. Respect the requested count exactly ("top 5" means LIMIT 5).
8. For vendor-level metrics (freight, sales, profit) prefer final_summary (pre-aggregated, fast).
   Join other tables only when you need names or fields it does not have.
9. Final answer: 1 to 3 sentences, mention the key numbers, no markdown tables."""


# ----------------------------------------------------------------------------
# Tools
# ----------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _cached_schema() -> str:
    return base.get_schema()  # COUNT(*) on 12.8M rows runs once, not on every question


@tool
def get_schema() -> str:
    """Return all table definitions (columns, types, row counts) and usage notes for the database."""
    return _cached_schema()


@tool
def describe_table(table_name: str) -> str:
    """Return 3 sample rows from one table, to see value and date formats."""
    if not table_name.replace("_", "").isalnum():
        return "ERROR: invalid table name."
    con = sqlite3.connect(f"file:{base.DB_PATH}?mode=ro", uri=True)
    try:
        cur = con.execute(f'SELECT * FROM "{table_name}" LIMIT 3')
        cols = [d[0] for d in cur.description]
        return f"columns: {cols}\nrows: {cur.fetchall()}"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"
    finally:
        con.close()


@tool
def run_sql(query: str) -> str:
    """Run ONE read-only SQLite SELECT query and return up to 50 rows. Returns an ERROR message on failure."""
    res = base.run_sql(query)
    if res["error"]:
        return f"ERROR: {res['error']}"
    if not res["rows"]:
        return "OK but 0 rows returned. Check filters, joins and column names."
    return f"columns: {res['columns']}\nrows: {res['rows']}"


_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.USub: operator.neg,
    ast.Mod: operator.mod,
}


def _eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError("unsupported expression")


@tool
def calculate(expression: str) -> str:
    """Evaluate an arithmetic expression like '(120.5 - 80) / 80 * 100'. Supports + - * / ** %."""
    try:
        return str(_eval(ast.parse(expression, mode="eval").body))
    except Exception as e:
        return f"ERROR: {e}"


TOOLS = [get_schema, describe_table, run_sql, calculate]
TOOLS_BY_NAME = {t.name: t for t in TOOLS}


# ----------------------------------------------------------------------------
# Result model (also used by the eval script later)
# ----------------------------------------------------------------------------
class AgentResult(BaseModel):
    question: str
    answer: str
    last_sql: Optional[str] = None
    steps: int
    sql_calls: int
    sql_errors: int
    tools_used: list[str]
    forced_final: bool = False


# ----------------------------------------------------------------------------
# Graph
# ----------------------------------------------------------------------------
class State(TypedDict):
    messages: Annotated[list, add_messages]
    steps: int
    sql_calls: int
    sql_errors: int
    sql_ok: bool
    nudged: bool
    last_sql: Optional[str]
    tools_used: list
    forced: bool


def _text(msg) -> str:
    """Gemini may return content as a list of parts; flatten to plain text."""
    c = msg.content
    if isinstance(c, str):
        return c
    return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)


def _invoke(model, msgs, tries: int = 4):
    """Call the LLM; retry with exponential backoff on temporary API errors (503, 429, timeouts)."""
    for i in range(tries):
        try:
            return model.invoke(msgs)
        except Exception as e:
            transient = any(k in str(e) for k in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "DEADLINE", "timed out"))
            if not transient or i == tries - 1:
                raise
            wait = 2 * (2 ** i)  # 2s, 4s, 8s
            print(f"  [retry] temporary LLM error, waiting {wait}s (attempt {i + 1}/{tries - 1})")
            time.sleep(wait)


def build_graph(llm, max_steps: int = MAX_STEPS, trace: bool = False):
    llm_tools = llm.bind_tools(TOOLS)

    def agent_node(state: State):
        reply = _invoke(llm_tools, state["messages"])
        return {"messages": [reply], "steps": state["steps"] + 1}

    def tools_node(state: State):
        out, sql_calls, sql_errors = [], state["sql_calls"], state["sql_errors"]
        sql_ok, last_sql, used = state["sql_ok"], state["last_sql"], list(state["tools_used"])
        for call in state["messages"][-1].tool_calls:
            name, args = call["name"], call["args"]
            used.append(name)
            t = TOOLS_BY_NAME.get(name)
            try:
                result = t.invoke(args) if t else f"ERROR: unknown tool {name}"
            except Exception as e:
                result = f"ERROR: {type(e).__name__}: {e}"
            if name == "run_sql":
                sql_calls += 1
                last_sql = args.get("query")
                if result.startswith("ERROR") or result.startswith("OK but 0 rows"):
                    sql_errors += 1
                else:
                    sql_ok = True
            if trace:
                print(f"  [tool] {name}({str(args)[:300]})\n         -> {str(result)[:200]!r}")
            out.append(ToolMessage(content=str(result), tool_call_id=call["id"]))
        return {"messages": out, "sql_calls": sql_calls, "sql_errors": sql_errors,
                "sql_ok": sql_ok, "last_sql": last_sql, "tools_used": used}

    def nudge_node(state: State):
        msg = HumanMessage(content="You have not run any successful SQL yet. "
                                   "Use run_sql on the database, then answer from its result.")
        return {"messages": [msg], "nudged": True}

    def finalize_node(state: State):
        # Step cap hit: drop the unanswered tool call and make the model answer from what it has.
        msgs = list(state["messages"][:-1]) + [HumanMessage(content=(
            "Step limit reached. Do not call any more tools. Give your best final answer from the "
            "results you already have, and say clearly if you are uncertain."))]
        reply = _invoke(llm, msgs)  # plain LLM, no tools bound
        return {"messages": [reply], "steps": state["steps"] + 1, "forced": True}

    def route(state: State):
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None):
            return "tools" if state["steps"] < max_steps else "finalize"
        if not state["sql_ok"] and not state["nudged"]:
            return "nudge"
        return END

    g = StateGraph(State)
    g.add_node("agent", agent_node)
    g.add_node("tools", tools_node)
    g.add_node("nudge", nudge_node)
    g.add_node("finalize", finalize_node)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route, {"tools": "tools", "nudge": "nudge", "finalize": "finalize", END: END})
    g.add_edge("finalize", END)
    g.add_edge("tools", "agent")
    g.add_edge("nudge", "agent")
    return g.compile()


def ask(app, question: str) -> AgentResult:
    final = app.invoke(
        {
            "messages": [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=question)],
            "steps": 0, "sql_calls": 0, "sql_errors": 0, "sql_ok": False,
            "nudged": False, "last_sql": None, "tools_used": [], "forced": False,
        },
        config={"recursion_limit": 50},
    )
    last = final["messages"][-1]
    if getattr(last, "tool_calls", None):
        answer = f"Stopped after {final['steps']} steps without a final answer."
    elif not final["sql_ok"]:
        answer = "Could not produce a verified answer (no successful SQL was run)."
    else:
        answer = _text(last).strip()
    return AgentResult(
        question=question, answer=answer, last_sql=final["last_sql"], steps=final["steps"],
        sql_calls=final["sql_calls"], sql_errors=final["sql_errors"], tools_used=final["tools_used"],
        forced_final=final["forced"],
    )


if __name__ == "__main__":
    from dotenv import load_dotenv
    from langchain_google_genai import ChatGoogleGenerativeAI

    load_dotenv()
    llm = ChatGoogleGenerativeAI(model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
    app = build_graph(llm, trace=True)
    q = " ".join(sys.argv[1:]) or "Which 5 vendors have the highest total freight cost?"
    t0 = time.perf_counter()
    r = ask(app, q)
    print("\nTools used :", r.tools_used)
    print("SQL calls  :", r.sql_calls, "| errors:", r.sql_errors, "| LLM steps:", r.steps)
    print("Forced stop:", r.forced_final)
    print("Last SQL   :", r.last_sql)
    print("Answer     :", r.answer)
    print(f"Total time : {time.perf_counter() - t0:.1f}s")
