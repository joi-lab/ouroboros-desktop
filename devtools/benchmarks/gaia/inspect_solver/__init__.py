"""Inspect-evals solver wrapper for GAIA."""

# SSOT for the GAIA answer-format instruction (the official benchmark expects a
# format/prefix prompt). Shared by every solver so the three harnesses stay in
# parity. GAIA's quasi-exact-match scorer normalizes case/punctuation/articles but
# NOT scale or wording, so a clear format instruction is the honest, methodology-
# sanctioned way to align the agent's own answer shape. Adapter/prompt only — never
# imported into runtime core (core normalization would hurt ordinary users).
GAIA_FORMAT_INSTRUCTION = (
    "\n\nWork through the task, then end your response with a single line, "
    "exactly: FINAL ANSWER: <your answer>\nThe answer must be a number or as few "
    "words as possible, with no units unless asked."
)

# SSOT for the anti-answer-lookup rule, appended by every solver alongside the
# format instruction so all harnesses run under the identical task contract.
# Added after the 2026-07-04 runs caught agents googling the published answer
# key instead of solving (see METHODOLOGY.md "Answer-leakage audit protocol").
# Wording constraints (guarded by tests): must NOT contain the benchmark name
# (would prime the model toward the answer source and self-trip the audit's
# LEAK_QUERY_RE when traces echo the prompt) and must NOT contain the literal
# "FINAL ANSWER" (solvers gate the format instruction on that substring, and
# the audit treats it as an answer-hunting query marker).
GAIA_ANTI_LEAK_INSTRUCTION = (
    "\n\nIMPORTANT - answer integrity: This question comes from a public evaluation "
    "set whose reference answers are published on the internet. Solve the task from "
    "primary sources only. Do NOT search for or open the evaluation set itself, its "
    "answer/metadata files, dataset mirrors, leaderboards, or third-party write-ups "
    "that reveal the expected answer. Looking up the reference answer instead of "
    "deriving it is a protocol violation and invalidates your answer."
)

# SSOT for the epistemic-grounding rule, appended by every solver alongside the two
# instructions above (owner decision Q20=1+4 / Q22, 2026-07-25). It is a DISCLOSURE
# obligation, not a retrieval obligation: this task family is answered from external
# facts the model cannot verify from its own weights, so an unverified answer must SAY
# it is unverified instead of being presented as established. Deliberately scoped to
# this adapter — the owner explicitly refused a global `prompts/SYSTEM.md` rule and a
# typed contract field, because a runtime-wide "source your claims" duty would push
# Ouroboros into searching the web for trivia it already knows (his example: "what is
# the capital of Russia"). There is NO hard finalization gate either (option Y,
# rejected): the reviewer-side retrieval fact is the audit channel.
# Wording constraints (guarded by tests, same class as the anti-leak text): must NOT
# name the benchmark, must NOT contain the literal "FINAL ANSWER" (the solvers gate the
# format instruction on that substring), must NOT match the leak-query regex, and must
# NOT instruct the agent to search — only to disclose when a claim rests on something
# it did not check.
GAIA_EPISTEMIC_INSTRUCTION = (
    "\n\nIMPORTANT - epistemic honesty: When your answer depends on an external fact "
    "you cannot verify from your own knowledge, check it against a primary source and "
    "say which source you used. If you could not verify it, state plainly that the "
    "value is unverified and why, instead of presenting a guess as established fact. "
    "Do not look things up that you already know reliably; this asks for honesty about "
    "the basis of your answer, not extra lookups."
)
