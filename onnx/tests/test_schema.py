"""`server.schema` — the request-schema boundary.

Two things are being pinned here:

1. **Ordering.** Struct order, field order, and — the subtle one — *description* order, which
   is the description mapping's own insertion order filtered by field membership, NOT field
   order. The export-time parity gate compares byte-exact input ids against gliner2's own
   collator, so a reordering here is a silent wrong-answer bug, not a test failure. These tests
   catch it in a second instead of in a 90-second torch run.
2. **Strictness.** Every malformed value raises `SchemaError` and nothing else. The bug that
   motivated this module was `float(None)` raising a bare `TypeError` past
   `except SchemaError`, out of `do_POST`, into `socketserver` — where the client received no
   HTTP response at all.

This module imports stdlib + `server.schema` only, deliberately: no numpy, no onnxruntime.
"""

from __future__ import annotations

import unittest

from server.schema import (
    ClassificationSpec,
    EntitySpec,
    SchemaError,
    compile_schema,
    struct_tokens,
)


class TestOrdering(unittest.TestCase):
    def test_entities_struct_comes_first_then_classifications_in_order(self):
        specs = compile_schema({
            "classifications": [
                {"name": "severity", "labels": ["low", "high"]},
                {"name": "event_type", "labels": ["fire", "flood"]},
            ],
            "entities": ["person", "location"],
        })
        self.assertEqual(
            [type(s).__name__ for s in specs],
            ["EntitySpec", "ClassificationSpec", "ClassificationSpec"],
        )
        self.assertEqual([s.name for s in specs], ["entities", "severity", "event_type"])

    def test_field_order_is_supplied_order_for_both_entity_forms(self):
        listed = compile_schema({"entities": ["zebra", "apple", "mango"]})
        self.assertEqual(listed[0].fields, ("zebra", "apple", "mango"))
        mapped = compile_schema({"entities": {"zebra": "z", "apple": "a", "mango": "m"}})
        self.assertEqual(mapped[0].fields, ("zebra", "apple", "mango"))

    def test_description_order_is_description_order_not_field_order(self):
        """The single most breakable rule in this file.

        Descriptions are supplied as c, a (and an unknown key z that must be dropped) while the
        labels are a, b, c. The prompt must read "c" then "a". Rebuilding the descriptions from
        `fields` — the obvious "tidy-up" — would emit "a" then "c" and change every logit.
        """
        specs = compile_schema({
            "classifications": [{
                "name": "task",
                "labels": ["a", "b", "c"],
                "descriptions": {"c": "see", "a": "ay", "z": "not a label"},
            }],
        })
        spec = specs[0]
        self.assertEqual(spec.descriptions, (("c", "see"), ("a", "ay"), ("z", "not a label")))
        self.assertEqual(
            struct_tokens(spec),
            [
                "(",
                "[P]",
                "task [DESCRIPTION] c: see [DESCRIPTION] a: ay",
                "(",
                "[L]", "a",
                "[L]", "b",
                "[L]", "c",
                ")",
                ")",
            ],
        )

    def test_entity_dict_description_order_and_non_string_values(self):
        """A non-string value means *no description*; the key is still a field."""
        specs = compile_schema({
            "entities": {"person": "who", "count": 7, "location": "where"},
        })
        spec = specs[0]
        self.assertEqual(spec.fields, ("person", "count", "location"))
        self.assertEqual(spec.descriptions, (("person", "who"), ("location", "where")))
        self.assertEqual(
            struct_tokens(spec),
            [
                "(",
                "[P]",
                "entities [DESCRIPTION] person: who [DESCRIPTION] location: where",
                "(",
                "[E]", "person",
                "[E]", "count",
                "[E]", "location",
                ")",
                ")",
            ],
        )

    def test_empty_entity_description_is_kept(self):
        """Matches `Schema._parse_entity_input`: "" is a description, not an absence."""
        spec = compile_schema({"entities": {"person": ""}})[0]
        self.assertEqual(spec.descriptions, (("person", ""),))
        self.assertIn("entities [DESCRIPTION] person: ", struct_tokens(spec))

    def test_undescribed_struct_renders_a_bare_parent(self):
        spec = compile_schema({"entities": ["person"]})[0]
        self.assertEqual(struct_tokens(spec), ["(", "[P]", "entities", "(", "[E]", "person", ")", ")"])

    def test_label_descriptions_alias_is_still_honoured(self):
        spec = compile_schema({
            "classifications": [{
                "name": "task",
                "labels": ["a"],
                "label_descriptions": {"a": "ay"},
            }],
        })[0]
        self.assertEqual(spec.descriptions, (("a", "ay"),))


