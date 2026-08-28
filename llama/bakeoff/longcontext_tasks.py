#!/usr/bin/env python3
"""The long-context task battery: single-needle literal recall, multi-needle ordered readback,
and passage-anchored QA (WP-R2 of `plans/repository-tooling-structural-remediation.md`; review
finding F7).

One `Task` subclass per scored battery entry, each owning both halves of its contract:
`build_trial` (what prompt this task constructs from a haystack at a position fraction) and
`score` (how a completion is graded against it). `TASKS` and `POSITION_FRACTIONS` are the
battery's registry and its position-sweep axis.

`SingleNeedleTask` is the LITERAL-recall half of `docs/long-context-collapse.md` Req 1;
`PassageQATask` is the deliberately-distinct NoLiMa-class non-literal half, and its questions
never reuse the answer-bearing term verbatim (asserted by `tests/test_tasks.py`, not merely by
eye). Nothing about the prompt text, the question wording, the `PASSAGE_QA_ITEMS` set, or the
scoring rules changed in this split -- the dry-run CLI digest pins all three tasks' assembled
prompt sizes and expected values.

Every public name here is re-exported from `longcontext` at its historical home.

Zero new runtime dependencies: Python 3 stdlib only.
"""

from __future__ import annotations

import random
from typing import Any, Dict, NamedTuple, Tuple

# `run_harness._generate_reference_code` is resolved through the module object at call time --
# same reason every other module here imports `run_harness` rather than the symbol.
import run_harness
from longcontext_corpus import insert_at_fraction, insert_multiple


# ---------------------------------------------------------------------------------------------
# Task battery: single-needle literal recall, multi-needle ordered readback, passage-anchored QA
# ---------------------------------------------------------------------------------------------

_MULTI_NEEDLE_SPREAD = 0.03  # fraction-of-haystack spacing between the 3 multi-needle markers
_MAX_INJECTION_FRACTION = 0.98  # keeps every injection point (and its spread siblings) in-bounds


class TrialSpec(NamedTuple):
    prompt: str
    expected: Any
    task: str
    position_fraction: float
    meta: Dict[str, Any]


class Task:
    """Base class for one battery task. `build_trial` consumes `rng` (continuing the SAME
    instance `assemble_haystack` was just called with -- see that function's docstring) to
    generate any needle/question content; `score` grades a completion's `content_text` against
    the `TrialSpec` `build_trial` returned."""

    name: str = "base_task"

    def build_trial(
        self, haystack_text: str, position_fraction: float, rng: random.Random
    ) -> TrialSpec:
        raise NotImplementedError

    def score(self, content_text: str, trial: TrialSpec) -> bool:
        raise NotImplementedError


class SingleNeedleTask(Task):
    """Literal-match recall (NIAH-style): a unique reference code is injected at
    `position_fraction`; the model must reproduce it verbatim. This is the LITERAL-recall half
    of doc Req 1's "separate literal-match recall from non-literal recall as distinct battery
    tasks" -- `PassageQATask` below is the deliberately-distinct non-literal half. A pass here
    does not bound performance on `PassageQATask` (doc §1's NoLiMa-class finding), which is
    exactly why the plan pins both as separate battery entries rather than one NIAH-only task.
    """

    name = "single_needle"

    def build_trial(self, haystack_text, position_fraction, rng):
        code = run_harness._generate_reference_code(rng)
        injected = f"Reference code for this section: {code}. "
        assembled, _ = insert_at_fraction(haystack_text, position_fraction, injected)
        question = (
            "\n\nQuestion: what is the exact reference code given above? "
            "Reply with only the code, no other text."
        )
        return TrialSpec(
            prompt=assembled + question,
            expected=code,
            task=self.name,
            position_fraction=position_fraction,
            meta={"expected_code": code},
        )

    def score(self, content_text, trial):
        return bool(content_text) and trial.expected in content_text


