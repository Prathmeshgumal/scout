# Working on career-lens

- Keep the pipeline shape: request → capture → observation.
- Tables marked append-only are enforced by triggers and grants. Never relax
  either to make a test pass. A new table must declare its mutability.
- Keep `partial`, `absent`, `not_captured` and `failed` distinct.
- Schema changes are Alembic revisions added after `0001_baseline`. Update
  `store/schema/` in the same change; `tests/test_schema_agreement.py` checks it.
- Never log, return, fixture or commit passwords, cookies, bearer tokens,
  credential keys, browser profiles, HAR files or unredacted personal data.
- Browser profiles are live credentials. Never copy, reset or delete one, and
  never automate sign-in or sign-out without explicit authorization.
- Automated tests never contact LinkedIn, Google or any other live site.
- Do not bulk-rewrite fixtures. They are reviewed and redacted by hand.
- Before calling work done, run `uv run ruff check src tests scripts` and
  `uv run pytest -q`.

## UI controls

- Do not use native `select`, checkbox, radio, date or time pickers. Their open state ignores the theme.
- Use `web/src/components/ui/`. `web/src/test/native-controls.test.ts` checks this.
- Check each open state, such as a dropdown list, in dark mode.
