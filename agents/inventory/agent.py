"""Arth Inventory: stock levels and movements. python -m agents.inventory.agent"""

from agents.inventory import logic
from core.net import make_agent
from core.specialist import serve

agent = make_agent("inventory", "Arth Inventory",
                   description="Stock for Arth: stock movements, oversell checks and low-stock alerts.")
serve(agent, "inventory", plan=logic.plan, commit=logic.commit, query=logic.query, undo=logic.undo)

if __name__ == "__main__":
    agent.run()