class MultiNeedleTask(Task):
    """Multi-needle recall: 3 distinct reference codes injected in ascending order, clustered
    near `position_fraction` (spaced `_MULTI_NEEDLE_SPREAD` apart) -- `position_fraction`
    remains the single independent variable per cell (plan's position-sweep dimension); the
    spread tests ORDERED readback within that neighborhood, not a second independent position
    axis. Correct requires all three codes present AND recovered in their original order.
    """

    name = "multi_needle"

    def build_trial(self, haystack_text, position_fraction, rng):
        fractions = [
            min(_MAX_INJECTION_FRACTION, position_fraction + i * _MULTI_NEEDLE_SPREAD)
            for i in range(3)
        ]
        codes = [run_harness._generate_reference_code(rng) for _ in range(3)]
        injections = [f"Checkpoint {i + 1} marker: {c}. " for i, c in enumerate(codes)]
        assembled, _ = insert_multiple(haystack_text, fractions, injections)
        question = (
            "\n\nQuestion: list the three checkpoint markers above, in the exact order they "
            "appeared in the text, separated by commas. Reply with only the three codes in "
            "order, no other text."
        )
        return TrialSpec(
            prompt=assembled + question,
            expected=tuple(codes),
            task=self.name,
            position_fraction=position_fraction,
            meta={"expected_codes": list(codes), "insertion_fractions": fractions},
        )

    def score(self, content_text, trial):
        if not content_text:
            return False
        positions = []
        for code in trial.expected:
            idx = content_text.find(code)
            if idx == -1:
                return False
            positions.append(idx)
        return positions == sorted(positions)


# Passage-anchored QA items (doc Req 1's NoLiMa-class task): each passage states a fact using an
# invented, distinctive multi-word answer term; each question asks about that fact via
# paraphrase (occupation/role/context), deliberately never repeating the answer term itself --
# the model must locate the right passage semantically, then recall a specific term it cannot
# find by literal string-matching the question against the haystack. Kept as a small, fixed,
# versioned set (this WP's own call, matching quality_corpus.jsonl's precedent of a small fixed
# battery rather than a growing one).
PASSAGE_QA_ITEMS: Tuple[Dict[str, str], ...] = (
    {
        "id": "qa-organ-restorer",
        "passage": (
            "Petra Lindqvist spent four decades restoring pipe organs in unheated parish naves "
            "across northern Sweden; her workshop kept a single brass instrument on the wall, a "
            "devotional relic she called the Vintergatan Bell, which she rang each solstice."
        ),
        "question": (
            "What did the organ restorer in Sweden call the brass object she rang every "
            "solstice?"
        ),
        "answer_keyword": "Vintergatan Bell",
    },
    {
        "id": "qa-astronomer-route",
        "passage": (
            "Ravi Chandrasekaran catalogued double stars for the district almanac from an "
            "observatory reachable only by an eleven-kilometer walk along a dry riverbed; "
            "colleagues nicknamed the route the Amber Path."
        ),
        "question": (
            "What name did Ravi's colleagues give to the long walking route to his observatory?"
        ),
        "answer_keyword": "Amber Path",
    },
    {
        "id": "qa-clockmaker-gift",
        "passage": (
            "The apprentice clockmaker Tomas Arrieta spent six years in a Zaragoza workshop "
            "tightening the gears of pocket watches; his master eventually gifted him a "
            "prototype timepiece known only as the Iron Sparrow."
        ),
        "question": (
            "What was the nickname of the prototype timepiece given to the young apprentice in "
            "Spain?"
        ),
        "answer_keyword": "Iron Sparrow",
    },
    {
        "id": "qa-glassworker-ledger",
        "passage": (
            "Mira Voss restored stained-glass windows for three cathedrals near Lake Geneva; "
            "the smallest chapel kept a hidden ledger of her repairs, a notebook the sacristan "
            "privately called the Cobalt Register."
        ),
        "question": (
            "What did the sacristan call the notebook that recorded the glassworker's repairs?"
        ),
        "answer_keyword": "Cobalt Register",
    },
)


class PassageQATask(Task):
    """Passage-anchored QA (doc Req 1's non-literal-recall half -- see module docstring and
    `SingleNeedleTask`). Injects one of `PASSAGE_QA_ITEMS`' short synthetic passages at
    `position_fraction`; the question shares minimal lexical overlap with the passage's
    answer-bearing term specifically (the exact `answer_keyword` string never appears in any
    `question` text above -- verified by this module's own test suite, not merely by eye).
    """

    name = "passage_qa"

    def build_trial(self, haystack_text, position_fraction, rng):
        item = rng.choice(PASSAGE_QA_ITEMS)
        assembled, _ = insert_at_fraction(haystack_text, position_fraction, item["passage"] + " ")
        question = f"\n\nQuestion: {item['question']} Answer in one short phrase."
        return TrialSpec(
            prompt=assembled + question,
            expected=item["answer_keyword"],
            task=self.name,
            position_fraction=position_fraction,
            meta={"qa_id": item["id"]},
        )

    def score(self, content_text, trial):
        if not content_text:
            return False
        return trial.expected.lower() in content_text.lower()


TASKS: Dict[str, Task] = {
    "single_needle": SingleNeedleTask(),
    "multi_needle": MultiNeedleTask(),
    "passage_qa": PassageQATask(),
}

POSITION_FRACTIONS: Tuple[float, ...] = (0.10, 0.45, 0.85)
