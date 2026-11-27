# Secret Scanning

The `Repository hygiene` GitHub Actions workflow runs on every push and pull request. Its scanner test enumerates every Git-tracked path, checks file contents for token-shaped OpenRouter keys, Shopify access tokens, and Telegram bot tokens, and rejects tracked files under `15_OBSERVABILITY/audit/`. Failure output suppresses token values.

Run the same check locally from `Nina/`:

```powershell
.\.venv\Scripts\python.exe -m pytest -v 17_TESTS\test_repository_hygiene.py
```

The scan intentionally matches credential-shaped values rather than bare prefixes, so documentation can safely name a credential type. It scans the current contents of tracked paths; stage newly added files before running it locally so Git includes them in `git ls-files`. Never paste a detected value into logs, issues, or pull requests. Remove it from the affected file and rotate the credential if it was real.
