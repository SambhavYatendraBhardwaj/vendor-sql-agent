Vendor Sales Text-to-SQL Agent

A tool-calling Text-to-SQL agent built with LangGraph. It turns natural-language business questions into SQL, runs them against a SQLite vendor sales database, repairs its own failed queries from the error messages, and synthesizes a final answer.

Developed and tested against a 7-table, 15.6M-row vendor sales database. The repo includes a script to generate a local sample database.

 Key Features

- **Tool calling:** the LLM chooses between four tools: `get_schema`, `describe_table`, `run_sql`, and `calculate`.
- **Self-healing SQL:** when a query fails or returns nothing useful, the error is fed back to the model so it can fix the query.
- **Controlled agent loop (LangGraph):**
  - a **nudge node** pushes the model to run SQL when it tries to answer without querying the data
  - a **hard 8-step cap** stops runaway loops
  - a **finalize node** forces an answer from the results collected so far
- **Safe execution:**
  - SELECT-only SQL validator
  - read-only database connection
  - 60-second query timeout
  - AST-based calculator instead of `eval`
- **Resilience:** backoff retries for transient API errors (HTTP 503 / 429).
- **Observability:** a Pydantic result model records the steps taken, SQL errors, and tools used for every question, with per-tool trace logging.

 Architecture

```
User question
     │
     ▼
[ Agent node: Gemini with tool calling ] ◄──────────────┐
     │                                                  │
     ├── tool call ──► [ get_schema | describe_table |  │
     │                   run_sql | calculate ] ─────────┘
     │
     ├── answered without running SQL ──► [ Nudge node ] ──► back to agent
     │
     ├── 8-step cap reached ──► [ Finalize node ] (forces answer from collected results)
     │
     ▼
Final answer + structured result log (steps, SQL errors, tools used)
```

 Failure modes found and fixed

| Problem | Fix |
|---|---|
| A "top brands by margin" ranking surfaced brands with 100% margin because it averaged unweighted per-row margins | Schema notes and prompt rules that require weighted aggregation |
| The agent looped for the full 8 steps, taking over a minute per question | Prompt rules plus per-tool trace logging to find where it was stuck |

 Tech Stack

| Layer | Tools |
|---|---|
| Orchestration | LangGraph |
| LLM | Google Gemini |
| Validation / result model | Pydantic |
| Database | SQLite |

 Repository Structure

```
vendor-sql-agent/
├── agent_sql.py         # LangGraph Text-to-SQL agent
├── agent_sql_v2.py      # Iterative / advanced agent implementation
├── make_sample_db.py    # Generates a local SQLite sample database
├── requirements.txt     # Python dependencies
├── .env.example         # Environment variable template
└── .gitignore
```

Setup

```bash
git clone https://github.com/SambhavYatendraBhardwaj/vendor-sql-agent.git
cd vendor-sql-agent

python -m venv myenv
# Windows (PowerShell): .\myenv\Scripts\Activate.ps1
# Linux / macOS:        source myenv/bin/activate

pip install -r requirements.txt
```

Copy `.env.example` to `.env` and add your API key. Set the database path if needed (defaults to `inventory.db`):

```
DB_PATH="inventory.db"
```

Generate the sample database:

```bash
python make_sample_db.py
```

Usage

Pass your question as a command-line argument:

```bash
python agent_sql.py "Which 5 vendors have the highest total freight cost?"
```

 Limitations

- SQLite only; other SQL dialects are not supported.
- Answer quality depends on the schema notes and prompt rules, which are tuned for the vendor sales dataset.
- No automated accuracy benchmark yet.
 Roadmap

- Benchmark set of questions with expected answers
- Multi-database support
- Streamlit or FastAPI front end
