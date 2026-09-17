"""
全局配置
"""
import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_DIR / '.env.local', override=False)
    load_dotenv(PROJECT_DIR / '.env', override=False)
except ImportError:
    pass

# ========== Ollama 配置 ==========
# 默认连接本机 Ollama；其他部署机可通过环境变量指定模型地址。
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")


def _bypass_proxy_for_lan(host: str):
    """内网/本机模型服务直连，不走外部代理。

    环境里若注入了不可用的 HTTP_PROXY（常见于开了代理软件但没启动），requests 会把
    对 192.168.x.x 的请求也发给代理，导致模型被误判为离线、审核静默降级成规则审查。
    """
    if not host:
        return
    for key in ("no_proxy", "NO_PROXY"):
        current = os.environ.get(key, "")
        entries = [e.strip() for e in current.split(",") if e.strip()]
        if host not in entries:
            entries.append(host)
        os.environ[key] = ",".join(entries)


try:
    from urllib.parse import urlparse as _urlparse
    _bypass_proxy_for_lan(_urlparse(OLLAMA_BASE_URL).hostname)
except Exception:
    pass
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "ollama")

# 模型配置
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3.6:27b")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "bge-m3")  # Ollama embedding模型

# 网页审核允许选择的本地模型。每个模型使用独立的上下文窗口，避免大模型因
# KV cache 过大挤占显存，也避免页面选择与后台实际运行的模型不一致。
REVIEW_MODEL_OPTIONS = {
    "qwen2.5-7b:latest": {"label": "Qwen2.5 7B（快速试跑）", "num_ctx": 16384},
    "qwen3.6:27b": {"label": "Qwen3.6 27B（默认）", "num_ctx": 16384},
    "qwen3:32b": {"label": "Qwen3 32B（较慢）", "num_ctx": 8192},
    "qwen2.5-72b-iq4xs:latest": {"label": "Qwen2.5 72B IQ4XS（冷启动约4分钟）", "num_ctx": 4096},
}

# ========== 知识库配置 ==========
KB_DIR = os.path.join(os.path.dirname(__file__), "knowledge_base", "data")
KB_INDEX_PATH = os.path.join(KB_DIR, "faiss_index.bin")
KB_META_PATH = os.path.join(KB_DIR, "metadata.json")

# 检索配置
RETRIEVE_TOP_K = 5          # 每次检索返回条数
RETRIEVE_SCORE_THRESHOLD = 0.3  # 相似度阈值
KB_DOMAINS = ["legal_kb", "industry_kb", "company_kb", "template_kb"]
# 审核链路允许检索的知识域。默认只走法规域（legal_kb）：行业规范、公司政策、标准条款
# 目前仍是小样本草稿，作为"依据"进入 prompt 容易让模型把它当法条引用。
# 需要放开时用环境变量 KB_REVIEW_DOMAINS=legal_kb,industry_kb 追加（Web 法律检索页不受此限制）。
KB_REVIEW_DOMAINS = [d.strip() for d in os.getenv("KB_REVIEW_DOMAINS", "legal_kb").split(",") if d.strip()]
# 审核链路每域取几条证据。单域（默认）下要给足，否则模型无依据可引，风险全落 needs_review。
KB_REVIEW_TOP_K_PER_KB = int(os.getenv("KB_REVIEW_TOP_K_PER_KB", "6"))
# 注入 prompt 的法条摘录长度上限（完整原文仍存进结果供人工查看）
KB_REVIEW_EXCERPT_CHARS = int(os.getenv("KB_REVIEW_EXCERPT_CHARS", "260"))
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "")

