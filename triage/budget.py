"""Pause triage when the month's Google Cloud spend reaches the budget.

The $5 billing budget publishes to Pub/Sub several times a day, pushed to
gmail-triage /budget. Paused triage acknowledges Gmail notifications without
doing any work (no Jev calls) and doesn't advance the stored historyId, so on
resume it files what arrived meanwhile (Gmail keeps about a week of history;
older falls back to the 7-day Inbox search).

Resume from the rules console (no auto-pause again that billing month), or
automatically when a new billing month starts.

State (review database, mailboxes/<mailbox>.budget):
  {paused, period, cost, budget, currency, since?, resumed_period?, resumed_by?}
"""

from __future__ import annotations

from datetime import datetime, timezone


def on_budget_message(state: dict, msg: dict) -> dict | None:
    """New budget state for one budget notification, or None if nothing changes."""
    cost, budget = float(msg["costAmount"]), float(msg["budgetAmount"])
    period = msg["costIntervalStart"]
    facts = {"period": period, "cost": cost, "budget": budget, "currency": msg.get("currencyCode", "USD")}
    if state.get("paused"):
        if state.get("period") != period and cost < budget:
            return {**facts, "paused": False, "resumed_by": "new billing month",
                    "resumed_at": datetime.now(timezone.utc)}
        return None
    if cost >= budget and state.get("resumed_period") != period:
        return {**facts, "paused": True, "since": datetime.now(timezone.utc)}
    return None


def resumed(state: dict, editor: str) -> dict:
    """State after an editor resumes: no auto-pause again this billing month."""
    return {**state, "paused": False, "resumed_period": state.get("period"), "resumed_by": editor,
            "resumed_at": datetime.now(timezone.utc)}
