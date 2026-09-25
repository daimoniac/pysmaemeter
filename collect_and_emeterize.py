#!/usr/bin/env python3
"""Backward-compatible entry point for the PV collector."""

from pathlib import Path
import runpy

runpy.run_path(
    str(Path(__file__).resolve().parent / 'collect_and_emeterize' / 'collect_and_emeterize.py'),
    run_name='__main__',
)
