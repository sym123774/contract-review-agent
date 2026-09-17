# 人工标注基准集（Gold Labels）

这里保存用于判断"合同风险能否准确识别"的人工标准答案。模型自己生成的结果不能反过来当标准答案。

每份标签文件必须记录源 DOCX 和解析后正文的 SHA-256、我方立场、风险事实、条款编号、标准风险类型和审批状态。评测使用 `input_text_sha256` 与审核结果对应，`source_file_sha256` 用于确认原始文件未被替换。只有同时满足以下条件的数据才会产生正式准确率：

1. `status` 为 `approved`；
2. `approved_by`、`approved_at` 不为空；
3. `exhaustively_reviewed` 为 `true`，表示评审人同时确认了风险项和可接受项，而非只标了几个明显问题；
4. 标签文件中的源文件散列与审核结果一致。

`draft_defect_purchase.json` 是根据人工构造瑕疵整理的待审草稿，只能验证评测流程，不能证明准确率。评审确认后复制为正式文件并填写审批字段。

运行示例：

```powershell
.\.venv\Scripts\python.exe tools\evaluate_gold_set.py `
  --labels evals\gold_contracts `
  --include-draft
```