class TestValidValues(unittest.TestCase):
    def test_defaults(self):
        spec = compile_schema({"classifications": [{"name": "t", "labels": ["a"]}]})[0]
        self.assertIsInstance(spec, ClassificationSpec)
        self.assertIs(spec.multi_label, False)
        self.assertEqual(spec.cls_threshold, 0.5)

    def test_threshold_fallback_chain(self):
        def threshold_of(entry):
            entry = {"name": "t", "labels": ["a"], **entry}
            return compile_schema({"classifications": [entry]})[0].cls_threshold

        self.assertEqual(threshold_of({"cls_threshold": 0.9}), 0.9)
        self.assertEqual(threshold_of({"threshold": 0.7}), 0.7)
        # cls_threshold wins over threshold.
        self.assertEqual(threshold_of({"cls_threshold": 0.9, "threshold": 0.1}), 0.9)
        # An int is a number; it must survive as a float without a coercion complaint.
        self.assertEqual(threshold_of({"cls_threshold": 1}), 1.0)

    def test_multi_label_accepts_real_booleans_only(self):
        spec = compile_schema({
            "classifications": [{"name": "t", "labels": ["a"], "multi_label": True}],
        })[0]
        self.assertIs(spec.multi_label, True)

    def test_task_is_accepted_as_a_name_alias(self):
        spec = compile_schema({"classifications": [{"task": "sev", "labels": ["a"]}]})[0]
        self.assertEqual(spec.name, "sev")

    def test_empty_unsupported_key_stays_a_no_op(self):
        """`relations: []` is falsy and requests nothing — deliberately NOT a new 400."""
        specs = compile_schema({"entities": ["person"], "relations": []})
        self.assertEqual(len(specs), 1)

    def test_specs_are_frozen(self):
        spec = compile_schema({"entities": ["person"]})[0]
        self.assertIsInstance(spec, EntitySpec)
        with self.assertRaises(Exception):
            spec.fields = ("other",)  # type: ignore[misc]


class TestEmptyEntities(unittest.TestCase):
    """Null and empty are treated differently on purpose — see `_entity_spec`'s docstring.

    An explicit null is a client mistake. An empty container is a coherent "no entity labels
    requested", it can fall out of a programmatically-built schema, and it returned 200 before
    the strict pass — so it still does.
    """

    def test_null_entities_is_an_error(self):
        with self.assertRaises(SchemaError) as ctx:
            compile_schema({"entities": None, "classifications": [{"name": "t", "labels": ["a"]}]})
        self.assertIn("null", str(ctx.exception))

    def test_empty_list_alongside_a_classification_compiles(self):
        specs = compile_schema({
            "entities": [],
            "classifications": [{"name": "t", "labels": ["a"]}],
        })
        self.assertEqual(len(specs), 1)
        self.assertIsInstance(specs[0], ClassificationSpec)
        self.assertEqual(specs[0].name, "t")

    def test_empty_object_alongside_a_classification_compiles(self):
        specs = compile_schema({
            "entities": {},
            "classifications": [{"name": "t", "labels": ["a"]}],
        })
        self.assertEqual(len(specs), 1)
        self.assertIsInstance(specs[0], ClassificationSpec)
        self.assertEqual(specs[0].name, "t")

    def test_empty_entities_alone_still_fails_the_must_contain_check(self):
        for empty in ([], {}):
            with self.subTest(empty=empty):
                with self.assertRaises(SchemaError) as ctx:
                    compile_schema({"entities": empty})
                self.assertIn("must contain", str(ctx.exception))


