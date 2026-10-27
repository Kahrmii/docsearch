"""Ask questions about an index (command line). The window (start.bat) offers the same with history.

  python ask.py                                  interactive, local model
  python ask.py "How many documents are there per file type?"
  python ask.py --provider anthropic --index project_a "Question"    cloud (approved indices only)
  python ask.py --index project_a --allow-cloud                      approve an index for the cloud
  python ask.py --index project_a --deny-cloud                       withdraw the approval
"""
import argparse
import json
import sys

from . import __version__, agent, cloud_policy, config, llm, secrets_check, tools
from .es import Es, EsError
from .llm import LlmError


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version="docsearch " + __version__)
    ap.add_argument("question", nargs="?")
    ap.add_argument("--index", default="documents", help="index to search (default: documents)")
    ap.add_argument("--provider", choices=sorted(llm.PROVIDER_LABELS), default=config.LLM_PROVIDER,
                    help="local = local model, anthropic = Claude in the cloud (default: LLM_PROVIDER or local)")
    ap.add_argument("--allow-cloud", action="store_true", help="approve the index for the cloud (stays saved)")
    ap.add_argument("--deny-cloud", action="store_true", help="withdraw the cloud approval of the index")
    ap.add_argument("--ignore-secret-warning", action="store_true",
                    help="use the cloud even if the index looks like it contains keys or credentials")
    args = ap.parse_args()
    index = args.index.strip().lower()
    config.ensure_env()
    es = Es()

    def check_secrets():
        """Stop if the index looks like it holds keys or credentials (unless explicitly overridden)."""
        findings = secrets_check.scan(es, index)
        if secrets_check.risky(findings) and not args.ignore_secret_warning:
            sys.exit(secrets_check.describe(index, findings)
                     + "\nNothing was sent. Use --ignore-secret-warning to proceed anyway.")

    if args.allow_cloud or args.deny_cloud:
        if args.allow_cloud:
            check_secrets()
        cloud_policy.set_allowed(index, args.allow_cloud)
        print(f"Index '{index}': cloud {'approved' if args.allow_cloud else 'blocked'}.")
        if not args.question:
            return
    try:
        client = llm.get_client(args.provider, index)
    except LlmError as e:
        sys.exit(str(e))
    if client.cloud:
        check_secrets()
    ctx_chars = client.context_chars()
    history = []

    def show_step(name, call_args):
        shown = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in call_args.items())
        print(f"  -> {name}({shown})")

    def ask(question):
        ctx = tools.Context(es, index, ctx_chars)
        try:
            answer = agent.ask(question, ctx, client, history, on_step=show_step)
        except (EsError, LlmError) as e:
            print(f"Error: {e}")
            return
        print("\n" + answer)
        history.extend([{"role": "user", "content": question}, {"role": "assistant", "content": answer}])

    if args.question:
        ask(args.question)
        return
    print(f"Index: {index} | Model: {client.label()}. Enter a question (empty line quits).")
    while True:
        try:
            question = input("\n> ").strip()
        except EOFError:
            break
        if not question:
            break
        ask(question)


if __name__ == "__main__":
    main()
