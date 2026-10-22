"""In-memory stand-in for triage.rules.RulesStore."""

from datetime import datetime, timezone

from triage.rules import RulesConflict, Version


class FakeRulesStore:
    def __init__(self, rules=None):
        self.versions = {}
        self.active_number = None
        self.reads = 0
        self.broken = False
        if rules is not None:
            self.save(rules, based_on=None, editor="seed@corp.example", note="seed")

    def active_version(self):
        self.reads += 1
        if self.broken:
            raise RuntimeError("firestore down")
        return self.active_number

    def get(self, number):
        return self.versions.get(number)

    def active(self):
        return self.versions.get(self.active_number)

    def history(self, limit=50):
        return sorted(self.versions.values(), key=lambda v: -v.number)[:limit]

    def save(self, rules, based_on, editor, note=""):
        if based_on != self.active_number:
            raise RulesConflict("moved")
        number = (self.active_number or 0) + 1
        self.versions[number] = Version(number, rules, editor, note, datetime.now(timezone.utc), based_on)
        self.active_number = number
        return number


class FakeReviewStore:
    """In-memory stand-in for triage.review.ReviewStore."""

    def __init__(self):
        self.filings_by_id = {}
        self.corrections = {}
        self.suggestions = {}
        self.budget = {}

    def budget_state(self):
        return dict(self.budget)

    def set_budget_state(self, state):
        self.budget = dict(state)

    def record_filing(self, filing):
        self.filings_by_id[filing.message_id] = filing

    def filings(self, since, category=None, limit=300):
        from dataclasses import replace
        out = [replace(f, correction=self.corrections.get(f.message_id, {}))
               for f in self.filings_by_id.values()
               if f.filed_at >= since and (category is None or f.category == category)]
        return sorted(out, key=lambda f: f.filed_at, reverse=True)[:limit]

    def search(self, since, words, category=None, limit=300):
        words = [w.lower() for w in words if w.strip()]
        return [f for f in self.filings(since, category, limit=10_000)
                if all(w in f"{f.sender} {f.subject}".lower() for w in words)][:limit]

    def counts(self, since):
        counts = {}
        for f in self.filings_by_id.values():
            if f.filed_at >= since:
                counts[f.category] = counts.get(f.category, 0) + 1
        return counts

    def get_filing(self, message_id):
        from dataclasses import replace
        f = self.filings_by_id.get(message_id)
        return replace(f, correction=self.corrections.get(message_id, {})) if f else None

    def request_correction(self, message_id, category, sender_rule, editor):
        self.corrections[message_id] = {"category": category, "sender_rule": sender_rule,
                                        "requested_by": editor, "status": "pending", "error": ""}

    def pending_corrections(self):
        from triage.review import Correction
        return [Correction(m, c["category"], c["sender_rule"], c["requested_by"])
                for m, c in self.corrections.items() if c["status"] == "pending"]

    def finish_correction(self, message_id, status, error=""):
        from dataclasses import replace
        self.corrections[message_id].update(status=status, error=error)
        if status == "applied" and message_id in self.filings_by_id:
            self.filings_by_id[message_id] = replace(
                self.filings_by_id[message_id], corrected_to=self.corrections[message_id]["category"],
                mail_labels=None)

    def record_move(self, message_id, labels):
        from dataclasses import replace
        labels = sorted(labels)
        category = labels[0] if len(labels) == 1 else ""
        self.filings_by_id[message_id] = replace(self.filings_by_id[message_id], mail_labels=labels,
                                                 corrected_to=category)
        self.corrections[message_id] = {"category": category, "labels": labels, "sender_rule": False,
                                        "requested_by": "moved in mail", "source": "moved in mail",
                                        "status": "applied", "error": ""}

    def suggest_rule(self, sender, category, message_id):
        s = self.suggestions.get(sender)
        if s and s["category"] == category:
            if message_id not in s["message_ids"]:
                s["message_ids"].append(message_id)
            return
        self.suggestions[sender] = {"sender": sender, "category": category, "message_ids": [message_id],
                                    "status": "open"}

    def open_suggestions(self, limit=20):
        out = [{"sender": s["sender"], "category": s["category"], "count": len(s["message_ids"])}
               for s in self.suggestions.values() if s["status"] == "open"]
        return sorted(out, key=lambda d: (-d["count"], d["sender"]))[:limit]

    def get_suggestion(self, sender):
        s = self.suggestions.get(sender)
        return dict(s) if s else None

    def close_suggestion(self, sender, status, editor):
        self.suggestions[sender].update(status=status, closed_by=editor)

    def metrics_records(self, since):
        filings = [dict(filed_at=f.filed_at, category=f.category, action=f.action, confidence=f.confidence,
                        note=f.note, sender=f.sender, id=f.message_id)
                   for f in self.filings_by_id.values() if f.filed_at >= since]
        corrections = {k: dict(v) for k, v in self.corrections.items()}
        return filings, corrections, sum(1 for s in self.suggestions.values() if s["status"] == "open")
