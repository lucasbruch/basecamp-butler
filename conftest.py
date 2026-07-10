"""Placing an (empty) conftest at the repo root makes pytest add the root to
sys.path, so tests can `import app...` no matter how pytest is invoked."""
