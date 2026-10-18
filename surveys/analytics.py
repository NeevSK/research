"""
surveys/analytics.py — Feedora
Complete analytics engine for survey dashboards.
"""

from .models import Survey, Answer, Response

# ── Construct metadata ─────────────────────────────────────
CONSTRUCT_LABELS = {
    'peou':     'Perceived Ease of Use',
    'pu':       'Perceived Usefulness',
    'joy':      'Joy',
    'curiosity':'Curiosity',
    'control':  'Control',
    'fi':       'Focused Immersion',
    'biu':      'Behavioural Intention to Use',
}

CONSTRUCT_ORDER = ['joy', 'curiosity', 'peou', 'pu', 'control', 'biu', 'fi']


# ── Score interpretation ───────────────────────────────────
def get_score_interpretation(score):
    """
    Per-construct interpretation based on percentage score.
    Adapted from the paper's 7-point Likert intervals to 5-point scale.
    """
    if score >= 84.1:
        return 'Strongly Agree'
    elif score >= 70.1:
        return 'Agree'
    elif score >= 56.1:
        return 'Somewhat Agree'
    elif score >= 42.1:
        return 'Neutral'
    elif score >= 28.1:
        return 'Somewhat Disagree'
    elif score >= 14.1:
        return 'Disagree'
    else:
        return 'Strongly Disagree'


def get_overall_interpretation(score):
    """
    Qualitative interpretation of the overall HMSAM adoption score.
    """
    if score >= 84.1:
        return {
            'level':       'Excellent adoption',
            'description': 'Users strongly agree the system is enjoyable, useful, '
                           'and easy to use. Hedonic adoption is very high.',
            'color':       '#0F6B4F',
            'bg':          '#dcfce7',
        }
    elif score >= 70.1:
        return {
            'level':       'Good adoption',
            'description': 'Users agree the system motivates continued use. '
                           'Hedonic qualities are well received.',
            'color':       '#1D9E75',
            'bg':          '#d1fae5',
        }
    elif score >= 56.1:
        return {
            'level':       'Moderate adoption',
            'description': 'Users somewhat agree the system is engaging. '
                           'There is room to improve hedonic motivation.',
            'color':       '#B87333',
            'bg':          '#fef3c7',
        }
    elif score >= 42.1:
        return {
            'level':       'Neutral adoption',
            'description': 'Users are neither motivated nor demotivated. '
                           'Significant improvements to engagement are needed.',
            'color':       '#d97706',
            'bg':          '#fef9c3',
        }
    else:
        return {
            'level':       'Low adoption',
            'description': 'Users do not find the system enjoyable or motivating. '
                           'Core hedonic design elements need to be reconsidered.',
            'color':       '#A04545',
            'bg':          '#fee2e2',
        }


