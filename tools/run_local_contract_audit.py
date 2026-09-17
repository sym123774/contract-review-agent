"""Run real local Ollama reviews and retain requests, native responses and per-case metrics.

The profile hook observes the existing client; it does not replace the client,
transport, model response, parser, rules or retriever.
"""
import argparse
import contextlib
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import config
from agent.pipeline import ContractReviewAgent
from core.llm_client import LLMClient
from core.parser import parse_docx
from knowledge_base.retriever import KnowledgeBase


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, value):
        for stream in self.streams:
            stream.write(value)
            stream.flush()
        return len(value)

    def flush(self):
        for stream in self.streams:
            stream.flush()


class ClientObserver:
    def __init__(self, dest):
        self.dest, self.active, self.calls = dest, {}, []
        self.codes = {LLMClient.chat.__code__, LLMClient.embed.__code__}

    def profile(self, frame, event, arg):
        if frame.f_code not in self.codes:
            return
        key = id(frame)
        if event == 'call':
            record = {'call': len(self.calls) + 1, 'kind': frame.f_code.co_name,
                      'started_at': datetime.now().isoformat(timespec='seconds')}
            self.calls.append(record)
            self.active[key] = (time.perf_counter(), record)
            initial = {k: frame.f_locals[k] for k in ('messages', 'text') if k in frame.f_locals}
            write_json(self.dest / f"call_{record['call']:03d}_input.json", initial)
            print(f"AUDIT call {record['call']} {record['kind']} started", flush=True)
        elif event == 'return' and key in self.active:
            start, record = self.active.pop(key)
            local = frame.f_locals
            record['wall_seconds'] = round(time.perf_counter() - start, 3)
            record['returned_value'] = arg is not None
            if 'payload' in local:
                write_json(self.dest / f"call_{record['call']:03d}_request.json", {
                    'url': local.get('url'), 'json': local['payload']})
            data = local.get('data')
            if isinstance(data, dict):
                write_json(self.dest / f"call_{record['call']:03d}_response.json", data)
                for name in ('done', 'done_reason', 'prompt_eval_count', 'eval_count',
                             'total_duration', 'load_duration', 'prompt_eval_duration', 'eval_duration'):
                    record[name] = data.get(name)
                duration = (data.get('eval_duration') or 0) / 1e9
                record['generation_tokens_per_second'] = round(data.get('eval_count', 0) / duration, 2) if duration else None
            write_json(self.dest / 'calls.json', self.calls)
            print('AUDIT ' + json.dumps(record, ensure_ascii=False), flush=True)


