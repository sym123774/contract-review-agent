"""Inspect saved real-model audit evidence without calling or replacing the model."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import json_repair

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def norm(text):
    return re.sub(r'\s+', '', text or '')


def quote_matches(item, clauses):
    clause = clauses.get(item.get('clause_id') or item.get('clause_ref'))
    quote = re.sub(r'^\[c\d+\]\s*', '', item.get('original_text') or '')
    return bool(clause and norm(quote) and norm(quote) in norm(clause['title'] + '\n' + clause['content']))


def inspect(root):
    rows = []
    result_paths = set(root.glob('*/result.json'))
    result_paths.update(root.glob('*/*/result.json'))
    for result_path in sorted(result_paths):
        case = result_path.parent
        result, metrics, source = read(result_path), read(case / 'metrics.json'), read(case / 'source.json')
        clauses = {c['clause_id']: c for c in read(case / 'parsed.json')['clauses']}
        actual = Path(source['path'])
        if not actual.is_absolute():
            actual = ROOT / actual
        source_unchanged = hashlib.sha256(actual.read_bytes()).hexdigest() == source['sha256']
        calls = read(case / 'calls.json')
        prompt = sum(c.get('prompt_eval_count') or 0 for c in calls if c['kind'] == 'chat')
        completion = sum(c.get('eval_count') or 0 for c in calls if c['kind'] == 'chat')
        raw_items, raw_confidence, raw_parse_errors = [], Counter(), []
        for response_path in sorted(case.glob('call_*_response.json')):
            response = read(response_path)
            if not isinstance(response.get('message'), dict):
                continue  # embedding responses have no chat message
            content = response['message'].get('content', '')
            blocks = re.findall(r'```(?:json)?\s*([\s\S]*?)\s*```', content)
            candidate = blocks[-1] if blocks else content
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError as exc:
                try:
                    data = json_repair.loads(candidate)
                    raw_parse_errors.append({"call": response_path.name, "repaired": True,
                                             "error": str(exc)[:160]})
                except Exception as repair_exc:
                    raw_parse_errors.append({"call": response_path.name, "repaired": False,
                                             "error": str(repair_exc)[:160]})
                    continue
            if isinstance(data, dict):
                data = data['risks']
            for i, item in enumerate(data, 1):
                raw_confidence[str(item.get('confidence', 'missing'))] += 1
                raw_items.append(dict(call=response_path.name, item=i, title=item.get('title'),
                                      clause_id=item.get('clause_id'), original_text=item.get('original_text'),
                                      quote_matches=quote_matches(item, clauses)))
        retained = result['legal_risks'] + result['commercial_risks']
        evidence_ids = {e['evidence_id'] for e in result['evidence']}
        row = dict(case=case.name, source_unchanged=source_unchanged,
                   completeness=result['completeness'], review_errors=result['review_errors'],
                   calls=len(calls), native_prompt_tokens=prompt, native_completion_tokens=completion,
                   metrics_match_native=(prompt == metrics['native_usage']['prompt_tokens'] and
                                         completion == metrics['native_usage']['completion_tokens']),
                   pipeline_usage_matches_this_contract=(prompt == metrics['pipeline_usage']['prompt_tokens'] and
                                                        completion == metrics['pipeline_usage']['completion_tokens']),
                   evidence_domains=sorted({e['kb_type'] for e in result['evidence']}),
                   dangling_evidence=[ref for r in retained for ref in r.get('evidence', []) if ref not in evidence_ids],
                   dangling_related_evidence=[ref for r in retained for ref in r.get('related_evidence', []) if ref not in evidence_ids],
                   commercial_direct_evidence=[r['risk_id'] for r in result['commercial_risks'] if r.get('evidence')],
                   retained_invalid_quotes=[r['risk_id'] for r in retained if not quote_matches(r, clauses)],
                   raw_invalid_quotes=[r for r in raw_items if not r['quote_matches']],
                   raw_item_count=len(raw_items), retained_llm_item_count=len(retained),
                   raw_parse_errors=raw_parse_errors,
                   raw_confidence=dict(raw_confidence),
                   final_confidence=dict(Counter(str(r['confidence']) for r in retained)),
                   manual_context=result['party_context'])
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    if not rows:
        raise ValueError('No saved audit results found')
    (root / 'verification.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    assert all(r['source_unchanged'] and r['metrics_match_native'] and not r['retained_invalid_quotes']
               and not r['dangling_evidence'] and not r['dangling_related_evidence']
               and not r['commercial_direct_evidence'] and r['evidence_domains'] == ['legal_kb'] for r in rows)
    print(f'Evidence integrity passed for {len(rows)} contracts; reported model failures and audit findings remain unresolved.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('evidence_directory', type=Path)
    inspect(parser.parse_args().evidence_directory.resolve())
