import os
import time

import streamlit as st
from dotenv import load_dotenv

load_dotenv()  # local runs: read GOOGLE_API_KEY from .env

# Point the agent at the small demo database BEFORE importing it.
os.environ.setdefault("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample.db"))

# API key from Streamlit secrets (never hardcode it in the repo)
for _k in ("LLM_PROVIDER", "GOOGLE_API_KEY", "GEMINI_MODEL", "GROQ_API_KEY", "GROQ_MODEL"):
    try:
        os.environ[_k] = st.secrets[_k]
    except Exception:
        pass  # not in Streamlit secrets: falls back to a local .env / environment variable

import agent_sql_v2 as agent  # noqa: E402

MAX_QUERIES_PER_SESSION = 5

EXAMPLES = [
    "Which 5 vendors have the highest total freight cost?",
    "Which 5 brands have the highest profit margin?",
    "What are the top 5 vendors by total sales dollars?",
]


@st.cache_resource
def get_app():
    if os.getenv("LLM_PROVIDER", "gemini").lower() == "groq":
        from langchain_groq import ChatGroq

        llm = ChatGroq(model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"), temperature=0)
    else:
        from langchain_google_genai import ChatGoogleGenerativeAI

        llm = ChatGoogleGenerativeAI(model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
    return agent.build_graph(llm)


st.set_page_config(page_title="Vendor Sales SQL Agent", page_icon="🗄️")
st.title("Vendor Sales SQL Agent")
st.caption(
    "A LangGraph tool-calling agent: the LLM picks between get_schema, describe_table, "
    "run_sql and calculate, and repairs its own failed queries. "
    "Demo runs on a sampled subset of the 15.6M-row database, so exact numbers differ from the full data."
)

if "count" not in st.session_state:
    st.session_state["count"] = 0
if "q" not in st.session_state:
    st.session_state["q"] = ""

st.write("Try an example:")
for ex in EXAMPLES:
    if st.button(ex):
        st.session_state["q"] = ex

question = st.text_input("Or ask your own question", key="q")

left = MAX_QUERIES_PER_SESSION - st.session_state["count"]
st.caption(f"Demo queries left in this session: {max(left, 0)}")

if st.button("Run agent", type="primary") and question.strip():
    if left <= 0:
        st.warning("Demo limit reached. Run it locally from the GitHub repo for unlimited queries.")
    else:
        st.session_state["count"] += 1
        try:
            with st.spinner("Agent working..."):
                t0 = time.perf_counter()
                result = agent.ask(get_app(), question.strip())
                elapsed = time.perf_counter() - t0
        except Exception as e:
            st.error(f"The model API failed: {type(e).__name__}. Please try again in a moment.")
        else:
            st.subheader("Answer")
            st.write(result.answer.replace("$", "\\$"))  # stop Streamlit treating $...$ as math

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("LLM steps", result.steps)
            c2.metric("SQL calls", result.sql_calls)
            c3.metric("SQL errors", result.sql_errors)
            c4.metric("Time (s)", f"{elapsed:.1f}")

            with st.expander("Agent trace"):
                st.write("**Tools used (in order):**", " → ".join(result.tools_used) or "none")
                st.write("**Forced final answer (step cap hit):**", result.forced_final)
                if result.last_sql:
                    st.write("**Last SQL:**")
                    st.code(result.last_sql, language="sql")