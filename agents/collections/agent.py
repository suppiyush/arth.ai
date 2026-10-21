"""Arth Collections: dues, reminders and invoice payment status. python -m agents.collections.agent"""

from agents.collections import logic
from core.net import make_agent
from core.specialist import serve

agent = make_agent("collections", "Arth Collections",
                   description="Collections for Arth: udhaar list, reminders with UPI links, invoice payment status.")
serve(agent, "collections", plan=logic.plan, commit=logic.commit, query=logic.query, undo=logic.undo)

if __name__ == "__main__":
    agent.run()
