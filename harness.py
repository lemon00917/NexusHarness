"""
NexusHarness CLI Entry Point
============================
Shim to allow running `python harness.py` from project root.
"""

import sys
import os

# Add project root to path so `from microharness import` works
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from microharness.harness import main

if __name__ == "__main__":
    main()