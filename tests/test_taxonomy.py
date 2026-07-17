from faria.domain.taxonomy import (
    CATEGORY_OF,
    SEVERITY_ANCHORS,
    SUBCATEGORIES,
    ErrorCategory,
    ErrorSubcategory,
    Severity,
    render_for_prompt,
)


def test_every_subcategory_belongs_to_exactly_one_category():
    seen = [s for subs in SUBCATEGORIES.values() for s in subs]
    assert len(seen) == len(set(seen)), "subcategory assigned to two categories"
    assert set(seen) == set(ErrorSubcategory), "subcategory missing from SUBCATEGORIES"
    assert set(CATEGORY_OF) == set(ErrorSubcategory)


def test_every_category_has_subcategories():
    assert set(SUBCATEGORIES) == set(ErrorCategory)
    assert all(subs for subs in SUBCATEGORIES.values())


def test_severity_scale_is_anchored():
    assert [int(s) for s in Severity] == [1, 2, 3, 4]
    assert set(SEVERITY_ANCHORS) == set(Severity)


def test_prompt_rendering_covers_everything():
    text = render_for_prompt()
    for cat in ErrorCategory:
        assert cat.value in text
    for sub in ErrorSubcategory:
        assert sub.value in text
    for anchor in SEVERITY_ANCHORS.values():
        assert anchor in text
