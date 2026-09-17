"""Task 1 evidence: real local Ollama and optional live web service checks.

Run with .venv/Scripts/python.exe tools/verify_local_environment.py [--web].
No mocks, model downloads, index rebuilds, or contract reviews are performed.
Passwords and authorization headers are never logged.
"""
import argparse
import importlib.metadata
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import requests
from packaging.requirements import Requirement
import config
from core.llm_client import LLMClient


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--web', action='store_true', help='Also check the running web app')
    parser.add_argument('--output', default=str(ROOT / 'output/task1_environment_20260911'))
    args = parser.parse_args()
    dest = Path(args.output)
    dest.mkdir(parents=True, exist_ok=True)
    result = {'checked_at': datetime.now().astimezone().isoformat(), 'python': sys.version,
              'executable': sys.executable, 'checks': {}}

    def record(name, data):
        result['checks'][name] = data
        print(name + ': ' + json.dumps(data, ensure_ascii=False), flush=True)
        (dest / 'environment-verification.json').write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')

    dependencies = []
    for line in (ROOT / 'requirements.txt').read_text(encoding='utf-8-sig').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        req = Requirement(line)
        version = importlib.metadata.version(req.name)
        assert version in req.specifier, f'{req.name} {version} does not satisfy {req.specifier}'
        dependencies.append({'name': req.name, 'version': version, 'requirement': str(req)})
    record('requirements', dependencies)
    pip_check = subprocess.run([sys.executable, '-m', 'pip', 'check'], text=True, capture_output=True)
    record('pip_check', {'exit_code': pip_check.returncode, 'output': pip_check.stdout + pip_check.stderr})
    assert pip_check.returncode == 0
    assert config.OLLAMA_BASE_URL == 'http://127.0.0.1:11434/v1'
    assert config.API_HOST == '127.0.0.1'
    assert len(config.AUTH_PASS) >= 12
    record('config', {'ollama_base_url': config.OLLAMA_BASE_URL, 'llm_model': config.LLM_MODEL,
                      'embedding_model': config.EMBEDDING_MODEL, 'api_host': config.API_HOST,
                      'auth_user': config.AUTH_USER, 'auth_password_length': len(config.AUTH_PASS)})
    session = requests.Session()
    session.trust_env = False
    base = config.OLLAMA_BASE_URL.removesuffix('/v1')
    tags = session.get(base + '/api/tags', timeout=10)
    tags.raise_for_status()
    model_data = tags.json()
    (dest / 'ollama-tags.json').write_text(json.dumps(model_data, ensure_ascii=False, indent=2), encoding='utf-8')
    names = {m['name'] for m in model_data['models']}
    assert config.LLM_MODEL in names
    assert config.EMBEDDING_MODEL in names or config.EMBEDDING_MODEL + ':latest' in names
    record('ollama_tags', {'http_status': tags.status_code, 'models': sorted(names)})

    payload = {'model': config.LLM_MODEL,
               'messages': [{'role': 'user', 'content': '只回答数字：1加1等于几？'}],
               'stream': False, 'think': False,
               'options': {'num_ctx': config.LLM_NUM_CTX, 'num_predict': 64, 'temperature': 0}}
    (dest / 'ollama-chat-request.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Starting real local /api/chat request...', flush=True)
    started = time.perf_counter()
    response = session.post(base + '/api/chat', json=payload, timeout=(5, config.LLM_TIMEOUT_SECONDS))
    response.raise_for_status()
    raw = response.json()
    (dest / 'ollama-chat-response.json').write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding='utf-8')
    elapsed = time.perf_counter() - started
    assert raw.get('message', {}).get('content', '').strip(), 'No final model answer'
    assert raw.get('done_reason') != 'length', 'Model output truncated'
    record('real_ollama_chat', {'http_status': response.status_code, 'model': raw.get('model'),
                              'content': raw['message']['content'], 'elapsed_seconds': round(elapsed, 3),
                              'input_tokens': raw.get('prompt_eval_count'), 'output_tokens': raw.get('eval_count'),
                              'tokens_per_second': round(raw.get('eval_count', 0) / max(raw.get('eval_duration', 0)/1e9, 1e-9), 3),
                              'done_reason': raw.get('done_reason')})
    print('Starting real project LLMClient.chat call...', flush=True)
    client = LLMClient()
    assert client.is_available(), 'Project model availability check failed'
    started = time.perf_counter()
    answer = client.chat([{'role': 'user', 'content': '只回答数字：2加2等于几？'}], temperature=0, max_tokens=64)
    record('project_llm_client', {'answer': answer, 'elapsed_seconds': round(time.perf_counter()-started, 3)})
    print('Starting real project LLMClient.embed call...', flush=True)
    vector = client.embed('本地合同审核环境连接验证')
    assert vector and all(isinstance(x, (int, float)) for x in vector)
    record('project_embedding', {'model': client.embedding_model, 'dimensions': len(vector)})

    if args.web:
        health = session.get('http://127.0.0.1:8000/health', timeout=10)
        health.raise_for_status()
        anonymous = session.get('http://127.0.0.1:8000/', timeout=10)
        auth = (config.AUTH_USER, config.AUTH_PASS)
        home = session.get('http://127.0.0.1:8000/', auth=auth, timeout=10)
        home.raise_for_status()
        status = session.get('http://127.0.0.1:8000/api/system/status', auth=auth, timeout=20)
        status.raise_for_status()
        record('live_web', {'health_status': health.status_code, 'health_body': health.json(),
                           'anonymous_home_status': anonymous.status_code, 'authenticated_home_status': home.status_code,
                           'system_status_http': status.status_code, 'system_status': status.json()})
        assert anonymous.status_code == 200 and anonymous.url.endswith('/login')
        assert status.json()['llm_available'] is True
    record('result', 'PASS')


if __name__ == '__main__':
    main()
