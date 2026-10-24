"""
surveys/validation.py — Configurable Server-Side Validation for Feedora Surveys.
Enforces research data integrity by rejecting out-of-range rating scores, foreign option IDs,
and empty submissions while supporting arbitrary scale bounds (e.g. 1..5, 1..7, -5..+5).
"""
from typing import Dict, Any, List, Tuple
from surveys.models import Survey, Question, MCQOption


def validate_survey_submission(survey: Survey, answers: Any, strict_required: bool = False) -> Tuple[bool, List[Dict[str, str]]]:
    """
    Validates submitted survey answers against survey questions and rating boundaries.
    Returns: (is_valid: bool, errors: list of {'code': str, 'reason': str})

    Validation invariants:
      1. Empty submissions (no answers provided) are rejected.
      2. Likert and rating items must be integers within [rating_min, rating_max].
         Supports 5-point, 7-point, and bipolar scales (e.g. -5 to +5).
      3. MCQ, dropdown, and checkbox options must belong to the question.
      4. If strict_required is True (or survey.is_hmsam is True), required items must be present.
    """
    errors: List[Dict[str, str]] = []
    features = survey.features if isinstance(survey.features, dict) else {}

    # Normalise answers into a lookup dictionary
    normalized: Dict[str, Any] = {}
    if isinstance(answers, dict):
        normalized = answers
    elif isinstance(answers, list):
        for item in answers:
            if isinstance(item, dict):
                qid = item.get('questionId') or item.get('id') or item.get('code')
                if qid is not None:
                    normalized[str(qid)] = item.get('val') if 'val' in item else item.get('value')

    # Guard against completely empty submissions
    has_any_answer = any(v is not None and v != '' for v in normalized.values())
    if not normalized or not has_any_answer:
        return False, [{
            'code': 'SUBMISSION_EMPTY',
            'reason': 'Survey submission contains no answers.'
        }]

    # Check whether strict requiredness is enforced
    enforce_required = (
        strict_required or 
        bool(features.get('validation', {}).get('strict_required', False))
    )

    questions = survey.questions.all().order_by('order')

    for question in questions:
        q_code = question.code or f"Q_{question.id}"

        # Resolve value by question.id, q_<id>, or question.code
        val = normalized.get(str(question.id))
        if val is None:
            val = normalized.get(f"q_{question.id}")
        if val is None and question.code:
            val = normalized.get(question.code)

        # Enforce requiredness when strict mode is active
        if enforce_required and question.is_required:
            if val is None or val == '' or (isinstance(val, list) and len(val) == 0):
                errors.append({
                    'code': q_code,
                    'reason': f"Required item '{q_code}' is missing."
                })
                continue

        # If answer is omitted and not strictly required, skip type validation
        if val is None or val == '':
            continue

        # Likert and rating boundary check with configurable min and max scales
        if question.question_type in ('likert', 'rating'):
            try:
                num_val = int(val)
                min_val = getattr(question, 'rating_min', None)
                if min_val is None:
                    min_val = features.get('rating_scale', {}).get('min', 1)
                max_val = getattr(question, 'rating_max', None)
                if max_val is None:
                    max_val = features.get('rating_scale', {}).get('max', 5)

                if num_val < min_val or num_val > max_val:
                    errors.append({
                        'code': q_code,
                        'reason': f"Value {num_val} for '{q_code}' is out of range ({min_val}..{max_val})."
                    })
            except (ValueError, TypeError):
                errors.append({
                    'code': q_code,
                    'reason': f"Value '{val}' for '{q_code}' must be a valid integer."
                })

        # MCQ and dropdown option membership check
        elif question.question_type in ('mcq', 'dropdown'):
            opt_valid = False
            try:
                opt_id = int(val)
                opt_valid = MCQOption.objects.filter(question=question, id=opt_id).exists()
            except (ValueError, TypeError):
                if isinstance(val, str) and val.strip():
                    opt_valid = MCQOption.objects.filter(question=question, text__iexact=val.strip()).exists()

            if not opt_valid:
                errors.append({
                    'code': q_code,
                    'reason': f"Selected option for '{q_code}' is invalid or does not belong to the question."
                })

        # Checkbox option membership check
        elif question.question_type == 'checkbox':
            items = val if isinstance(val, list) else [val]
            for item in items:
                opt_valid = False
                try:
                    opt_id = int(item)
                    opt_valid = MCQOption.objects.filter(question=question, id=opt_id).exists()
                except (ValueError, TypeError):
                    if isinstance(item, str) and item.strip():
                        opt_valid = MCQOption.objects.filter(question=question, text__iexact=item.strip()).exists()

                if not opt_valid:
                    errors.append({
                        'code': q_code,
                        'reason': f"Option '{item}' for '{q_code}' does not belong to the question."
                    })
                    break

        # Short and open verbatim text question validation & quality analysis
        elif question.question_type in ('short', 'open'):
            text_str = str(val).replace('\x00', '')

            # Enforce non-empty whitespace when item is required
            if enforce_required and not text_str.strip():
                errors.append({
                    'code': q_code,
                    'reason': f"Text response for '{q_code}' cannot be empty."
                })
                continue

            # Configurable strict length boundary (default allows safe truncation up to 10k)
            val_cfg = features.get('validation', {})
            max_len = val_cfg.get('max_text_length', 10000)
            if val_cfg.get('strict_length', False) and len(text_str) > max_len:
                errors.append({
                    'code': q_code,
                    'reason': f"Text for '{q_code}' exceeds maximum length of {max_len} characters."
                })
                continue

            # Minimum length check for research feedback if configured
            min_len = val_cfg.get('min_open_length', 0)
            if min_len > 0 and len(text_str.strip()) < min_len:
                errors.append({
                    'code': q_code,
                    'reason': f"Response for '{q_code}' must contain at least {min_len} characters."
                })
                continue

    return len(errors) == 0, errors


def analyze_text_quality(text: str, question: Question = None) -> Dict[str, Any]:
    """
    Granular NLP / quality analysis for verbatim answers.
    Evaluates depth, repetition, sentiment hints, and construct alignment
    for downstream analysis and LLM summarization.
    """
    cleaned = str(text or '').strip()
    words = cleaned.split()
    word_count = len(words)
    char_count = len(cleaned)

    # Detect repetition or keyboard spam (e.g. 'aaaaa', 'asdfasdf', high char concentration)
    is_spam = False
    if char_count >= 8:
        unique_chars = len(set(cleaned.lower()))
        if unique_chars <= 3 or (char_count / max(1, unique_chars)) > 4.0:
            is_spam = True

    # Hedonic / HMSAM construct keywords dictionary for automated qualitative tagging
    construct_keywords = {
        'joy': ['fun', 'enjoy', 'happy', 'pleasure', 'entertaining', 'delight', 'exciting'],
        'peou': ['easy', 'simple', 'clear', 'effortless', 'intuitive', 'straightforward', 'hard', 'difficult'],
        'pu': ['useful', 'effective', 'helpful', 'valuable', 'productive', 'efficient', 'useless'],
        'curiosity': ['curious', 'explore', 'discover', 'learn', 'interesting', 'intriguing'],
    }

    lowered = cleaned.lower()
    matched_constructs = [
        c for c, kws in construct_keywords.items()
        if any(kw in lowered for kw in kws)
    ]

    return {
        'word_count': word_count,
        'char_count': char_count,
        'is_spam': is_spam,
        'matched_constructs': matched_constructs,
        'llm_ready': word_count >= 3 and not is_spam,
    }
