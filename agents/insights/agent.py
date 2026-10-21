"""Arth Insights: business questions answered from the shop's own figures. python -m agents.insights.agent"""

from agents.insights import logic
from core.net import make_agent
from core.specialist import serve

agent = make_agent("insights", "Arth Insights",
                   description="Insights for Arth: sales, profit, top items and dues, explained in plain Hinglish.")
serve(agent, "insights", query=logic.query)

if __name__ == "__main__":
    agent.run()
