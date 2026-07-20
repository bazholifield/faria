from faria.tutor.control import parse_control_call


def test_parse_switch_language():
    change = parse_control_call({
        "action": "switch_language",
        "language": "de",
        "correction_frequency": None,
        "correction_style": None,
        "pivot_frequency": None,
        "reason": "learner_request",
    })
    assert change.action == "switch_language"
    assert change.language == "de"
    assert change.reason == "learner_request"


def test_parse_tutor_pivot():
    change = parse_control_call({
        "action": "switch_language",
        "language": "fr",
        "correction_frequency": None,
        "correction_style": None,
        "pivot_frequency": None,
        "reason": "tutor_pivot",
    })
    assert change.reason == "tutor_pivot"
    assert change.language == "fr"


def test_parse_set_correction_frequency():
    change = parse_control_call({
        "action": "set_correction_frequency",
        "language": None,
        "correction_frequency": "aggressive",
        "correction_style": None,
        "pivot_frequency": None,
        "reason": "learner_request",
    })
    assert change.action == "set_correction_frequency"
    assert change.correction_frequency == "aggressive"
    assert change.language is None


def test_parse_defaults_reason_when_missing():
    change = parse_control_call({"action": "set_pivot_frequency", "pivot_frequency": "occasional"})
    assert change.reason == "learner_request"
    assert change.pivot_frequency == "occasional"
    assert change.language is None