class TestMalformedThreshold(unittest.TestCase):
    """The live bug: each of these used to escape as ValueError/TypeError, not SchemaError."""

    def _reject(self, entry_extra):
        entry = {"name": "t", "labels": ["a"], **entry_extra}
        with self.assertRaises(SchemaError):
            compile_schema({"classifications": [entry]})

    def test_cls_threshold_null(self):
        self._reject({"cls_threshold": None})

    def test_cls_threshold_string(self):
        self._reject({"cls_threshold": "high"})

    def test_cls_threshold_empty_string(self):
        self._reject({"cls_threshold": ""})

    def test_cls_threshold_list(self):
        self._reject({"cls_threshold": [0.5]})

    def test_cls_threshold_object(self):
        self._reject({"cls_threshold": {}})

    def test_cls_threshold_bool(self):
        """`isinstance(True, int)` is True — a bool must not pass as 1.0."""
        self._reject({"cls_threshold": True})

    def test_cls_threshold_nan(self):
        self._reject({"cls_threshold": float("nan")})

    def test_cls_threshold_infinity(self):
        self._reject({"cls_threshold": float("inf")})

    def test_threshold_null(self):
        """A present-but-null key is an error, never a fallback to the default."""
        self._reject({"threshold": None})

    def test_threshold_string(self):
        self._reject({"threshold": "0.5"})


class TestMalformedSchema(unittest.TestCase):
    def _reject(self, schema):
        with self.assertRaises(SchemaError):
            compile_schema(schema)

    def test_multi_label_quoted_false(self):
        """`bool("false")` is True — the head mode must not silently flip."""
        self._reject({"classifications": [{"name": "t", "labels": ["a"], "multi_label": "false"}]})

    def test_multi_label_zero(self):
        self._reject({"classifications": [{"name": "t", "labels": ["a"], "multi_label": 0}]})

    def test_multi_label_null(self):
        self._reject({"classifications": [{"name": "t", "labels": ["a"], "multi_label": None}]})

    def test_entities_as_string(self):
        self._reject({"entities": "person"})

    def test_entities_as_int(self):
        self._reject({"entities": 7})

    def test_entities_as_null(self):
        # Empty list/object are NOT errors — see TestEmptyEntities.
        self._reject({"entities": None})

    def test_entity_label_not_a_string(self):
        self._reject({"entities": ["person", 7]})

    def test_classification_entry_not_an_object(self):
        self._reject({"classifications": ["severity"]})

    def test_classifications_not_a_list(self):
        self._reject({"entities": ["person"], "classifications": {"name": "t"}})

    def test_classifications_null(self):
        self._reject({"entities": ["person"], "classifications": None})

    def test_classification_without_a_name(self):
        self._reject({"classifications": [{"labels": ["a"]}]})

    def test_classification_name_not_a_string(self):
        self._reject({"classifications": [{"name": ["sev"], "labels": ["a"]}]})

    def test_classification_without_labels(self):
        self._reject({"classifications": [{"name": "t"}]})

    def test_classification_labels_empty(self):
        self._reject({"classifications": [{"name": "t", "labels": []}]})

    def test_classification_label_not_a_string(self):
        self._reject({"classifications": [{"name": "t", "labels": ["a", 2]}]})

    def test_classification_descriptions_not_an_object(self):
        self._reject({"classifications": [{"name": "t", "labels": ["a"], "descriptions": ["ay"]}]})

    def test_classification_description_value_is_a_list(self):
        """Unguarded `str(v)` used to render "[1, 2]" straight into the prompt."""
        self._reject({
            "classifications": [{"name": "t", "labels": ["a"], "descriptions": {"a": [1, 2]}}],
        })

    def test_classification_description_value_is_null(self):
        self._reject({
            "classifications": [{"name": "t", "labels": ["a"], "descriptions": {"a": None}}],
        })

    def test_schema_is_not_an_object(self):
        self._reject(["entities"])

    def test_schema_is_empty(self):
        self._reject({})

    def test_unsupported_keys_are_rejected(self):
        for key in ("relations", "structures", "json_structures"):
            with self.subTest(key=key):
                self._reject({"entities": ["person"], key: ["something"]})


if __name__ == "__main__":
    unittest.main()