# ── HMSAM construct scores ─────────────────────────────────
def get_hmsam_scores(survey, responses_qs=None):
    """
    Calculates HMSAM construct scores using the paper formula:
    Score = (weighted_sum / (max_scale × n)) × 100
    With 5-point scale: max_scale = 5.
    Returns (construct_scores dict, overall_score float).
    """
    questions = survey.questions.filter(
        hmsam_construct__isnull=False
    ).exclude(hmsam_construct='')

    construct_scores = {}

    for construct_key in CONSTRUCT_ORDER:
        qs = questions.filter(hmsam_construct=construct_key)
        if not qs.exists():
            continue

        ans_query = Answer.objects.filter(
            question__in=qs,
            likert_value__isnull=False,
        )
        if responses_qs is not None:
            ans_query = ans_query.filter(response__in=responses_qs)

        answers = ans_query.values_list('likert_value', flat=True)

        if not answers:
            continue

        values = list(answers)
        n      = len(values)

        # Scale detection: support 1..5, 1..7, 1..10, and bipolar scales (-5..+5)
        max_scale_val = max([q.rating_max for q in qs if q.rating_max] or [5])
        min_scale_val = min(values) if min(values) < 1 else 1

        scale_span = max(1, max_scale_val - min_scale_val)
        mean = round(sum(values) / n, 2)

        # Dual scoring (D2):
        # 1. Raw percentage score (paper formula): (weighted_sum / (max_scale * n)) * 100
        raw_percentage = round((sum(values) / (max_scale_val * n)) * 100, 1) if max_scale_val > 0 else 0.0
        # 2. Normalized interval percentage: ((mean - min) / (max - min)) * 100
        interval_score = round(max(0.0, min(100.0, ((mean - min_scale_val) / scale_span) * 100)), 1)

        # For standard 1..5 scale, raw_percentage is the original paper formula
        score = raw_percentage if (min_scale_val == 1 and max_scale_val == 5) else interval_score

        # Response distribution across all values in the scale
        dist_min = min(min_scale_val, min(values))
        dist_max = max(max_scale_val, max(values))
        distribution = {i: values.count(i) for i in range(dist_min, dist_max + 1)}

        construct_scores[construct_key] = {
            'label':            CONSTRUCT_LABELS[construct_key],
            'score':            score,
            'raw_percentage':   raw_percentage,
            'normalized_score': interval_score,
            'mean':             mean,
            'scale_min':        min_scale_val,
            'scale_max':        max_scale_val,
            'count':            n,
            'distribution':     distribution,
            'interpretation':   get_score_interpretation(score),
        }

    if construct_scores:
        overall = round(
            sum(c['score'] for c in construct_scores.values()) / len(construct_scores),
            1
        )
    else:
        overall = 0

    return construct_scores, overall


# ── Cronbach's alpha ───────────────────────────────────────
def cronbach_alpha(survey):
    """
    Calculates Cronbach's alpha for all Likert/rating questions.
    Requires minimum 2 questions and 2 complete responses.
    Returns (alpha float, interpretation string).
    """
    try:
        import numpy as np
    except ImportError:
        return None, 'numpy not installed — run: pip install numpy'
 
    # Get all likert AND rating questions (both use likert_value)
    questions = list(survey.questions.filter(
        question_type__in=['likert', 'rating']
    ).order_by('order'))
 
    # Need at least 10 questions for reliability analysis - just for testing the form - change to 10 later
    if len(questions) < 2:
        return None, 'Need at least 2 Likert/rating questions'
 
    responses = list(Response.objects.filter(survey=survey))
    if len(responses) < 2:
        return None, 'Need at least 2 responses'
 
    # Build matrix — only include complete rows (all questions answered)
    matrix = []
    for resp in responses:
        row = []
        for q in questions:
            ans = Answer.objects.filter(
                response=resp,
                question=q,
                likert_value__isnull=False
            ).first()
            if ans is None:
                row.append(None)
            else:
                row.append(ans.likert_value)
        # Only include rows with ALL answers present
        if None not in row:
            matrix.append(row)
 
    if len(matrix) < 2:
        return None, 'Need 2+ complete responses'
 
    arr = np.array(matrix, dtype=float)
    k   = arr.shape[1]  # number of questions
 
    if k < 2:
        return None, 'Need at least 2 questions'
 
    # Cronbach's alpha formula
    item_vars  = arr.var(axis=0, ddof=1)
    total_var  = arr.sum(axis=1).var(ddof=1)
 
    if total_var == 0:
        return 0.0, 'No variance in responses'
 
    alpha = round(float((k / (k - 1)) * (1 - item_vars.sum() / total_var)), 2)
    # Clamp to [-1, 1] range
    alpha = max(-1.0, min(1.0, alpha))
 
    if alpha >= 0.9:
        interp = 'Excellent reliability'
    elif alpha >= 0.8:
        interp = 'Good reliability'
    elif alpha >= 0.7:
        interp = 'Acceptable reliability'
    elif alpha >= 0.6:
        interp = 'Questionable reliability'
    elif alpha >= 0.5:
        interp = 'Poor reliability'
    elif alpha >= 0:
        interp = 'Unacceptable reliability'
    else:
        interp = 'Negative reliability — check reverse-coded or inconsistent items'
 
    return alpha, interp


