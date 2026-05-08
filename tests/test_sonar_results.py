import json
from types import SimpleNamespace

import pytest

from agent import clean_sonar_results


def make_raw_results(payload):
    return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(payload))])


def test_clean_sonar_results_accepts_sonarqube_dict_payload():
    raw_results = make_raw_results({
        "issues": [
            {
                "key": "S1",
                "severity": "MAJOR",
                "message": "Avoid hardcoded value",
                "textRange": {"startLine": 8},
                "component": "project:src/main/App.java",
            }
        ]
    })

    assert clean_sonar_results(raw_results) == [
        {
            "sonar_key": "S1",
            "severity": "MAJOR",
            "message": "Avoid hardcoded value",
            "line": 8,
            "file": "src/main/App.java",
        }
    ]


def test_clean_sonar_results_accepts_raw_issue_list_payload():
    raw_results = make_raw_results([
        {
            "key": "S1",
            "severity": "CRITICAL",
            "message": "Fix this",
            "component": "project:service.py",
        }
    ])

    assert clean_sonar_results(raw_results)[0] == {
        "sonar_key": "S1",
        "severity": "CRITICAL",
        "message": "Fix this",
        "line": 0,
        "file": "service.py",
    }


def test_clean_sonar_results_rejects_unexpected_payload_shape():
    raw_results = make_raw_results({"issues": {"not": "a list"}})

    with pytest.raises(ValueError, match="Unexpected SonarQube issues format"):
        clean_sonar_results(raw_results)
