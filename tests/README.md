# 测试目录

日常回归只运行不访问网络和模型的单元测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

- `unit/`：默认 pytest 测试，必须快速、确定且不访问 Ollama。
- `archive/legacy_model_probes/`：旧模型、旧 IP 和历史排障脚本，仅保留追溯，禁止由 pytest 自动收集。
- 根目录 `_r*.py`、`regression_rules.py`：旧轮次的专项回归，逐步迁入 `unit/`；默认测试不依赖它们。
- `../tools/run_local_contract_audit.py`：真实本机 Ollama 验收。它耗时较长，必须显式提供清单和输出目录。
- `../tools/verify_local_contract_audit.py`：离线核对真实模型证据，不重新调用模型。

新增业务缺陷时，先在 `unit/` 写能够稳定复现的测试。真实模型输出不能作为单元测试断言；模型比较应保存请求、原始响应、解析结果和原生 token 指标。
