"""Build versioned local FAISS indexes using the configured Ollama embedding model."""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import KB_REVIEW_DOMAINS
from core.llm_client import LLMClient
from knowledge_base.retriever import KnowledgeBase


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", action="append", dest="domains",
                        help="knowledge domain to build; repeat for multiple domains")
    parser.add_argument("--all", action="store_true", help="build every knowledge domain")
    args = parser.parse_args()

    client = LLMClient()
    kb = KnowledgeBase(llm_client=client)
    kb.load()
    domains = None if args.all else (args.domains or list(KB_REVIEW_DOMAINS))
    print(f"embedding_model={client.embedding_model}; domains={domains or list(kb.metadata_by_domain)}")
    kb.build(force=True, kb_types=domains)
    check = KnowledgeBase(llm_client=client)
    check.load()
    missing = [domain for domain in (domains or kb.metadata_by_domain) if domain not in check.indexes]
    if missing:
        raise SystemExit("index verification failed: " + ", ".join(missing))
    print("verified indexes:", ", ".join(sorted(check.indexes)))


if __name__ == "__main__":
    main()
