# Security policy

Do not commit or attach Telegram bot tokens, OpenAI API keys, OAuth access/refresh tokens, passwords, private keys or production env files.

Use `.env.example` only as a list of variable names. Real values must be transferred through the organization's approved secret channel and stored outside Git and Jira.

If a secret is exposed, revoke or rotate it first, then remove it from every reachable Git revision and deployment artifact. Do not open a public issue containing the value.
