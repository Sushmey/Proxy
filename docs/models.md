# Choosing a model

The agent works with any model that supports **tool calling**. You choose in
`.env` (copy `.env.example` first). Leave a line blank to use its default.

| Setting | What it does |
|---|---|
| `LLM_PROVIDER` | `ollama` (default), `openai_compatible`, or `anthropic` |
| `LLM_MODEL` | model name as the provider knows it; blank uses the provider's default |
| `LLM_BASE_URL` | where the API lives; blank uses the provider's default |
| `LLM_API_KEY` | key for hosted providers; blank for local Ollama |
| `LLM_NUM_CTX` | Ollama only: context window in tokens |
| `LLM_MAX_TOKENS` | Anthropic only: longest reply (default 16000) |

Restart the bot after changing `.env`.

## Local: Ollama (default)

Nothing about your messages leaves your machine except calls to Google and the
optional services.

```bash
# install Ollama from https://ollama.com, then:
ollama pull gpt-oss:20b
```

No `.env` changes needed. Any Ollama model that supports tools works; very
small models struggle with multi-step requests (for example, checking the
calendar and the inbox in one answer).

If replies get cut off mid-sentence, the model's context window is filling up.
Set `LLM_NUM_CTX=16384` (or higher). It uses more memory.

## OpenAI and OpenAI-compatible services

One setting covers OpenAI, OpenRouter, Groq, Together, vLLM, LM Studio, and
anything else that speaks the same API.

```
LLM_PROVIDER=openai_compatible
LLM_MODEL=<model id from your provider>
LLM_API_KEY=<your key>
LLM_BASE_URL=<see below; blank means OpenAI>
```

Typical base URLs (check your provider's docs):
OpenRouter `https://openrouter.ai/api/v1`,
Groq `https://api.groq.com/openai/v1`,
LM Studio `http://localhost:1234/v1`,
Ollama's own `http://localhost:11434/v1`.

This provider has been tested against Ollama's `/v1` endpoint, not yet against
a paid OpenAI account.

## Claude (Anthropic)

```bash
pip install anthropic
```

```
LLM_PROVIDER=anthropic
LLM_API_KEY=<your key>      # or leave blank and set ANTHROPIC_API_KEY
LLM_MODEL=                  # blank = claude-opus-5-5; claude-sonnet-5-5 costs less
```

This provider has been tested with a stand-in client only, not yet against the
live API. If a call fails, the error message from Anthropic is printed in the
terminal.

## What a hosted model sees

With any hosted provider (OpenAI-compatible or Anthropic), the text of your
calendar events and emails that the agent reads is sent to that provider, and
you pay per use. Keep Ollama if you don't want that.
