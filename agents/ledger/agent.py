"""Arth Ledger: the khata. python -m agents.ledger.agent"""

from agents.ledger import logic
from core.net import make_agent
from core.specialist import serve

agent = make_agent("ledger", "Arth Ledger",
                   description="Khata for Arth: sales, purchases, expenses, payments and party balances.")
serve(agent, "ledger", plan=logic.plan, commit=logic.commit, query=logic.query, undo=logic.undo)

if __name__ == "__main__":
    agent.run()
