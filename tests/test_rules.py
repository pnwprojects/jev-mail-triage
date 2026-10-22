import pytest
from pydantic import ValidationError

from tests.fakes_rules import FakeRulesStore
from triage.config import load_config
from triage.rules import Rules, RulesLoader, apply_rules, diff, rules_from_config


@pytest.fixture
def base(monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    return rules_from_config(load_config())


def edit(rules, **changes):
    return Rules.model_validate({**rules.model_dump(), **changes})


def test_env_defaults_round_trip(base):
    cfg = load_config()
    assert apply_rules(cfg, base) == cfg


def test_apply_rules_changes_policy_only(base):
    cfg = load_config()
    new = edit(base, archive_threshold=0.8, protected_domains=["example.org"])
    out = apply_rules(cfg, new)
    assert out.archive_threshold == 0.8 and out.protected_domains == ("example.org",)
    assert (out.gmail_user, out.jev_base_url, out.label_prefix) == (cfg.gmail_user, cfg.jev_base_url, cfg.label_prefix)


def test_env_dry_run_wins(base, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    assert apply_rules(load_config(), edit(base, dry_run=False)).dry_run is True


@pytest.mark.parametrize("changes", [
    {"gmail_user": "someone@else.com"},          # infrastructure fields don't exist here
    {"jev_base_url": "https://evil.example"},
    {"archive_threshold": 1.5},
    {"spam_threshold": 0.8},                      # below archive (0.9)
    {"archive_categories": ["Nope"]},
    {"spam_category": "Nope"},
    {"archive_categories": ["Junk"]},             # Spam category can't also archive
    {"protected_domains": ["not a domain"]},
    {"jev_model": "gpt-4"},
    {"categories": []},
    {"categories": [{"name": "<script>", "description": "x"}]},
])
def test_invalid_rules_rejected(base, changes):
    with pytest.raises(ValidationError):
        edit(base, **changes)


def test_duplicate_category_names_rejected(base):
    cats = base.model_dump()["categories"]
    with pytest.raises(ValidationError):
        edit(base, categories=cats + [{"name": cats[0]["name"].upper(), "description": "x"}])


def test_diff_reports_changes(base):
    cats = base.model_dump()["categories"]
    cats[0]["description"] = "new words"
    rows = diff(base, edit(base, categories=cats, spam_threshold=0.97))
    fields = [r[0] for r in rows]
    assert f"Category {cats[0]['name']}" in fields and "spam_threshold" in fields


def test_loader_reads_version_once_and_keeps_it_when_firestore_fails(base):
    store = FakeRulesStore(base)
    loader = RulesLoader(store)
    assert loader.current().number == 1
    store.broken = True
    assert loader.current().number == 1


def test_loader_fails_on_cold_start_when_firestore_is_down(base):
    store = FakeRulesStore(base)
    store.broken = True
    with pytest.raises(RuntimeError):
        RulesLoader(store).current()


def test_loader_without_ruleset_means_env(base):
    assert RulesLoader(FakeRulesStore()).current() is None


def test_loader_picks_up_new_version(base):
    store = FakeRulesStore(base)
    loader = RulesLoader(store)
    loader.current()
    store.save(edit(base, archive_threshold=0.85), based_on=1, editor="e")
    assert loader.current().rules.archive_threshold == 0.85


def test_sender_rules_validate_and_match(base):
    from triage.rules import SenderRule
    rules = edit(base, sender_rules=[{"sender": "Boss@Work.com", "category": "FYI"},
                                     {"sender": "@shop.example", "category": "Marketing"}])
    assert [r.sender for r in rules.sender_rules] == ["boss@work.com", "shop.example"]
    assert SenderRule(sender="shop.example", category="x").matches("a@mail.shop.example")
    assert not SenderRule(sender="shop.example", category="x").matches("a@notshop.example")
    for bad in ([{"sender": "x@y.com", "category": "Nope"}],
                [{"sender": "not an address", "category": "FYI"}],
                [{"sender": "a@b.com", "category": "FYI"}, {"sender": "a@b.com", "category": "Junk"}]):
        with pytest.raises(ValidationError):
            edit(base, sender_rules=bad)


def test_sender_rules_reach_config_and_diff(base):
    new = edit(base, sender_rules=[{"sender": "a@b.com", "category": "FYI"}])
    assert apply_rules(load_config(), new).sender_rules == (("a@b.com", "FYI"),)
    assert ("Sender rule a@b.com", "(none)", "FYI") in diff(base, new)


@pytest.mark.parametrize("pattern, address, hit", [
    ("*.candidateforcongress.example", "info@candidateforcongress.example", True),
    ("*.candidateforcongress.example", "info@mail.candidateforcongress.example", True),
    ("*.candidateforcongress.example", "info@notcandidateforcongress.example", False),
    ("candidateforcongress.example", "info@e.candidateforcongress.example", True),       # plain domain: unchanged
    ("*congress.example", "info@candidateforcongress.example", True),
    ("*congress.example", "info@congress.org", False),
    ("alerts-*@bank.com", "alerts-card@bank.com", True),
    ("alerts-*@bank.com", "alerts@bank.com", False),
    ("*@news.example.com", "anyone@news.example.com", True),
    ("boss@work.com", "Boss@Work.com", True),
])
def test_sender_pattern_matching(pattern, address, hit):
    from triage.rules import sender_pattern_matches
    assert sender_pattern_matches(pattern, address) is hit


@pytest.mark.parametrize("pattern", ["*", "*.com", "*@*", "*@*.com", "?.org", "a@*", "*.c*m", "[ab].com"])
def test_too_broad_patterns_rejected(pattern):
    from triage.rules import SenderRule
    with pytest.raises(ValidationError):
        SenderRule(sender=pattern, category="FYI")


def test_most_specific_rule_wins():
    from triage.rules import best_sender_rule
    rules = [("*.bank.com", "Marketing"), ("alerts-*@bank.com", "Notifications"), ("alerts-card@bank.com", "FYI"),
             ("*.mail.bank.com", "Junk")]
    assert best_sender_rule(rules, "alerts-card@bank.com") == ("alerts-card@bank.com", "FYI")
    assert best_sender_rule(rules, "alerts-loan@bank.com") == ("alerts-*@bank.com", "Notifications")
    assert best_sender_rule(rules, "x@mail.bank.com") == ("*.mail.bank.com", "Junk")
    assert best_sender_rule(rules, "x@bank.com") == ("*.bank.com", "Marketing")
    assert best_sender_rule(rules, "x@other.com") is None
