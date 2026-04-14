# Ouroboros — common development commands
# Usage: make test, make lint, make health

.PHONY: test lint health clean

# Run smoke tests (fast, no external deps needed at runtime)
test:
	uv run pytest tests/ -q --tb=short

# Run smoke tests with verbose output
test-v:
	uv run pytest tests/ -v --tb=long

# Run codebase health check (requires ouroboros importable)
health:
	python3 -c "from ouroboros.review import compute_complexity_metrics; \
		import pathlib, json; \
		m = compute_complexity_metrics(pathlib.Path('.')); \
		print(json.dumps(m, indent=2, default=str))"

# Clean Python cache files
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