# ========== Agent 配置 ==========
LLM_TEMPERATURE = 0.3
LLM_MAX_TOKENS = int(os.getenv('LLM_MAX_TOKENS', '2048'))
LLM_TIMEOUT_SECONDS = int(os.getenv('LLM_TIMEOUT_SECONDS', '300'))
LLM_ITEM_RETRY_LIMIT = max(0, min(1, int(os.getenv('LLM_ITEM_RETRY_LIMIT', '1'))))
REVIEW_BATCH_CHARS = int(os.getenv('REVIEW_BATCH_CHARS', '2400'))
REVIEW_EVIDENCE_CHARS = int(os.getenv('REVIEW_EVIDENCE_CHARS', '2800'))
REVIEW_MAX_WORKERS = int(os.getenv('REVIEW_MAX_WORKERS', '1'))
REVIEW_MAX_PENDING = int(os.getenv('REVIEW_MAX_PENDING', '8'))
MAX_UPLOAD_BYTES = int(os.getenv('MAX_UPLOAD_MB', '20')) * 1024 * 1024
MAX_DOCX_EXPANDED_BYTES = int(os.getenv('MAX_DOCX_EXPANDED_MB', '80')) * 1024 * 1024
AUTH_USER = os.getenv('AUTH_USER', 'admin')
AUTH_PASS = os.getenv('AUTH_PASS', '')
API_HOST = os.getenv('API_HOST', '127.0.0.1')
# 服务端口。Windows 上 8000 常被 Hyper-V 动态保留段占用（netsh 排除范围），
# 绑定报 WinError 10013 时用 API_PORT 换成未保留端口。
API_PORT = int(os.getenv('API_PORT', '8000'))
# Ollama KV cache 上下文窗口，按模型显存占用调参（RTX 3090 24G 实测）：
#   qwen3.6:27B    → 16384（17GB 权重，显存有富余，16384 时 size_vram 仍等于 size）
#   qwen3:32B      →  8192（22GB 权重，实测最优值；32K 会超 24G 显存导致 ~20% 层落 CPU）
#   llama3.1:70B   →  4096（42GB 权重，设再大也无法全部进显存）
# 切换模型时同步设置 OLLAMA_BASE_URL / LLM_MODEL / LLM_NUM_CTX 三个环境变量
LLM_NUM_CTX = int(os.getenv("LLM_NUM_CTX", "16384"))

# 是否启用"从合同文本自动识别甲/乙方角色（委托方/供应商…）"。
# 默认关闭：自动识别把"甲方=委托方=付款方"这类推定当成事实，会把中风险误升为高风险。
# 立场（我方是甲方还是乙方、是付款方还是收款方）一律由用户手动选择；
# 确需恢复自动识别时设 PARTY_AUTO_DETECT=1。
PARTY_AUTO_DETECT = os.getenv("PARTY_AUTO_DETECT", "0") == "1"

# 审核维度
SEVEN_DIMENSIONS = [
    "主体资格", "标的客体", "合同条款",
    "合法性合规性", "商业风险", "履约能力", "争议解决"
]

# ========== 违约金畸高阈值（可配置）==========
# 内部预警阈值，不是通用合同违约金的法定上限。LPR×4 的利率保护上限仅适用于民间借贷；
# 违约金是否畸高需结合具体合同与法律依据判断，故 LPR 默认不预填（0），借贷利率须结合合同成立时报价另行核实。
LPR_ANNUAL_RATE = float(os.getenv("LPR_ANNUAL_RATE", "0"))
PENALTY_ANNUAL_CAP_RATIO = float(os.getenv("PENALTY_ANNUAL_CAP_RATIO", "4.0"))  # LPR 倍数上限
PENALTY_ANNUAL_CAP_PCT = round(LPR_ANNUAL_RATE * PENALTY_ANNUAL_CAP_RATIO, 2)   # 年化比率上限（%）
PENALTY_DAILY_CAP_PCT = float(os.getenv("PENALTY_DAILY_CAP_PCT", "0.5"))        # 日违约金直接报警阈值（%）
PENALTY_ANNUAL_WARNING_PCT = float(os.getenv('PENALTY_ANNUAL_WARNING_PCT', '30'))