def main():
    # Windows PowerShell may expose a GBK text stream. Pipeline progress uses
    # Unicode symbols, so make the evidence runner independent of console codepage.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if reconfigure:
            reconfigure(encoding='utf-8', errors='replace')
    ap = argparse.ArgumentParser()
    ap.add_argument('--manifest', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    dest = args.output.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    cases = json.loads(args.manifest.read_text(encoding='utf-8-sig'))
    if urlparse(config.OLLAMA_BASE_URL).hostname not in ('localhost', '127.0.0.1', '::1'):
        raise RuntimeError('This audit requires Ollama on this computer.')
    llm = LLMClient()
    if not llm.is_available():
        raise RuntimeError('Configured local model is unavailable; audit will not use a substitute.')
    kb = KnowledgeBase(llm_client=llm)
    kb.load()
    snapshot = {name: getattr(config, name) for name in (
        'OLLAMA_BASE_URL', 'LLM_MODEL', 'EMBEDDING_MODEL', 'LLM_NUM_CTX', 'LLM_MAX_TOKENS',
        'LLM_TIMEOUT_SECONDS', 'LLM_ITEM_RETRY_LIMIT', 'KB_REVIEW_DOMAINS', 'KB_REVIEW_TOP_K_PER_KB',
        'KB_REVIEW_EXCERPT_CHARS', 'PARTY_AUTO_DETECT')}
    snapshot.update(git_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                    started_at=datetime.now().isoformat(timespec='seconds'),
                    knowledge_counts={k: len(v) for k, v in kb.metadata_by_domain.items()},
                    vector_indexes=list(kb.indexes), knowledge_warnings=kb.warnings,
                    model_list=llm.list_models(),
                    observation='sys.setprofile on real LLMClient; no response substitution',
                    client_lifetime='shared across cases, matching the web API; native metrics are counted per case')
    write_json(dest / 'environment.json', snapshot)
    rows = []
    for case in cases:
        case_dest = dest / case['id']
        case_dest.mkdir(exist_ok=False)
        path = Path(case['path'])
        if not path.is_absolute():
            path = ROOT / path
        source = dict(case, sha256=hashlib.sha256(path.read_bytes()).hexdigest(), bytes=path.stat().st_size)
        write_json(case_dest / 'source.json', source)
        parsed = parse_docx(str(path))
        write_json(case_dest / 'parsed.json', parsed)
        observer = ClientObserver(case_dest)
        previous = sys.getprofile()
        row = {'id': case['id'], 'source': source}
        result = None
        started = time.perf_counter()
        with (case_dest / 'terminal.log').open('w', encoding='utf-8') as log:
            with contextlib.redirect_stdout(Tee(sys.stdout, log)), contextlib.redirect_stderr(Tee(sys.stderr, log)):
                print(f"CASE {case['id']} | {path.name} | {len(parsed['full_text'])} chars", flush=True)
                try:
                    sys.setprofile(observer.profile)
                    agent = ContractReviewAgent(llm_client=llm, knowledge_base=kb, enable_llm=True, enable_kb=True)
                    result = agent.review(str(path), our_side=case.get('our_side'), we_pay=case.get('we_pay'))
                except Exception as exc:
                    row['fatal_error'] = f'{type(exc).__name__}: {exc}'
                    traceback.print_exc()
                finally:
                    sys.setprofile(previous)
                row['wall_seconds'] = round(time.perf_counter() - started, 3)
                chats = [c for c in observer.calls if c['kind'] == 'chat']
                seconds = sum((c.get('eval_duration') or 0) / 1e9 for c in chats)
                output = sum(c.get('eval_count') or 0 for c in chats)
                row['native_usage'] = {'chat_attempts': len(chats),
                    'native_responses': sum('done_reason' in c for c in chats),
                    'prompt_tokens': sum(c.get('prompt_eval_count') or 0 for c in chats),
                    'completion_tokens': output, 'generation_seconds': round(seconds, 3),
                    'tokens_per_second': round(output / seconds, 2) if seconds else None,
                    'length_stops': sum(c.get('done_reason') == 'length' for c in chats)}
                if result is not None:
                    write_json(case_dest / 'result.json', result)
                    evidence_ids = {e['evidence_id'] for e in result['evidence']}
                    row.update(completeness=result['completeness'], clauses=result['clause_count'],
                        batches=len(result['batches']), errors=result['review_errors'],
                        counts={key: len(result[key + '_risks']) for key in ('rule', 'legal', 'commercial')},
                        evidence_domains=sorted({e['kb_type'] for e in result['evidence']}),
                        retrieval_modes=sorted({m for b in result['batches'] for m in b.get('retrieval_mode', [])}),
                        dangling_evidence=[{'risk_id': r['risk_id'], 'ref': ref}
                            for r in result['legal_risks'] + result['commercial_risks']
                            for ref in r.get('evidence', []) if ref not in evidence_ids],
                        dangling_related_evidence=[{'risk_id': r['risk_id'], 'ref': ref}
                            for r in result['legal_risks'] + result['commercial_risks']
                            for ref in r.get('related_evidence', []) if ref not in evidence_ids],
                        pipeline_usage=result['provenance']['usage'])
                print('CASE_RESULT ' + json.dumps(row, ensure_ascii=False), flush=True)
                write_json(case_dest / 'metrics.json', row)
                rows.append(row)
                write_json(dest / 'metrics.json', rows)
    print(f'AUDIT DONE: {len(rows)} contracts; evidence: {dest}', flush=True)


if __name__ == '__main__':
    main()
