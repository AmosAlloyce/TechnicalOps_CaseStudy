# Tests

Focused tests for deterministic demo routing and after-hours handling.

Run with:
```bash
docker compose run --rm --no-deps triage-agent \
  python -m unittest discover -s tests -v
```

The command uses the triage image, so no host Python dependencies are required.
