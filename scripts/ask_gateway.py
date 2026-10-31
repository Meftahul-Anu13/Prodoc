"""Ask the configured LLM gateway one question and print its answer.

Usage: python -m scripts.ask_gateway "your question"
Uses backend/app/gateway_settings.py (or GATEWAY_URL/TOKEN/MODEL).
"""
import json
import sys
import time
import urllib.error
import urllib.request

from backend.app.gateway_llm import _settings


def main():
    question = " ".join(sys.argv[1:]) or "In one sentence, what is an invoice?"
    url, token, model = _settings()
    body = {"model": model, "messages": [{"role": "user", "content": question}]}
    request = urllib.request.Request(url + "/v1/chat/completions", data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    print(f"Gateway: {url}  Model: {model}\nQuestion: {question}\n")
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            reply = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        sys.exit(f"Gateway returned HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}")
    except (urllib.error.URLError, TimeoutError) as exc:
        sys.exit(f"Cannot reach the gateway at {url}: {exc}. Is the proxy running?")
    answer = reply["choices"][0]["message"]["content"]
    usage = reply.get("usage", {})
    print(f"Answer ({reply.get('model')}, {time.time() - started:.1f}s, "
          f"{usage.get('prompt_tokens')} in / {usage.get('completion_tokens')} out tokens):\n{answer}")


if __name__ == "__main__":
    main()
