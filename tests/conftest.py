import os
import sys

# Make the server modules (server.py, server_remote.py) importable
# from the repo root regardless of where pytest is invoked.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
