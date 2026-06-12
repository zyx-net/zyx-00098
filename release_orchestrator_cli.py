#!/usr/bin/env python3
"""Convenience entry-point script.

Equivalent to running:
    python -m release_orchestrator <command> [options]
"""
from release_orchestrator.__main__ import main
import sys

if __name__ == "__main__":
    sys.exit(main())
