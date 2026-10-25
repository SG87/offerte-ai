"""Outgoing integrations. In v1 these are placeholders with a real seam:

- mail:   OutboxMessage rows; sent over SMTP when OFFERTE_SMTP_HOST is set, otherwise they wait in the outbox.
- events: IntegrationEvent rows; POSTed to OFFERTE_WEBHOOK_URL when set, otherwise an agent pulls them via the API.
- stock:  material requirements of a quote vs. StockLevel rows (kept by hand until a stock agent takes over).
"""
