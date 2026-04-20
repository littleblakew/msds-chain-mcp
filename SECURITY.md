# Security & Privacy Policy

## Data Handling

### What we process
- Chemical names and CAS numbers submitted in tool calls
- Natural language safety questions
- Protocol text submitted to `validate_protocol_chemicals`

### What we do NOT do
- **No training:** Your queries are never used to train AI models
- **No storage of query content:** Stateless tool calls (`check_chemical_compatibility`, `get_ppe_recommendation`, etc.) are processed and discarded. No query text is persisted.
- **No third-party sharing:** Query data is not shared with or sold to any third party

### What we log (for service reliability)
- Tool name called
- Chemical names (for analytics: hit rate, coverage gaps)
- Timestamp, duration, success/failure
- API key identifier (hashed, not the key itself)

Logs are retained for 90 days and are used solely for service monitoring, debugging, and improving chemical data coverage.

### Audit sessions
When you use `create_audit_session`, a persistent record is created containing:
- Experiment name
- Chemical list
- Compatibility matrix results
- Risk warnings

This data is associated with your API key and retained indefinitely (it's the point of an audit — to have a record). You can request deletion via contact@lagentbot.com.

## Authentication

### Local mode (stdio)
- API key stored in environment variable, never transmitted except to MSDS Chain backend over HTTPS
- No authentication between your AI agent and the local MCP server (they share the same process)

### Remote mode (HTTP)
- All communication over TLS (HTTPS)
- API key transmitted via `X-API-Key` header
- OAuth 2.1 support planned for Marketplace integration (PKCE + DCR)

## Rate Limiting

| Tier | Limit | Description |
|------|-------|-------------|
| Free | 30 requests/minute | For evaluation and personal use |
| Standard | 120 requests/minute | For active research use |
| Enterprise | Custom | Contact us for higher limits |

Rate limit headers are returned in HTTP responses:
- `X-RateLimit-Limit`
- `X-RateLimit-Remaining`
- `X-RateLimit-Reset`

## Infrastructure

- Backend hosted on **Azure Container Apps** (Southeast Asia region)
- Database: **Azure PostgreSQL Flexible Server** (encrypted at rest, TLS in transit)
- AI processing: **Azure OpenAI Service** (data stays within Azure, not used for model improvement per Microsoft DPA)
- No data leaves the Azure Southeast Asia region

## Compliance

- ISO 27001 certification in progress (policies complete, audit pending)
- GDPR: Data minimization, right to erasure, DPA available on request
- SOC 2 Type II: Planned for 2026 Q4

## Vulnerability Reporting

If you discover a security vulnerability, please report it responsibly:
- Email: security@lagentbot.com
- Do NOT open a public GitHub issue for security vulnerabilities
- We aim to respond within 48 hours and patch critical issues within 7 days

## Contact

- General: contact@lagentbot.com
- Security: security@lagentbot.com
- Data deletion requests: privacy@lagentbot.com
