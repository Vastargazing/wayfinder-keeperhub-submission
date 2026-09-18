#!/usr/bin/env python3
"""Compatibility entry point for the portable offline mutation runner."""
from pathlib import Path
import runpy
runpy.run_path(str(Path(__file__).resolve().parents[2] / 'integration/scripts/offline_mutations.py'), run_name='__main__')
