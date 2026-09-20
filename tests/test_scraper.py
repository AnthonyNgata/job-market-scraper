"""
Skill extraction.

The matcher is the one piece of logic whose failures are invisible end to end:
a broken pattern still yields a full dataset, just one where the insights are
quietly wrong. Every case here is a boundary the custom lookarounds exist to
handle — plain `\\b` gets several of them wrong.
"""

from src import scraper


def test_matches_a_plain_skill_case_insensitively():
    assert "Python" in scraper.extract_skills("Senior PYTHON Engineer")


def test_punctuated_names_survive():
    found = scraper.extract_skills("Node.js, CI/CD and scikit-learn")
    assert {"Node.js", "CI/CD", "scikit-learn"} <= set(found)


def test_java_does_not_match_inside_javascript():
    # `\b` would match "java" here, inflating Java on every frontend listing.
    found = scraper.extract_skills("JavaScript developer")
    assert "JavaScript" in found
    assert "Java" not in found


def test_ml_does_not_match_inside_html():
    assert "Machine Learning" not in scraper.extract_skills("HTML5 and CSS")


def test_go_needs_an_unambiguous_alias():
    # "Go" is deliberately not its own alias — the English verb is far too
    # common in job copy for it to mean the language.
    assert "Go" not in scraper.extract_skills("a great place to go to work")
    assert "Go" in scraper.extract_skills("golang microservices")


def test_longest_alias_wins():
    assert "GCP" in scraper.extract_skills("google cloud platform")


def test_empty_input_returns_empty_list():
    assert scraper.extract_skills(None, "", "   ") == []


def test_result_is_sorted_and_deduplicated():
    found = scraper.extract_skills("python", "Python", "SQL")
    assert found == sorted(found)
    assert len(found) == len(set(found))
