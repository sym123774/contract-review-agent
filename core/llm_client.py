"""
LLM客户端 - 支持远程Ollama和GLM API
封装 chat 补全和 embedding 接口
"""
try:
    import requests
except ImportError:
    requests = None
import json
from typing import List, Optional
from config import OLLAMA_BASE_URL, OLLAMA_API_KEY, LLM_MODEL, EMBEDDING_MODEL, LLM_TEMPERATURE, LLM_MAX_TOKENS, LLM_NUM_CTX, LLM_TIMEOUT_SECONDS


class LLMClient:
    """LLM客户端 - 支持Ollama和GLM API"""

    # 熔断器：连接失败后 60 秒内直接快速失败，避免检索/审核循环里反复等待超时
    BREAKER_COOLDOWN = 60
    EMBED_CONNECT_TIMEOUT = 5   # 远程不可达时 5 秒内判定
    EMBED_READ_TIMEOUT = 30

    def __init__(self, base_url: str = None, api_key: str = None, model: str = None,
                 num_ctx: int = None):
        self.base_url = (base_url or OLLAMA_BASE_URL).rstrip("/")
        self.api_key = api_key or OLLAMA_API_KEY
        self.model = model or LLM_MODEL
        self.num_ctx = int(num_ctx or LLM_NUM_CTX)
        self.embedding_model = EMBEDDING_MODEL
        self.is_glm = "glm" in self.model.lower() or "open.bigmodel.cn" in self.base_url
        self._breaker_open_until = 0.0
        self._availability_checked_at = 0.0
        self._availability = False
        # 每次成功调用的用量明细（prompt/completion token、耗时），供审核报告统计
        self.usage_log = []
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }

    def _mark_unreachable(self):
        import time
        self._breaker_open_until = time.time() + self.BREAKER_COOLDOWN

    def _check_breaker(self):
        """熔断打开期间抛出快速失败异常"""
        import time
        if time.time() < self._breaker_open_until:
            raise ConnectionError(f"LLM服务不可达（熔断中，{self.base_url}）")

    def is_available(self) -> bool:
        """服务是否可达（带 30 秒缓存）"""
        import time
        if time.time() < self._breaker_open_until:
            return False
        if time.time() - self._availability_checked_at < 30:
            return self._availability
        ok = self.check_connection()
        self._availability_checked_at = time.time()
        self._availability = ok
        if not ok:
            self._mark_unreachable()
        return ok

    def chat(self, messages: List[dict], temperature: float = None, max_tokens: int = None) -> str:
        """
        聊天补全
        messages: [{"role": "system"/"user"/"assistant", "content": "..."}]
        """
        if requests is None:
            raise RuntimeError("缺少 requests 依赖，请安装 requirements.txt")
        self._check_breaker()
        temperature = LLM_TEMPERATURE if temperature is None else temperature
        max_tokens = LLM_MAX_TOKENS if max_tokens is None else max_tokens
        # Conservative CJK/UTF-8 budget; reject instead of silently truncating input.
        estimated = sum(len(m['content'].encode('utf-8')) for m in messages) // 2 + 128
        if not self.is_glm and estimated + max_tokens > self.num_ctx:
            raise ValueError('本批输入和输出预算超过模型上下文，请减小审核批次或增大LLM_NUM_CTX。')
        if self.is_glm:
            url = f"{self.base_url}/chat/completions"
            payload = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False
            }
            resp = requests.post(url, headers=self.headers, json=payload, timeout=(5, LLM_TIMEOUT_SECONDS))
            resp.raise_for_status()
            data = resp.json()
            self._record_usage('chat', data)
            return data["choices"][0]["message"]["content"]
        # Ollama：用原生 /api/chat 才能传 options.num_ctx（/v1 兼容接口会静默忽略 options，
        # 导致按模型默认 32768 分配 KV cache，超出显存后 ~20% 权重被卸到 CPU）
        url = self.base_url.removesuffix('/v1') + "/api/chat"
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "think": False,
            "options": {
                "num_ctx": self.num_ctx,
                "num_predict": max_tokens,
                "temperature": temperature,
            },
        }
        try:
            resp = requests.post(url, headers=self.headers, json=payload, timeout=(5, LLM_TIMEOUT_SECONDS))
        except (requests.ConnectionError, requests.Timeout):
            self._mark_unreachable()
            raise
        resp.raise_for_status()
        data = resp.json()
        msg = data.get("message") or {}
        content = msg.get("content", "")
        # 思考模型（如qwen3.6:27b）可能把输出放在thinking字段，content为空
        if not content.strip():
            raise ValueError('模型没有返回最终答案，不能把思考内容当成审核结果。')
        if data.get('done_reason') == 'length':
            raise ValueError('模型输出达到长度上限，本批未完成。')
        eval_count = data.get("eval_count")
        eval_dur = (data.get("eval_duration") or 1) / 1e9
        if eval_count:
            print(f"    [llm] 输入 {data.get('prompt_eval_count', '?')} tok → 生成 {eval_count} tok @ {eval_count / max(eval_dur, 1e-9):.1f} tok/s")
        self._record_usage('chat', data, eval_dur)
        return content

    def _record_usage(self, kind, data, seconds=None):
        """记录本次调用的 token 用量与耗时（Ollama 原生字段；GLM 用 usage 字段）。"""
        usage = data.get('usage') or {}
        prompt = data.get('prompt_eval_count', usage.get('prompt_tokens'))
        completion = data.get('eval_count', usage.get('completion_tokens'))
        try:
            prompt = int(prompt) if prompt is not None else 0
            completion = int(completion) if completion is not None else 0
        except (TypeError, ValueError):
            prompt, completion = 0, 0
        seconds = round(seconds if seconds is not None else (data.get('total_duration') or 0) / 1e9, 2)
        self.usage_log.append({'kind': kind, 'model': self.model, 'prompt_tokens': prompt,
                               'completion_tokens': completion, 'total_tokens': prompt + completion,
                               'seconds': seconds,
                               'tokens_per_second': round(completion / seconds, 1) if seconds > 0 else None})

    def usage_summary(self, start=0, end=None):
        """汇总指定调用区间；审核会话用起始下标隔离共享客户端的历史调用。"""
        calls = [u for u in self.usage_log[start:end] if u['kind'] == 'chat']
        total_seconds = round(sum(u['seconds'] for u in calls), 2)
        completion = sum(u['completion_tokens'] for u in calls)
        return {'model': self.model, 'chat_calls': len(calls),
                'prompt_tokens': sum(u['prompt_tokens'] for u in calls),
                'completion_tokens': completion,
                'total_tokens': sum(u['total_tokens'] for u in calls),
                'generation_seconds': total_seconds,
                'avg_tokens_per_second': round(completion / total_seconds, 1) if total_seconds else None}

    def embed(self, text: str, model: str = None) -> List[float]:
        """
        获取文本向量（embedding）
        优先用 Ollama 原生 /api/embeddings 接口
        """
        if requests is None:
            raise RuntimeError("缺少 requests 依赖，请安装 requirements.txt")
        self._check_breaker()
        if self.is_glm:
            raise ConnectionError('当前GLM配置未提供独立embedding服务，使用本地关键词检索。')
        url = self.base_url.removesuffix('/v1') + "/api/embeddings"
        payload = {
            "model": model or self.embedding_model,
            "prompt": text
        }
        try:
            resp = requests.post(url, headers=self.headers,
                                 json=payload, timeout=(self.EMBED_CONNECT_TIMEOUT, self.EMBED_READ_TIMEOUT))
            resp.raise_for_status()
        except (requests.ConnectionError, requests.Timeout) as exc:
            self._mark_unreachable()
            raise ConnectionError(f"Embedding服务不可达（{self.base_url}）: {exc}") from exc
        data = resp.json()
        return data["embedding"]

    def embed_batch(self, texts: List[str], model: str = None) -> List[List[float]]:
        """批量获取向量"""
        return [self.embed(t, model) for t in texts]

    def check_connection(self) -> bool:
        """检查Ollama连接"""
        try:
            if self.is_glm:
                resp = requests.get(self.base_url + '/models', headers=self.headers, timeout=5)
                return resp.status_code == 200
            return self.model.lower() in {m.lower() for m in self.list_models()}
        except Exception:
            return False

    def list_models(self) -> List[str]:
        """列出可用模型"""
        try:
            url = self.base_url.removesuffix('/v1') + "/api/tags"
            resp = requests.get(url, headers=self.headers, timeout=5)
            data = resp.json()
            return [m["name"] for m in data.get("models", [])]
        except Exception:
            return []

