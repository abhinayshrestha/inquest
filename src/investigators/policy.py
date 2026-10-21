"""Local Investigator budgets. Read as policy.<NAME> at call time so tests can tighten them.

The executor keeps its own, looser per-assignment limits (src/tools/policy.py); these
stop the graph first so it still has room to conclude."""

MAX_MODEL_CALLS = 20
# Conclude and its one repair attempt run even when the agent loop has used its share.
RESERVED_CONCLUDE_CALLS = 2
MAX_TOOL_CALLS = 15
# Includes reasoning tokens, which Nebius counts as output.
MAX_TOKENS = 150_000
RESERVED_CONCLUDE_TOKENS = 20_000
# Anchored on assignment.started_at, so a resumed run cannot outlive it.
WALL_CLOCK_SECONDS = 300.0
# Soft stop: the agent loop yields to conclude this long before the hard deadline.
CONCLUDE_HEADROOM_SECONDS = 60.0
# Times conclude may send the agent back for more evidence.
MAX_NEED_MORE = 1
MAX_REPAIRS = 1
MAX_HYPOTHESES = 5
MAX_FINDINGS = 5
# Consecutive transport-level MCP failures before the investigator gives up on tools.
MAX_MCP_FAILURES = 3
MCP_CALL_TIMEOUT_SECONDS = 60.0
MAX_PARALLEL_TOOL_CALLS = 3

# Fewer spans than this behind a latency figure is a sparse sample: the finding must say
# so and cannot be confirmed on it alone.
SPARSE_SAMPLE = 30
MAX_GAPS_NOTED = 5
# A deterministic finding for an unanswered objective topic: its newest evidence, clipped.
TOPIC_FINDING_EVIDENCE = 3
TOPIC_FINDING_CHARS = 300

# Model-facing context
TOOL_RESULT_CHARS = 3_000
# Older tool results are shown to the model as one-line digests.
FULL_TOOL_RESULTS_KEPT = 4
# Earlier evidence in a Local brief: the newest few, summaries shortened. The full
# catalog (with refs) is still shown at conclude time.
BRIEF_EVIDENCE_ITEMS = 10
BRIEF_PRIOR_SUMMARY_CHARS = 200
CATALOG_EVIDENCE_ITEMS = 60
EVIDENCE_SUMMARY_CHARS = 400

# Main Investigator (one turn)
MAIN_MAX_ROUNDS = 3
MAIN_MAX_MODEL_CALLS = 10
MAIN_MAX_TOKENS = 120_000
MAIN_MAX_REPAIRS = 1
# Findings a fallback answer lists when no model answer survives validation.
FALLBACK_FINDINGS = 6
# Covers up to three Local rounds (WALL_CLOCK_SECONDS each) plus Main's own calls.
TURN_WALL_CLOCK_SECONDS = 1_200.0
# Investigation memory shown to Main per call; findings first, evidence trimmed first.
MEMORY_CHARS = 24_000
MEMORY_EVIDENCE_ITEMS = 80
MAX_ENVIRONMENTS_PER_ROUND = 5
# Follow-ups: earlier answers shown to Main, each clipped.
CONVERSATION_ANSWER_CHARS = 600
# Long nested reports through Nebius json_schema guided decoding made Ultra emit the whole
# JSON and then loop on whitespace instead of closing it (finish_reason=length, live run
# 2026-10-18); function_calling returned valid output for the same prompt. Assess keeps
# json_schema: its short verdicts never degenerated.
MAIN_COMPOSE_METHOD = "function_calling"