# ── Per-construct Cronbach's Alpha (multidimensional reliability) ────
def get_hmsam_construct_reliabilities(survey, variant=None):
    """
    Computes Cronbach's alpha per individual HMSAM construct dimension.
    Optionally filters by experimental arm variant ('gamified' or 'control').
    """
    try:
        import numpy as np
    except ImportError:
        return {}

    responses_qs = Response.objects.filter(survey=survey)
    if variant:
        responses_qs = responses_qs.filter(variant=variant)
    responses = list(responses_qs)
    if len(responses) < 2:
        return {}

    results = {}
    for code, label in CONSTRUCT_LABELS.items():
        c_questions = list(survey.questions.filter(
            hmsam_construct__iexact=code,
            question_type__in=['likert', 'rating']
        ).order_by('order'))
        if len(c_questions) < 2:
            continue

        matrix = []
        for resp in responses:
            row = []
            for q in c_questions:
                ans = Answer.objects.filter(response=resp, question=q, likert_value__isnull=False).first()
                if ans is not None:
                    row.append(ans.likert_value)
                else:
                    row.append(None)
            if None not in row:
                matrix.append(row)

        if len(matrix) >= 2:
            arr = np.array(matrix, dtype=float)
            k = arr.shape[1]
            if k >= 2:
                item_vars = arr.var(axis=0, ddof=1)
                total_var = arr.sum(axis=1).var(ddof=1)
                if total_var > 0:
                    alpha = round(float((k / (k - 1)) * (1 - item_vars.sum() / total_var)), 3)
                    alpha = max(-1.0, min(1.0, alpha))
                    interp = 'High' if alpha >= 0.7 else ('Acceptable' if alpha >= 0.6 else 'Low')
                    results[code] = {
                        'label': label,
                        'itemsCount': k,
                        'sampleSize': len(matrix),
                        'alpha': alpha,
                        'interpretation': interp,
                    }
    return results


import math


def _student_t_pdf(x, df):
    """Probability density function of Student's t-distribution with df degrees of freedom."""
    c = math.exp(math.lgamma((df + 1.0) / 2.0) - math.lgamma(df / 2.0)) / math.sqrt(math.pi * df)
    return c * (1.0 + (x * x) / df) ** (-(df + 1.0) / 2.0)


def _student_t_cdf(t_val, df, steps=100):
    """Cumulative distribution function of Student's t using Simpson's quadrature."""
    if t_val == 0.0:
        return 0.5
    x_max = abs(t_val)
    h = x_max / float(steps)
    area = _student_t_pdf(0.0, df) + _student_t_pdf(x_max, df)
    for i in range(1, steps):
        x = i * h
        weight = 4.0 if i % 2 == 1 else 2.0
        area += weight * _student_t_pdf(x, df)
    integral = (h / 3.0) * area
    cdf = 0.5 + integral if t_val > 0.0 else 0.5 - integral
    return max(0.0, min(1.0, cdf))


