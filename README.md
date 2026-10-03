Vendor Sales Text-to-SQL AgentAn agentic Text-to-SQL pipeline built with LangGraph that converts natural language business questions into SQL queries, executes them against a SQLite vendor database, self-corrects execution errors, and synthesizes final answers.   Architecture FlowThe agent runs as a state graph with automatic retry and correction loops:   Plaintext[User Query] 
      │
      ▼
┌──────────────┐
│  Get Schema  │ ──► Inspects table structures and schemas
└──────┬───────┘
       ▼
┌──────────────┐
│  Write SQL   │ ──► Generates valid SQLite dialect queries
└──────┬───────┘
       ▼
┌──────────────┐
│ Execute SQL  │ ──► Runs query against database
└──────┬───────┘
       ├─────────────────────────────────┐
       ▼ (Execution Error / Empty Rows)  ▼ (Success)
┌──────────────┐                  ┌──────────────┐
│ Retry Loop   │                  │ Synthesize   │
│ (up to max N)│                  │ Final Answer │
└──────────────┘                  └──────────────┘
   FeaturesSelf-Healing SQL Generation: Automatically catches execution syntax issues or empty results and feeds the error back to the model for up to $N$ retry cycles.   Dynamic Schema Inspection: Dynamically extracts SQLite table metadata to ground prompts with accurate column names and data types.   Reproducible Sample Database: Includes an automated generator script (make_sample_db.py) to build test environments locally without checking heavy raw CSV data into Git.   Repository StructurePlaintextvendor-sql-agent/
├── agent_sql.py          # Core LangGraph Text-to-SQL implementation
├── agent_sql_v2.py       # Iterative / advanced agent implementation
├── make_sample_db.py     # Script to generate local SQLite database
├── requirements.txt      # Python dependencies
├── .env.example          # Environment variable template
└── .gitignore            # Git ignore configuration
   Setup & Installation1. Clone the RepositoryBashgit clone https://github.com/SambhavYatendraBhardwaj/vendor-sql-agent.git
cd vendor-sql-agent
   2. Set Up Virtual EnvironmentBash# Windows (PowerShell)
python -m venv myenv
.\myenv\Scripts\Activate.ps1

# Linux / macOS
python3 -m venv myenv
source myenv/bin/activate
   3. Install DependenciesBashpip install -r requirements.txt
   4. Configure Environment VariablesCopy .env.example to create .env and configure your API credentials and database path:   Bashcp .env.example .env
Ensure your database path is set (defaults to inventory.db if unset):   Code snippetDB_PATH="inventory.db"
   5. Generate Sample DatabaseRun the setup script to construct the local SQLite database:   Bashpython make_sample_db.py
   UsagePass your analytical question directly as a command-line argument:   Bashpython agent_sql.py "Which 5 vendors have the highest total freight cost?"
