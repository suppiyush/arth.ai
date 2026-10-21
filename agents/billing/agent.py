"""Arth Billing: GST invoices. python -m agents.billing.agent"""

from agents.billing import logic
from core.net import make_agent
from core.specialist import serve

agent = make_agent("billing", "Arth Billing",
                   description="GST billing for Arth: CGST/SGST or IGST split, invoice numbering per financial year.")
serve(agent, "billing", plan=logic.plan, commit=logic.commit, undo=logic.undo)

if __name__ == "__main__":
    agent.run()