def compute_welch_t_test(sample_a, sample_b):
    """
    Computes Welch's t-test (unequal variances) and Hedges' g effect size.
    Returns dict with t-stat, df, two-tailed p-value, Hedges' g, and significance label.
    """
    n_a = len(sample_a)
    n_b = len(sample_b)
    if n_a < 2 or n_b < 2:
        return {
            'tStatistic': None,
            'degreesOfFreedom': None,
            'pValue': None,
            'hedgesG': None,
            'effectSize': 'insufficient_data',
            'isSignificant': False,
            'significanceMarker': 'ns',
        }

    mean_a = sum(sample_a) / float(n_a)
    mean_b = sum(sample_b) / float(n_b)

    var_a = sum((x - mean_a) ** 2 for x in sample_a) / float(n_a - 1)
    var_b = sum((x - mean_b) ** 2 for x in sample_b) / float(n_b - 1)

    se_a = var_a / float(n_a)
    se_b = var_b / float(n_b)
    se_diff = math.sqrt(se_a + se_b)

    if se_diff == 0.0:
        return {
            'tStatistic': 0.0,
            'degreesOfFreedom': float(n_a + n_b - 2),
            'pValue': 1.0,
            'hedgesG': 0.0,
            'effectSize': 'negligible',
            'isSignificant': False,
            'significanceMarker': 'ns',
        }

    t_stat = (mean_a - mean_b) / se_diff

    # Welch–Satterthwaite degrees of freedom
    denom = 0.0
    if n_a > 1 and se_a > 0:
        denom += (se_a ** 2) / float(n_a - 1)
    if n_b > 1 and se_b > 0:
        denom += (se_b ** 2) / float(n_b - 1)

    df = ((se_a + se_b) ** 2) / denom if denom > 0 else float(n_a + n_b - 2)
    df = max(1.0, df)

    # Two-tailed p-value
    cdf = _student_t_cdf(abs(t_stat), df)
    p_val = round(2.0 * (1.0 - cdf), 4)

    # Pooled standard deviation & Hedges' g
    pooled_var = (((n_a - 1) * var_a) + ((n_b - 1) * var_b)) / float(n_a + n_b - 2)
    s_pooled = math.sqrt(pooled_var) if pooled_var > 0 else 0.0

    if s_pooled > 0:
        cohen_d = (mean_a - mean_b) / s_pooled
        # Small sample correction J
        j_corr = 1.0 - (3.0 / (4.0 * (n_a + n_b) - 9.0))
        hedges_g = round(cohen_d * j_corr, 3)
    else:
        hedges_g = 0.0

    abs_g = abs(hedges_g)
    if abs_g < 0.2:
        eff_size = 'negligible'
    elif abs_g < 0.5:
        eff_size = 'small'
    elif abs_g < 0.8:
        eff_size = 'medium'
    else:
        eff_size = 'large'

    is_sig = p_val < 0.05
    if p_val < 0.001:
        marker = '***'
    elif p_val < 0.01:
        marker = '**'
    elif p_val < 0.05:
        marker = '*'
    else:
        marker = 'ns'

    return {
        'tStatistic': round(t_stat, 3),
        'degreesOfFreedom': round(df, 1),
        'pValue': p_val,
        'hedgesG': hedges_g,
        'effectSize': eff_size,
        'isSignificant': is_sig,
        'significanceMarker': marker,
    }


def get_experimental_arm_analysis(survey):
    """
    Computes comparative analytics between experimental arms: gamified vs control.
    Includes Welch's t-test, p-values, degrees of freedom, and Hedges' g effect sizes.
    """
    detected_variants = set(
        Response.objects.filter(survey=survey)
        .exclude(variant__isnull=True)
        .exclude(variant='')
        .values_list('variant', flat=True)
        .distinct()
    )
    arm_names = list(detected_variants) if detected_variants else ['gamified', 'control']
    if 'control' not in arm_names:
        arm_names.append('control')
    if 'gamified' not in arm_names:
        arm_names.append('gamified')

    arms = {}
    for arm_name in arm_names:
        arm_responses = Response.objects.filter(survey=survey, variant=arm_name)
        count = arm_responses.count()
        scores, overall = get_hmsam_scores(survey, responses_qs=arm_responses)
        reliabilities = get_hmsam_construct_reliabilities(survey, variant=arm_name)
        arms[arm_name] = {
            'count': count,
            'constructScores': scores,
            'overallScore': overall,
            'reliabilities': reliabilities,
        }

    # Treatment effect: gamified - control
    g_scores = arms.get('gamified', {}).get('constructScores', {})
    c_scores = arms.get('control', {}).get('constructScores', {})
    diffs = {}
    stats_comparison = {}

    g_responses = list(Response.objects.filter(survey=survey, variant='gamified'))
    c_responses = list(Response.objects.filter(survey=survey, variant='control'))

    for k in CONSTRUCT_ORDER:
        if k in g_scores and k in c_scores:
            g_val = g_scores[k].get('normalized_score', g_scores[k].get('score', 0))
            c_val = c_scores[k].get('normalized_score', c_scores[k].get('score', 0))
            diffs[k] = round(g_val - c_val, 2)

            # Extract per-respondent means for construct k
            c_qs = survey.questions.filter(hmsam_construct__iexact=k)
            g_samples = []
            for resp in g_responses:
                ans_vals = list(Answer.objects.filter(response=resp, question__in=c_qs, likert_value__isnull=False).values_list('likert_value', flat=True))
                if ans_vals:
                    g_samples.append(sum(ans_vals) / float(len(ans_vals)))

            c_samples = []
            for resp in c_responses:
                ans_vals = list(Answer.objects.filter(response=resp, question__in=c_qs, likert_value__isnull=False).values_list('likert_value', flat=True))
                if ans_vals:
                    c_samples.append(sum(ans_vals) / float(len(ans_vals)))

            stats_comparison[k] = compute_welch_t_test(g_samples, c_samples)

    return {
        'arms': arms,
        'constructDifferences': diffs,
        'statisticalComparison': stats_comparison,
    }



