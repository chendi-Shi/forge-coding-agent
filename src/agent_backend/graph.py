import asyncio
import math
import os
import socket
import time
from datetime import UTC, datetime

from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from agent_backend.settings import AgentSettings


class AgentState(MessagesState):
    mock_delay_seconds: float
    probe: dict


@tool
async def current_time_utc() -> str:
    """Return the current time in UTC in ISO 8601 format."""
    return datetime.now(UTC).isoformat()


def build_graph(settings: AgentSettings | None = None):
    settings = settings or AgentSettings()
    settings.validate_runtime()
    builder = StateGraph(AgentState)

    if settings.agent_mode == "mock":
        async def mock_agent(state: AgentState) -> dict:
            delay = float(state.get("mock_delay_seconds", settings.mock_delay_seconds))
            if not math.isfinite(delay) or not 0 <= delay <= 60:
                raise ValueError("mock_delay_seconds must be finite and between 0 and 60")
            messages = state.get("messages", [])
            request_id = next((m.content for m in reversed(messages) if m.type == "human"), "")
            if not isinstance(request_id, str):
                raise ValueError("Mock probes require a text user message")
            started = time.time()
            await asyncio.sleep(delay)
            return {
                "messages": [AIMessage(content=f"Mock completed: {request_id}")],
                "probe": {
                    "started_at": started,
                    "finished_at": time.time(),
                    "instance": socket.gethostname(),
                    "request_id": request_id,
                },
            }

        builder.add_node("agent", mock_agent)
        builder.add_edge(START, "agent")
        builder.add_edge("agent", END)
    else:
        from langchain_openai import ChatOpenAI

        tools = [current_time_utc]
        model = ChatOpenAI(
            model=settings.llm_model,
            api_key=os.environ["OPENAI_API_KEY"],
            base_url=os.environ.get("OPENAI_BASE_URL") or None,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            max_tokens=settings.llm_max_tokens,
            streaming=True,
        ).bind_tools(tools)

        async def call_model(state: AgentState) -> dict:
            prompt = SystemMessage(content="You are a helpful assistant. Use the available tools when needed.")
            result = await model.ainvoke([prompt, *state["messages"]])
            return {"messages": [result]}

        builder.add_node("agent", call_model)
        builder.add_node("tools", ToolNode(tools))
        builder.add_edge(START, "agent")
        builder.add_conditional_edges("agent", tools_condition)
        builder.add_edge("tools", "agent")

    # Aegra injects its shared PostgreSQL checkpointer and store at runtime.
    return builder.compile()


graph = build_graph()
