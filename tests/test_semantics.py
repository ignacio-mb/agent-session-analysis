"""Questions mapped to data-engineering topics and layers (semantics/questions.json)."""

from session_analytics import semantics


def c(header, question, topic=None):
    r = semantics.load().classify(header, question, topic)
    return r["de_topic"], r["layer"], r["semantics_by"]


def test_header_first_then_question_then_fallback():
    # The header is the agent's own label: it decides before the question text.
    assert c("Trust label", "Should Revenue move from 'Draft' to 'Self-consistent only'?")[:2] == ("ownership", "semantic")
    assert c("Parity scope", "How faithful should raw_mailchimp be?")[:2] == ("quality", "source")
    assert c("Dashboard", "Where should the churn charts go?")[:2] == ("delivery", "presentation")
    assert c("Churn signal", "What should mark an account as churned?")[:2] == ("business-logic", "semantic")
    assert c("Where tables live", "The Sample Database is read-only. Where should the cleaner table structure live?")[:2] \
        == ("modeling", "modeling")
    assert c("Environment", "Is this instance a sandbox, or does its work go to production?")[:2] == ("platform", "platform")
    assert c("Auth flow", "Which Salesforce auth flow should the connector use?")[:2] == ("privacy", "source")
    assert c("Files", "Keep a copy of the working files, the SQL behind every Model?")[:2] == ("workflow", "cross-cutting")
    assert c(None, "Want me to add a global date-range filter?", "offer")[:2] == ("requirements", "presentation")
    assert c(None, "Hmm?", "sign-off") == ("ownership", "semantic", "fallback/fallback")


def test_dimensions_keep_the_file_order():
    topics, layers = semantics.load().dimensions()
    assert topics[0]["id"] == "privacy" and topics[-1]["id"] == "other"
    assert [x["id"] for x in layers][-1] == "cross-cutting" and all(x["label"] for x in layers)
