from audit_tools import calculate_risk_score, normalize_user_id


def test_normalize_user_id():
    assert normalize_user_id("  USER-1 ") == "user-1"


def test_calculate_risk_score():
    assert calculate_risk_score(2, 1) == 9

