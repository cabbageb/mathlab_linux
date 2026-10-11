# MathLab Linux

MathLab Linux is a single-user, terminal-first distribution of MathLab. It keeps
the Contract, Dynamic Prover, verification and publication pipeline, while
running the local control plane on SQLite and showing all interaction in the
terminal. It does not require Docker or a browser.

## Requirements

- Linux with Python 3.11 or newer;
- the OpenAI Codex CLI (Node.js/npm may be used to install it);
- XeLaTeX and `latexmk` when PDF publication is needed;
- a provider with an OpenAI Responses-compatible `/v1/responses` endpoint.

The provider and model tags are configurable. Chat Completions-only providers
are not supported by this distribution.

## Install

```bash
git clone https://github.com/cabbageb/mathlab_linux.git
cd mathlab_linux
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

Edit [`config/provider.toml`](config/provider.toml) with the provider URL, the
name of the environment variable containing the key, and the model tags. Then
export that key, or put it in `.env`:

```bash
export MATHLAB_PROVIDER_API_KEY='your-key'
```

The key is read only by the local process. It is never written to the database,
runtime artifacts, reports, or Git.

## CLI

Start the local terminal interface:

```bash
mathlab-linux
```

Useful commands inside the CLI:

```text
/help
/new <research problem>
/sessions
/use <conversation-id>
/show
/generate
/edit
/commit
/policy <model_tier> <duration_tier>
/approve
/status
/events
/test_api
/quit
```

`/test_api` asks for confirmation and performs one small paid Responses request.
It reports only the endpoint, HTTP status, model, usage field names and a short
output preview. The non-interactive equivalent is:

```bash
mathlab-linux test-api --profile medium
```

Add `--yes` only when the paid probe is intentional.

For the full isolated acceptance, configure the provider first and run:

```bash
python scripts/local/live_acceptance.py \
  --problem "State the research problem for this acceptance run." \
  --yes
```

This creates a separate temporary SQLite/data directory, generates an immutable
Contract with the low profile, freezes `gpt_low + quick_1h` (3,600 research
seconds), runs the real provider, and waits for the final publication pipeline.
The report and PDF remain in the printed temporary directory. The command never
prints the API key.

Local data is stored under `~/.local/share/mathlab_linux` by default. Override
it with `--data-root` or `MATHLAB_DATA_ROOT`.

## Contract editing

`/edit` writes the current Working Draft to a Markdown file and opens `$EDITOR`.
After saving, MathLab parses and validates the document and updates the mutable
Working Draft section by section. `/commit` creates the next immutable Contract
Version; `/approve` freezes that version and queues its Research Run.

## Safety boundary

This repository is a local single-user, terminal-first application. It does not
ship the web browser UI or browser assets. It contains no production database,
user data, deployment credentials, internal server addresses or private
operational runbooks. Long Research Runs should be executed on a machine
intended for computation rather than a shared login node.

## License

Apache-2.0. See [LICENSE](LICENSE).
