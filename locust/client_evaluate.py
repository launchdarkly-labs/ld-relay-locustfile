from ldclient.flag import EvalResult, EvaluationDetail

def evaluate(flag):
    value = flag.get('value')
    variation_index = flag.get('variation')
    reason = flag.get('reason')
    detail = EvaluationDetail(value, variation_index, reason)

    return EvalResult(detail = detail, events = None)
