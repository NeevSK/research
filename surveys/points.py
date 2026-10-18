"""
surveys/points.py — Authoritative Point Calculation Rules for Feedora.
Provides a configurable, centralized point evaluation engine used across web views and REST APIs.
"""

# Default point allocations per question type
DEFAULT_POINTS = {
    'likert':   10,
    'rating':   10,
    'mcq':      10,
    'checkbox': 10,
    'dropdown': 10,
    'short':    10,
    'open':     20,
    'date':     5,
    'file':     15,
}


def calculate_question_points(question_type, survey_features=None, answer_text='', default_enabled=True):
    """
    Calculate points earned for answering a question.
    Honors survey-level custom point configurations from survey.features['points'].
    Configurable via default_enabled (defaults to True, can be explicitly disabled by creator).
    """
    features = survey_features or {}
    points_config = features.get('points', {})
    points_enabled = points_config.get('enabled', default_enabled)

    if not points_enabled:
        return 0

    custom_values = points_config.get('values', {})
    base = custom_values.get(question_type, DEFAULT_POINTS.get(question_type, 10))

    # Detailed qualitative feedback (>20 characters) earns full open-ended point value
    if question_type in ('short', 'open') and len(answer_text) > 20:
        base = custom_values.get('open', DEFAULT_POINTS['open'])

    return base