# ── Response distribution ──────────────────────────────────
def get_response_distribution(survey):
    """
    For each question returns answer distribution for charts.
    Also collects text answers for word clouds.
    """
    distribution = []
    for q in survey.questions.all().order_by('order'):
        item = {
            'question':    q,
            'question_id': q.id,
            'text':        q.text,
            'type':        q.question_type,
            'data':        [],
            'texts':       [],
        }

        if q.question_type in ['likert', 'rating']:
            q_max = q.rating_max or 5
            if q_max == 5:
                labels = {1: 'Strongly Disagree', 2: 'Disagree', 3: 'Neutral', 4: 'Agree', 5: 'Strongly Agree'}
            elif q_max == 7:
                labels = {1: 'Strongly Disagree', 2: 'Disagree', 3: 'Somewhat Disagree', 4: 'Neutral', 5: 'Somewhat Agree', 6: 'Agree', 7: 'Strongly Agree'}
            else:
                labels = {i: f"Rating {i}" for i in range(1, q_max + 1)}

            existing_vals = set(Answer.objects.filter(question=q, likert_value__isnull=False).values_list('likert_value', flat=True))
            all_keys = sorted(set(labels.keys()) | existing_vals)
            for i in all_keys:
                lbl = labels.get(i, f"Rating {i}")
                count = Answer.objects.filter(question=q, likert_value=i).count()
                item['data'].append({'label': lbl, 'count': count})

        elif q.question_type == 'mcq':
            for opt in q.options.all():
                count = Answer.objects.filter(question=q, selected_option=opt).count()
                item['data'].append({'label': opt.text, 'count': count})

        elif q.question_type == 'checkbox':
            for opt in q.options.all():
                count = Answer.objects.filter(question=q, selected_option=opt).count()
                item['data'].append({'label': opt.text, 'count': count})

        elif q.question_type == 'dropdown':
            for opt in q.options.all():
                count = Answer.objects.filter(question=q, selected_option=opt).count()
                item['data'].append({'label': opt.text, 'count': count})

        elif q.question_type == 'date':
            # Group by date value
            answers = Answer.objects.filter(
                question=q
            ).exclude(text_value='').values_list('text_value', flat=True)
            date_counts = {}
            for val in answers:
                date_counts[val] = date_counts.get(val, 0) + 1
            item['data'] = [{'label': k, 'count': v}
                            for k, v in sorted(date_counts.items())]

        elif q.question_type in ['short', 'open']:
            texts = list(
                Answer.objects.filter(question=q)
                .exclude(text_value='')
                .values_list('text_value', flat=True)
            )
            item['texts'] = texts
            # Simple word frequency for word cloud
            item['word_freq'] = _word_frequency(texts)

        distribution.append(item)

    return distribution


def _word_frequency(texts, min_len=3, top_n=40):
    """
    Returns word frequency dict for word cloud rendering.
    Filters common stop words.
    """
    STOP_WORDS = {
        'the','and','is','in','it','of','to','a','an','that','this',
        'was','for','on','are','with','as','at','be','by','from',
        'or','but','not','have','had','has','were','they','their',
        'what','so','we','you','he','she','his','her','do','did',
        'will','can','all','been','more','which','when','there',
        'who','my','your','our','just','also','would','could',
        'than','then','about','into','up','out','if','its',
    }

    freq = {}
    for text in texts:
        words = text.lower().split()
        for word in words:
            clean = ''.join(c for c in word if c.isalpha())
            if len(clean) >= min_len and clean not in STOP_WORDS:
                freq[clean] = freq.get(clean, 0) + 1

    # Return top N sorted by frequency
    sorted_freq = sorted(freq.items(), key=lambda x: x[1], reverse=True)[:top_n]
    return dict(sorted_freq)


