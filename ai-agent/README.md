# ai-agent

A local, CPU-only, **read-only** AI agent for asking natural-language
questions about the k8s-lab cluster - health, root cause analysis,
resource right-sizing, capacity forecasts, cost optimization - backed
by [`cluster-stats`](../cluster-stats/) and
[`cluster-monitor`](../cluster-monitor/)'s existing REST APIs, not the
Kubernetes API directly. This is a learning project, not a production
service yet - see "What's not built (yet)" below.

Runs on the `llm` VM (`192.168.56.30`, see the `Vagrantfile`), which
has no GPU passthrough at all (VirtualBox gives a Linux guest none),
so "CPU-only" here isn't a config choice - it's the only mode
available, and `provisioning/llm.sh` pins it explicitly anyway (see
that file's comments for why).

## Why `qwen2.5:3b-instruct`, not a bigger or "smarter" model

Started with `qwen3:4b` for its hybrid thinking mode - abandoned it
after real testing, not guesswork:

- `"think": false` (the documented API parameter to disable reasoning)
  was not honored on this Ollama version (`0.33.3`) - every response
  embedded the full internal monologue in `content` regardless, with
  no separate `thinking` field.
- That reasoning is non-deterministic enough in length to be unusable
  for an interactive agent: three identical one-line prompts ("Say
  hello in one sentence") produced 330, 414, and 1778 reasoning
  tokens, and 14-124 second response times - confirmed via a
  correlated `top` trace to be the model itself, not VM/host CPU
  contention (the VM got its full 4-vCPU allocation uncontested during
  generation).

`qwen2.5:3b-instruct` predates the hybrid-thinking feature entirely -
nothing to fail to disable - and answered the identical prompt in a
consistent ~10 tokens, every time, ~0.3s once warm. Smaller footprint
(~2GB vs. `qwen3:4b`'s ~2.5GB) too. Same Qwen tool-calling training
that made the 3B/4B/7B sizes attractive in the first place.

## Two real accuracy bugs found and fixed along the way

Both found by comparing the model's answer against the actual JSON
from the same endpoint, not by assumption:

1. **Misreading a specific field while narrating several at once** -
   asked to summarize `get_cluster_health`'s output, the model reported
   the control-plane components as having "no restarts" when the data
   said `"restarts": 1` on every one of them. A system prompt telling
   it explicitly not to make this exact mistake did **not** fix it.
   What did: **flattening the nested JSON into plain text before it
   ever reaches the model** (see `format_cluster_health` /
   `format_resource_optimization` / etc. in `agent_test.py`) - small
   models are far more reliable reading flat lines than correctly
   indexing into JSON structure while also composing prose. This is
   the single most impactful lesson from building this: push
   precision-critical parsing into tested Python, and only ask the
   model to turn already-clear text into prose.
2. **Editorializing about magnitude** - called 6.4% CPU usage "quite
   high." Fixed by an explicit system prompt instruction (`SYSTEM_PROMPT`
   in `agent_test.py`) telling it to report numbers exactly as given
   and never characterize them as high/low/concerning unless the data
   itself already does. Unlike bug #1, a prompt instruction *did* fix
   this one - the difference being this was about *phrasing*, not
   about correctly tracking a value.

A third, subtler issue was found and left as a known limitation (see
below): given `get_cluster_health`'s health-score deduction ("4
critical + 77 warning active issue(s)") alongside `risk_factors`
(which only itemizes the critical ones, by design - warnings aren't
individually listed), the model once conflated the two into "4 out of
4 risk factors," implying all 81 issues were the 4 listed ones. Every
individual number was read correctly this time; the *relationship*
between two correctly-read numbers was wrong. That's a different, more
fundamental kind of error than the first two, and not one flattening
or a prompt instruction reliably fixes - a real signal about where a
3B model's synthesis ability actually tops out, worth knowing before
trusting it with anything more complex.

## The tools (`agent_test.py`)

All read-only `GET` calls through the `gateway` (this VM isn't a
cluster member, so it reaches both apps the same way a browser does -
`192.168.56.11:30090`, never a ClusterIP):

| Tool | Backend | Answers |
|---|---|---|
| `get_cluster_health` | cluster-monitor `/api/cluster/health` | overall health, control plane, saturation |
| `get_resource_optimization` | cluster-stats `/api/optimization` | what to resize |
| `get_active_issues` | cluster-monitor `/api/issues?active=true` | what's wrong right now |
| `get_incident_analysis` | cluster-monitor `/api/incidents` | RCA for actively crashing pods |
| `get_predictions` | cluster-monitor `/api/predictions` | capacity forecasts |
| `get_hpa_status` | cluster-stats `/api/autoscaling/hpa` | autoscaler state |
| `get_cost_optimization` | cluster-stats `/api/cost-optimization` | idle/underutilized nodes |
| `get_node_status` | cluster-stats `/api/nodes` | per-node health |
| `get_recent_events` | cluster-monitor `/api/events` | raw event stream, last hour |

Verified live: 4 different natural-language questions, each correctly
routed to a different one of the 9 tools purely from phrasing (no
questions were pre-mapped to a tool) - real evidence this model
distinguishes between tools reliably, not just "uses the one tool it's
ever seen."

## Running it

On the `llm` VM:

```bash
python3 /ai-agent/agent_test.py                                   # default question (cluster health)
python3 /ai-agent/agent_test.py "Any nodes I could shut down?"     # your own question
```

Prints the tool the model chose, the flattened text it actually saw,
and the final answer - useful for spot-checking accuracy against the
real API response (`curl` the same gateway URL directly to compare).

## What's not built (yet)

- **Answer synthesis/prioritization** - answers are currently accurate
  but close to a verbatim reformat of the tool output for anything
  with a list (e.g. resource optimization), rather than a prioritized
  "here's your single biggest win" summary.
- **A persistent loop** - right now it's one question per script
  invocation (a fresh process, fresh conversation each time).
- **Proactive alerting** - a background loop that periodically checks
  `get_cluster_health`/`get_active_issues` and surfaces a plain-language
  alert when something changes for the worse, unprompted.
- **Voice** - Whisper.cpp (speech-in) and Piper (speech-out) around the
  same agent loop - the eventual goal, deliberately last.
- **Wiring `provisioning/llm.sh` into a fully automated `vagrant up`** -
  see the Vagrantfile: the `llm` VM's provisioning is now automated
  (Ollama install + model pull), but this whole `ai-agent/` layer is
  still a manually-run script, not a system service.
