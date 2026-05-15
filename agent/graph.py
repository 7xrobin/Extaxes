"""
LangGraph StateGraph — Kyron investment agent.
"""
import sqlite3
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite import SqliteSaver
from django.conf import settings
from .state import AgentState
from . import nodes


def build_graph():
    builder = StateGraph(AgentState)

    builder.add_node("intake",   nodes.intake_node)
    builder.add_node("upload",   nodes.upload_node)
    builder.add_node("analysis", nodes.analysis_node)
    builder.add_node("plan",     nodes.plan_node)
    builder.add_node("approval", nodes.approval_node)
    builder.add_node("qa",       nodes.qa_node)
    builder.add_node("digest",   nodes.digest_node)

    builder.set_entry_point("intake")

    builder.add_conditional_edges("intake", nodes.route_after_intake, {
        "continue_intake": "intake",
        "upload":          "upload",
    })
    builder.add_edge("upload",   "analysis")
    builder.add_edge("analysis", "plan")
    builder.add_edge("plan",     "qa")      # plan goes directly to qa
    builder.add_edge("approval", "qa")      # after save, return to qa loop
    builder.add_conditional_edges("qa", nodes.route_after_qa, {
        "qa":       "qa",        # default: loop back
        "plan":     "plan",      # user wants to revise strategy
        "approval": "approval",  # user approved — save to DB (no interrupt)
    })
    builder.add_edge("digest", END)

    # Open a persistent connection — check_same_thread=False required for Django
    conn = sqlite3.connect(settings.LANGGRAPH_DB_PATH, check_same_thread=False)
    memory = SqliteSaver(conn)
    return builder.compile(
        checkpointer=memory,
        interrupt_before=["intake", "qa"],
    )


graph = build_graph()