# ── Leaderboard (check if this necessary) ────────────────────────────────────────────
def get_leaderboard(survey, limit=10):
    """Returns top respondents by points for this survey."""
    return Response.objects.filter(survey=survey)\
        .select_related('user')\
        .order_by('-points_earned')[:limit]


# ── Per-respondent answers ─────────────────────────────────
def get_respondent_answers(response_obj):
    """
    Returns all answers for a single response, ordered by question order.
    Used in the 'by respondent' tab.
    """
    answers = []
    for q in response_obj.survey.questions.all().order_by('order'):
        ans = Answer.objects.filter(
            response=response_obj, question=q
        ).first()

        if not ans:
            value = '—'
        elif ans.likert_value is not None:
            q_max = q.rating_max or 5
            if q_max == 5:
                labels = {1: 'Strongly Disagree', 2: 'Disagree', 3: 'Neutral', 4: 'Agree', 5: 'Strongly Agree'}
            elif q_max == 7:
                labels = {1: 'Strongly Disagree', 2: 'Disagree', 3: 'Somewhat Disagree', 4: 'Neutral', 5: 'Somewhat Agree', 6: 'Agree', 7: 'Strongly Agree'}
            else:
                labels = {}
            value = labels.get(ans.likert_value, f"{ans.likert_value} / {q_max}")
        elif ans.text_value:
            value = ans.text_value
        elif ans.selected_option:
            value = ans.selected_option.text
        else:
            value = '—'

        answers.append({
            'question':      q,
            'question_text': q.text,
            'question_code': q.code,
            'type':          q.question_type,
            'value':         value,
        })

    return answers


# ── CSV export ─────────────────────────────────────────────
def csv_export(survey, pseudonymize=True):
    """
    Returns CSV string of all responses with research variables:
    pseudonymized ID, experimental arm (variant), platform, timings, quality flags, and consent.
    """
    import csv, io, hashlib

    questions = list(survey.questions.all().order_by('order'))
    output    = io.StringIO()
    writer    = csv.writer(output)

    header = [
        'Response ID', 'Pseudonym ID', 'Variant / Arm', 'Platform',
        'Duration (ms)', 'Quality Flags', 'Consent Version', 'Points Earned', 'Completed At'
    ]
    header += [f'Q{i+1}_{q.code or q.hmsam_construct or "item"}: {q.text[:40]}' for i, q in enumerate(questions)]
    writer.writerow(header)

    for resp in Response.objects.filter(survey=survey).order_by('completed_at'):
        if resp.user:
            raw_id = resp.user.email or str(resp.user.id)
            pseudo_id = hashlib.sha256(f"feedora_salt_{raw_id}".encode()).hexdigest()[:12] if pseudonymize else raw_id
        else:
            pseudo_id = f"anon_{resp.id}"

        flags_str = ";".join(resp.quality_flags) if isinstance(resp.quality_flags, list) else str(resp.quality_flags or '')
        row = [
            resp.id,
            pseudo_id,
            resp.variant or 'gamified',
            resp.platform or 'web',
            resp.duration_ms or '',
            flags_str,
            resp.consent_version or '1.0',
            resp.points_earned,
            resp.completed_at.strftime('%Y-%m-%d %H:%M:%S'),
        ]
        for q in questions:
            ans = Answer.objects.filter(response=resp, question=q).first()
            if not ans:
                row.append('')
            elif ans.likert_value is not None:
                row.append(ans.likert_value)
            elif ans.text_value:
                row.append(ans.text_value)
            elif ans.selected_option:
                row.append(ans.selected_option.text)
            else:
                row.append('')
        writer.writerow(row)

    return output.getvalue()