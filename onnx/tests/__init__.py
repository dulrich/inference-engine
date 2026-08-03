"""Unit tests for the pure boundaries of `inference-engine-onnx`.

Plain `unittest`, no pytest: this repo carries no Python test toolchain and adds none. Run from
the repo root with the gate venv (see `onnx/AGENTS.md`):

    onnx/.venv/bin/python -m unittest discover -s onnx/tests -t onnx -v

These cover the *validation* boundary only. They are not a substitute for the export-time torch
parity gate, which is the only thing that proves the prompt bytes are right.
"""
