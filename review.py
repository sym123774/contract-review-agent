"""
合同审核 Agent - 命令行入口
用法：
  # 构建知识库（首次使用必须先构建）
  python review.py build-kb

  # 审核合同（完整流程：规则+知识库+LLM双路）
  python review.py 合同文件.docx

  # 仅规则引擎（不需要Ollama）
  python review.py 合同文件.docx --no-llm

  # 禁用知识库
  python review.py 合同文件.docx --no-kb

  # 指定远程Ollama地址
  python review.py 合同文件.docx --ollama http://<ollama-host>:11434/v1 --model qwen2.5:14b

  # 生成示例合同
  python review.py --sample

  # 输出JSON
  python review.py 合同文件.docx --json
"""
import os
import sys
import argparse
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import OLLAMA_BASE_URL, LLM_MODEL
from core.llm_client import LLMClient
from knowledge_base.retriever import KnowledgeBase, HAS_FAISS
from agent.pipeline import ContractReviewAgent, print_full_report


def main():
    parser = argparse.ArgumentParser(description="合同审核 Agent MVP")
    parser.add_argument("file", nargs="?", help="合同文件路径（.docx）")

    # 功能开关
    parser.add_argument("--no-llm", action="store_true", help="禁用LLM，仅运行规则引擎")
    parser.add_argument("--no-kb", action="store_true", help="禁用知识库检索")

    # Ollama配置
    parser.add_argument("--ollama", default=OLLAMA_BASE_URL, help=f"Ollama API地址（默认{OLLAMA_BASE_URL}）")
    parser.add_argument("--model", default=LLM_MODEL, help=f"模型名称（默认{LLM_MODEL}）")

    # 知识库
    parser.add_argument("--force-kb", action="store_true", help="强制重建知识库")
    parser.add_argument("--kb-backend", choices=["faiss", "qdrant"], default="faiss", help="知识库后端")
    parser.add_argument("--kb-domain", action="append", choices=["legal_kb", "industry_kb", "company_kb", "template_kb"], help="仅构建指定知识域，可重复")

    # 其他
    parser.add_argument("--sample", action="store_true", help="生成示例合同并审核")
    parser.add_argument("--json", action="store_true", help="输出JSON格式结果")
    parser.add_argument("--check", action="store_true", help="检查Ollama连接和模型")

    args = parser.parse_args()
    is_build_kb = args.file == 'build-kb'
    if is_build_kb: args.file = None

    # 检查Ollama连接
    if args.check:
        print("🔍 检查 Ollama 连接...")
        client = LLMClient(base_url=args.ollama, model=args.model)
        if client.check_connection():
            print(f"✅ Ollama 连接成功: {args.ollama}")
            models = client.list_models()
            print(f"   可用模型: {', '.join(models) if models else '（无）'}")
            if args.model in models:
                print(f"✅ 指定模型 {args.model} 可用")
            else:
                print(f"⚠️  指定模型 {args.model} 未找到，请先 pull")
        else:
            print(f"❌ Ollama 连接失败: {args.ollama}")
            print("   请确认Ollama已启动，地址是否正确")
        return

    # 构建知识库
    if is_build_kb:
        print("📚 开始构建知识库...")
        kb = KnowledgeBase(llm_client=LLMClient(base_url=args.ollama))
        success = kb.build(force=args.force_kb, kb_types=args.kb_domain, backend=args.kb_backend)
        if success:
            print("✅ 知识库构建完成")
        else:
            print("❌ 知识库构建失败")
        return

    # 生成示例合同
    if args.sample:
        from create_sample import create_sample
        sample_path = os.path.join(os.path.dirname(__file__), "sample_contract.docx")
        create_sample(sample_path)
        args.file = sample_path

    if not args.file:
        parser.print_help()
        print("\n💡 快速开始：")
        print("  1. 构建知识库：python review.py build-kb")
        print("  2. 审核合同：  python review.py 合同文件.docx")
        print("  3. 快速体验：  python review.py --sample --no-llm")
        sys.exit(0)

    if not os.path.exists(args.file):
        print(f"❌ 文件不存在: {args.file}")
        sys.exit(1)

    # 初始化组件
    llm_client = LLMClient(base_url=args.ollama, model=args.model)

    # 知识库
    kb = None
    if not args.no_kb:
        kb = KnowledgeBase(llm_client=llm_client)
        if not kb.load():
            if not HAS_FAISS and kb.build():
                print("✅ 知识库以内存关键词模式加载")
            else:
                print("⚠️  知识库未构建，将跳过知识库检索")
                print("   构建命令：python review.py build-kb")
                kb = None

    # 创建Agent并执行审核
    agent = ContractReviewAgent(
        llm_client=llm_client,
        knowledge_base=kb,
        enable_llm=not args.no_llm,
        enable_kb=kb is not None
    )

    result = agent.review(args.file)

    result_path = f"review_result_{os.path.splitext(os.path.basename(args.file))[0]}.json"
    with open(result_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2, default=str)
    print(f"\n📄 完整结果已保存: {result_path}")

    # 输出
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_full_report(result)


if __name__ == "__main__":
    main()



