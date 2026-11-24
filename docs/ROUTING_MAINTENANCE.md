# Closed ASK routing

ASK asks SemanticRouter to select a capability; only a validated plan is dispatched.
Classifier failure returns an unavailable response. It never falls back to Wiki.

To add an operation:
1. Describe its actual measurement and exclusions in catalog.CATALOG.
2. Define arguments in network_plan.argument_schema. The same schema is sent to the model and validated locally.
3. Implement its adapter, preserving identity, permissions and radio restrictions.
4. Add dispatch/validation tests and live classification examples, including paraphrases and unsupported cases.

The lexical AssistantRouter is legacy code, not used by ASK dispatch. Catalog fallback/override helpers were removed.
Active-radio intent checks remain conservative; DM/admin/channel permissions are checked by adapters.
No generated SQL is accepted. Country/target values must be present in the question.

Run test_catalog_dispatch.py and test_near_first_hop.py for deterministic contracts.
These mock the classifier and do NOT measure model quality. Evaluate the real configured
model separately without executing selected operations or transmitting radio packets.
Monitor Catalog logs for elapsed time, operation and errors. A timeout is not a semantic decision.

ROUTING_DIALOG_EXAMPLES in catalog.py provides short contrasting dialogues to the model; ROUTING_EXAMPLES also records regression examples. No lexical overrides.
Ask_Command.semantic_timeout_seconds defaults to 45 seconds (1–60), separate from execution timeout.
The CPU-hosted model must be evaluated cold and warm; passing contract tests alone is insufficient.

Live evaluation: `python tests/eval_catalog_live.py --config /etc/meshcore-bot/config.ini`.
The fixture is `tests/fixtures/routing_cases.json`. Output is JSON per case, including
arguments, latency and pass/fail. Exit is nonzero on failure. No capability is executed.
Add held-out paraphrases as well as production failures before accepting a model change.
Held-out paraphrases: pass `--cases tests/fixtures/routing_heldout.json`; do not
copy them into the prompt to make a failing evaluation pass.

## Relay usage and concise conversation (2026-09-28)
`mesh/relay_usage` uses packet_stream receive timestamps, packet hashes and explicit bytes_per_hop.
Never count observed_paths rows: they aggregate paths and retain the initial hash.
Only flood paths are traversed-path evidence; direct routes may describe planned hops.
Count each packet once per resolved relay across all observed paths; ignore unknown or
colliding prefixes and paths without usable hashes/encoding metadata. Default 24 h,
3 results. This includes adverts and is local observation, not total network load.
Output uses complete rows within the caller's UTF-8 budget.

ASK general conversation passes the transport byte budget into LlmService. The model
is instructed to answer directly without a systematic introduction. Over-budget text
gets one compression attempt, then a complete-sentence boundary. Wiki remains unchanged.

## Graph connectivity replaces relay packet ranking
ASK now selects mesh/relay_connectivity. Counts incident directed graph edges,
matching computeNodeStats (reciprocal directions count separately). Default:
24 h edge timeframe, 24 h node timeframe, >=2 lifetime observations per edge,
all countries, geolocated repeater/roomserver nodes. Node and edge cutoffs follow the map's local wall-clock convention (including
its browser-side edge filter); compare in the same timezone as the bot host. Prefix ambiguity
is excluded rather than assigned arbitrarily. Browser-specific saved filters
are not available to ASK. This measures observed connectivity, not packet load.


## Reliability regression (2026-09-30)
The transport asks for `{function: "route.operation", args: {...}}` rather than
repeating route and operation fields. Both the JSON grammar and local validation
are generated from the existing closed catalogue; no keyword override is added.
Keep semantic examples: removing them degraded the actual 2B model's choices.
Routing logs include prompt/cache/completion token counts and elapsed time.
An invalid plan gets at most one model repair within the same deadline; HTTP
failures are not retried because inference may still be running upstream.
The 45-second default provides margin over measured CPU inference; explicitly
configured timeouts still take precedence. An unavailable model never executes a tool.

`mesh.count_nodes` counts distinct public keys across all roles, using local
`complete_contact_tracking.last_heard` advert observations. Active defaults to
24 hours; 0 means all known identities. The model chooses hours and an optional
literal country. These are parameterized queries, not generated SQL. Date filters
use the producer's local naive clock (`repeater_manager.datetime.now()`), exclude
future dates, and do not hard-code UTC+2. Output states the observation scope and
passes through the existing numerical-preserving LLM reformulation.

Run `tests/fixtures/routing_reliability.json` via eval_catalog_live.py for original
failures AND unseen paraphrases. Run tests/test_count_nodes.py for counting,
argument validation and the tool-to-LLM handoff. Live evaluation sends no RF.

## Clarification and concrete statistics (2026-10-09)

The LLM may select `llm.clarify` when the expected measurement is unclear. This
asks one brief question through LlmService, with the caller's UTF-8 budget and
without wiki retrieval or global channel context. A precise unavailable measure
still uses `mesh.unsupported`; casual conversation uses `llm.chat`.

Model-facing statistics name the actual result: `bot_users`, `channel_traffic`,
`bot_usage`, `longest_paths`, `advert_counts`. They map to the existing
`mesh.stats` adapter and its fixed topic after strict argument validation.
The model still selects the function; this mapping never reads the user's words.
The `hashes` option, command permissions and usage metrics are preserved.

Keep telemetry measurements distinct from bot/network activity in the model's
instructions. Adding examples alone did not reliably generalize with the current
2B model. Test paraphrases and log transport timeouts separately from wrong plans.
