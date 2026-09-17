# 📑 合同审核 Agent（Contract Review Agent）

基于 **本地大模型 + 规则引擎 + 法规知识库** 的智能合同审核系统。上传 DOCX 合同后，系统自动完成**条款解析 → 确定性规则检查 → 法律/商业双视角分析 → 证据级结果校验**，输出带**原文定位与法规依据**的审核报告，支持网页查看与 PDF/CSV 导出。

---

## ✨ 核心特性

| 特性 | 说明 |
|------|------|
| 📄 完整 DOCX 解析 | 解析正文、表格、页眉页脚与文本框，生成稳定的条款 ID |
| ⚖️ 确定性规则引擎 | 对金额、比例、期限、违约责任、争议解决等执行可复算的硬规则检查 |
| 🏛️ 法规知识中心 | 内置 1000+ 条法律法规与司法解释（含民法典、司法解释、医疗/数据/支付等专项法规），FAISS 向量检索 + 本地关键词双通道 |
| 🤖 法律 + 商业双分析 | 同一合同分别执行法律分析与商业风险分析，再合并去重 |
| 🔍 证据级校验 | 逐条验证模型输出的合同原文引用与法规依据，无效引用单条重试，不污染整批结果 |
| 🖥️ Web 界面 | 审核结果可视化、登录鉴权、PDF/CSV 导出 |

---

## 🏗️ 系统架构

```
DOCX 合同
   │
   ▼
┌─ core/parser.py ──────────────┐
│ 正文 / 表格 / 页眉页脚 / 文本框  │
│ 条款 ID 稳定编号                │
└──────────────┬────────────────┘
               ▼
┌─ core/rules.py 确定性规则引擎 ──┐
│ 金额 / 比例 / 期限 / 违约责任     │
│ 争议解决 / 格式规范              │
└──────────────┬────────────────┘
               ▼
┌─ agent/pipeline.py 审核流水线 ──┐
│ 场景识别 → 知识检索 → 分批审核    │
│ 法律分析 ├── 商业分析            │
│ 证据校验 → 合并去重 → 结果整合    │
└──────┬─────────────────────────┘
       ▼
┌─ core/llm_client.py ──────────┐   ┌─ knowledge_base/retriever.py ─┐
│ 本地 Ollama / 远程模型适配      │   │ FAISS 向量检索 + 关键词检索    │
└───────────────────────────────┘   └──────────────┬───────────────┘
                                                    ▼
                                      legal_kb(1000+) / industry_kb / company_kb / template_kb
       ▼
┌─ Web 界面 (static/) ──────────┐
│ 审核报告 / 原文定位 / 导出 PDF/CSV │
└────────────────────────────────┘
```

---

## 📁 项目结构

```text
contract-review-agent/
├── api_server.py          # FastAPI 服务入口
├── review.py              # 命令行审核 / 知识库构建入口
├── config.py              # 配置（模型、端口、认证）
├── agent/                 # 审核流水线：提示词、分批审核、结果整合
├── core/                  # DOCX 解析、规则引擎、模型客户端、证据校验
├── knowledge_base/        # 法规数据源、检索器、适用域路由
├── static/                # Web 页面（HTML/CSS/JS）
├── tools/                 # 知识库索引构建、本地环境验证、黄金集评测
├── evals/                 # 人工标注基准（gold labels）
├── tests/                 # 离线单元测试
└── examples/              # 示例合同（docx 样本）
```

---

## 🚀 快速开始

### 1. 环境要求

- Python 3.10+
- 本地或远程 OpenAI 兼容的 LLM 服务（默认 `http://127.0.0.1:11434/v1`，即本机 Ollama，模型如 `qwen3.6:27b`）

### 2. 安装依赖

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env.local
```

### 3. 配置

编辑 `.env.local`：

| 变量 | 说明 |
|------|------|
| `OLLAMA_BASE_URL` | 模型服务地址（默认本机 Ollama `http://127.0.0.1:11434/v1`） |
| `LLM_MODEL` | 必须与 `ollama list` 中的模型名一致 |
| `AUTH_USER` / `AUTH_PASS` | 网页登录账号（密码至少 12 位） |
| `API_PORT` | 服务端口（Windows 8000 被占用时改为 8110 等） |

### 4. 启动

```powershell
ollama list
.\.venv\Scripts\python.exe api_server.py
```

浏览器访问日志中打印的 `API_HOST:API_PORT` 地址。

### 5. 知识库构建（可选）

没有 FAISS 索引时系统自动使用本地关键词检索；构建向量索引：

```powershell
.\.venv\Scripts\python.exe review.py build-kb
```

### 6. 运行测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

默认测试不访问网络和模型服务。真实模型审计会保存请求、原始响应、解析结果与耗时数据。

---

## 📊 知识中心

| 知识域 | 数量 | 状态 |
|---|---:|---|
| `legal_kb` | 1067 | 启用，唯一允许进入审核提示词的知识域 |
| `industry_kb` | 8 | 禁用 |
| `company_kb` | 0（默认不内置，可自行接入） | 禁用 |
| `template_kb` | 6 | 全部草稿，禁用 |

`legal_kb` 内部按 `general`、`medical_device`、`data_privacy`、`small_business_payment` 标记适用范围，普通合同自动排除专属法规。

---

## 📌 使用说明与免责声明

- 审核结果会把法规分为"与结论机械对齐的直接依据"与"仅主题相关的材料"，页面与导出文件始终提示人工复核。
- 不完整的模型审核不能导出 PDF/CSV，只能导出带 `INCOMPLETE` 标记的原始 JSON 用于排查。
- **本项目仅供学习与技术展示，模型结论与法规适用仍需执业律师复核，不构成法律意见。**
- 当前范围只处理带可读文字的 DOCX；图片扫描件、PDF 与 OCR 不在范围内。

---

## 📄 License

MIT License. 知识库中引用的法律法规条文来源于公开官方渠道（如国家法律法规数据库等），版权归原发布机关所有，此处仅作技术学习用途。
