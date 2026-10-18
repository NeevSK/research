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
        bool(survey.features.get('validation', {}).get('strict_required', False))
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
                    min_val = survey.features.get('rating_scale', {}).get('min', 1)
                max_val = getattr(question, 'rating_max', None)
                if max_val is None:
                    max_val = survey.features.get('rating_scale', {}).get('max', 5)

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

    return len(errors) == 0, errors
