# Local model runbook (LM Studio)

## Selected model and compatibility

The local model configured in `12_MODEL_ROUTING/routing_table.json` is
`google/gemma-4-e4b`. The installed LM Studio model metadata identifies that alias
and points to the `lmstudio-community/gemma-4-E4B-it-GGUF` quantized artifact.
LM Studio's OpenAI-compatible API uses `http://127.0.0.1:1234/v1` by default and
supports `GET /v1/models` and `POST /v1/chat/completions`.

Start the LM Studio API server from its Developer tab, keep **Serve on Local
Network** off, and confirm the server listens only on loopback. `GET
http://127.0.0.1:1234/v1/models` is authoritative for the identifier expected by
the running server. If it differs from `google/gemma-4-e4b`, update the local
model key and its references in `routing_table.json`; the model ID is routing
configuration, not embedded in provider code.

`SILICIUM_LOCAL_BASE_URL` overrides the endpoint; its default is
`http://127.0.0.1:1234/v1`. The router refuses to send local-tier prompts to a
non-loopback OpenAI-compatible provider. LM Studio authentication is disabled by
default; do not expose this unauthenticated API to a LAN or the public internet.
No token or credential is needed in this configuration.

## Privacy and fail-closed routing

Task 2 data-class enforcement runs before the provider call. Restricted data has
only the local model in its safe route. If that model is unavailable, the router
raises `ModelRoutingExhaustedError`; it does not try a hosted model. For public
data, and internal data only when policy permits it, hosted capacity/quota failures
can skip the remaining hosted models and continue to the local fallback.

The routing metadata sets `logs_prompts` to `false` for the local model because
the configured endpoint is a loopback service, not a hosted prompt-logging
provider. This is not a guarantee that LM Studio, its GUI, operating system
diagnostics, or local files never retain prompt content. LM Studio documents
`lms log stream` as a way to display exact model inputs and outputs; do not run it
or redirect its output while handling restricted data. The official settings
documentation reviewed for this integration does not establish a complete
no-retention guarantee. Verify the installed LM Studio version's local logging
and history behavior before using real customer data; until then, use synthetic
data only.

Keep the model store, LM Studio logs/history, and runtime state outside Git and
any OneDrive or other synced folder. On Windows, `SILICIUM_DATA_DIR` can point to
`%LOCALAPPDATA%\Silicium` for request-budget state. Configure LM Studio's model
directory to a local, non-synced path as well.

## Hardware and performance

The selected Gemma 4 E4B configuration is appropriate for the reported 16 GB RAM
system. The local model metadata reports about 5.9 GB minimum memory use. Use a
CPU-first configuration and keep context sizes modest; do not assume Intel Iris
Xe acceleration or Docker GPU passthrough. CPU inference avoids GPU-runtime
assumptions but will be slower and have lower throughput than a supported
discrete-GPU setup.

## Licensing and runtime notes

Google's Gemma 4 model card identifies the upstream model as Apache-2.0. The
LM Studio Community GGUF model card identifies the upstream model and quantizer
but does not state a separate license for that converted artifact; check its
current license before redistributing model files. Do not copy model weights into
this repository.

LM Studio is an external local runtime, not a project dependency. The provider
uses the existing standard-library OpenAI-compatible client so another local
runtime can replace it without changing agent code. The LM Studio application's
license and business-use terms are separate from the model license and were not
validated for this change; verify them independently before relying on that
runtime for business operations.

## Opt-in synthetic smoke test

The local smoke test is skipped unless explicitly enabled. It sends only a
synthetic, non-personal prompt:

```powershell
$env:SILICIUM_LIVE_LOCAL = "1"
$env:SILICIUM_LOCAL_BASE_URL = "http://127.0.0.1:1234/v1"
$env:SILICIUM_LOCAL_MODEL_ID = "google/gemma-4-e4b"
.\.venv\Scripts\python.exe -m pytest -m live -k live_lm_studio_smoke
```

Set `SILICIUM_LOCAL_MODEL_ID` to the ID returned by `/v1/models` when it differs
from the installed model metadata alias. Never use customer content for this
smoke test.

## References

- [LM Studio OpenAI-compatible endpoints](https://lmstudio.ai/docs/developer/openai-compat)
- [LM Studio server settings](https://lmstudio.ai/docs/developer/core/server/settings)
- [LM Studio list models](https://lmstudio.ai/docs/developer/openai-compat/models)
- [LM Studio model IO log stream](https://lmstudio.ai/docs/cli/serve/log-stream)
- [Google Gemma 4 model card and license](https://ai.google.dev/gemma/docs/core/model_card_4)
- [LM Studio Community Gemma 4 E4B GGUF card](https://huggingface.co/lmstudio-community/gemma-4-E4B-it-GGUF)
