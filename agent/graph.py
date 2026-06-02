"""
LangGraph StateGraph — Kyron investment agent.

Agent tools are first-class nodes here: `fetch_prices` (live prices), `retrieve`
(tax-source RAG) and `simulate` (growth projection) each run as their own graph
step and LangSmith span, feeding the deterministic/answer nodes around them.
"""
import sqlite3
import logging
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite import SqliteSaver
from django.conf import settings
from .state import AgentState
from . import nodes

logger = logging.getLogger("agent.graph")


def build_graph():
    builder = StateGraph(AgentState)

    # Conversational / deterministic nodes
    builder.add_node("intake",   nodes.intake_node)
    builder.add_node("upload",   nodes.upload_node)
    builder.add_node("analysis", nodes.analysis_node)
    builder.add_node("plan",     nodes.plan_node)
    builder.add_node("approval", nodes.approval_node)
    builder.add_node("qa",       nodes.qa_node)        # router at the interrupt point
    builder.add_node("answer",   nodes.answer_node)    # LLM reply after tools run
    builder.add_node("digest",   nodes.digest_node)

    # Tool nodes — agent capabilities promoted to explicit graph steps
    builder.add_node("fetch_prices", nodes.fetch_prices_node)
    builder.add_node("retrieve",     nodes.retrieve_node)
    builder.add_node("simulate",     nodes.simulate_node)

    builder.set_entry_point("intake")

    builder.add_conditional_edges("intake", nodes.route_after_intake, {
        "continue_intake": "intake",
        "upload":          "upload",
    })
    builder.add_edge("upload",       "fetch_prices")  # pull live prices as a tool step
    builder.add_edge("fetch_prices", "analysis")
    builder.add_edge("analysis",     "plan")
    builder.add_edge("plan",         "qa")            # plan goes directly to qa
    builder.add_edge("approval",     "qa")            # after save, return to qa loop

    builder.add_conditional_edges("qa", nodes.route_after_qa, {
        "answer":   "retrieve",  # question → RAG → projection → answer pipeline
        "plan":     "plan",      # user wants to revise strategy
        "approval": "approval",  # user approved — save to DB (no interrupt)
    })

    # Tool pipeline feeding the answer node, then back to the qa interrupt.
    builder.add_edge("retrieve", "simulate")
    builder.add_edge("simulate", "answer")
    builder.add_edge("answer",   "qa")

    builder.add_edge("digest", END)

    # Open a persistent connection — check_same_thread=False required for Django
    conn = sqlite3.connect(settings.LANGGRAPH_DB_PATH, check_same_thread=False)
    memory = SqliteSaver(conn)
    compiled = builder.compile(
        checkpointer=memory,
        interrupt_before=["intake", "qa"],
    )
    logger.info("LangGraph compiled with %d nodes", len(builder.nodes))
    return compiled


graph = build_graph()
