from faria.domain.taxonomy import ErrorCategory, ErrorSubcategory, Severity
from faria.tutor.schema import parse_annotated_errors


def test_parse_empty_errors_array():
    assert parse_annotated_errors({"errors": []}, utterance_id=1, detector="sidecar:test") == []


def test_parse_one_error_maps_all_fields():
    tool_input = {
        "errors": [
            {
                "span_text": "la problema",
                "category": "morphosyntax",
                "subcategory": "determiner",
                "severity": 2,
                "confidence": 0.92,
                "feature_key": "morphosyntax:determiner:el_problema",
                "correction": "el problema",
                "explanation": "problema is masculine despite the -a ending.",
                "interference_source": None,
                "handled": "not_addressed",
            }
        ]
    }
    events = parse_annotated_errors(tool_input, utterance_id=42, detector="sidecar:claude-sonnet-5")
    assert len(events) == 1
    e = events[0]
    assert e.utterance_id == 42
    assert e.category is ErrorCategory.MORPHOSYNTAX
    assert e.subcategory is ErrorSubcategory.DETERMINER
    assert e.severity is Severity.MINOR
    assert e.confidence == 0.92
    assert e.feature_key == "morphosyntax:determiner:el_problema"
    assert e.correction == "el problema"
    assert e.interference_source is None
    assert e.model_action == "not_addressed"
    assert e.detector == "sidecar:claude-sonnet-5"
    assert e.action == "logged_silent"  # default — decide() hasn't run yet


def test_parse_collapses_empty_interference_source():
    tool_input = {
        "errors": [
            {
                "span_text": "actualmente",
                "category": "lexis",
                "subcategory": "false_friend",
                "severity": 2,
                "confidence": 0.8,
                "feature_key": "lexis:false_friend:actualmente",
                "correction": "en este momento",
                "explanation": "actualmente means 'currently', not 'actually'.",
                "interference_source": "",
                "handled": "corrected_recast",
            }
        ]
    }
    events = parse_annotated_errors(tool_input, utterance_id=1, detector="sidecar:test")
    assert events[0].interference_source is None
