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


def calculate_variable_reward(base_points, survey_features=None, seed=None):
    """
    Optional Variable Rewards Engine (Skinnerian Variable-Ratio Schedule).
    When survey.features['rewards']['mode'] == 'variable', respondents have a chance
    to trigger a surprise dopaminergic boost multiplier (1.2x - 2.0x).
    Configurable and purely optional: defaults to fixed rewards if not enabled.
    """
    features = survey_features or {}
    rewards_config = features.get('rewards', {})
    if not rewards_config.get('enabled', False) or rewards_config.get('mode') != 'variable':
        return 0, 1.0, None

    import random
    rng = random.Random(seed) if seed is not None else random
    # 40% probability of surprise insight boost
    probability = float(rewards_config.get('probability', 0.40))
    if rng.random() < probability:
        min_mult = float(rewards_config.get('minMultiplier', 1.2))
        max_mult = float(rewards_config.get('maxMultiplier', 2.0))
        multiplier = round(rng.uniform(min_mult, max_mult), 1)
        bonus = max(1, int(round(base_points * (multiplier - 1.0))))
        label = f"🎲 Lucky Voice Boost ({multiplier}x)"
        return bonus, multiplier, label

    return 0, 1.0, None


def evaluate_negative_reinforcement(survey_features=None, responses_count=1, completed_clean=True):
    """
    Optional Negative Reinforcement Engine.
    In operant conditioning, negative reinforcement increases target behavior by
    REMOVING an aversive stimulus (friction, nagging banners, rank decay) upon completion.
    Configurable via survey.features['negative_reinforcement'].
    Returns active perks including friction removal and institutional streak shield.
    """
    features = survey_features or {}
    nr_config = features.get('negative_reinforcement', {})
    if not nr_config.get('enabled', False):
        return {
            'enabled': False,
            'friction_removed': False,
            'streak_shielded': False,
            'perk_description': None,
        }

    friction_removed = bool(nr_config.get('friction_removal', True)) and completed_clean
    streak_shielded = bool(nr_config.get('streak_shield', True)) and responses_count >= 1

    perk_desc = []
    if friction_removed:
        perk_desc.append("Survey friction & verification gates removed")
    if streak_shielded:
        perk_desc.append("🛡️ Institutional Leaderboard Shield activated (no rank decay for 7 days)")

    return {
        'enabled': True,
        'friction_removed': friction_removed,
        'streak_shielded': streak_shielded,
        'perk_description': " · ".join(perk_desc) if perk_desc else "Friction-free respondent status granted",
    }

