# Troubleshooting Log

## 2026-06-15 — 403 Errors from Playtomic API

### Symptoms
All clubs returned `403 Client Error: Forbidden` in Cloud Function logs. Function had been working fine for months.

### Diagnosis Steps
1. Opened the Playtomic URL directly in browser — returned JSON fine.
2. Cloud Function hit same URL — returned 403.
3. Added response body logging to confirm: CloudFront returned `"Request blocked"` HTML page.
4. Conclusion: Playtomic's AWS CloudFront CDN was temporarily blocking Google Cloud IP ranges.

### Root Cause
Temporary IP block by Playtomic's CloudFront CDN. Not a permanent policy change — resolved on its own within a few hours. Cloud provider IPs (Google Cloud, GitHub Actions, etc.) are on known lists that CDN WAF rules can block.

### How to Confirm IP Block
- Open the URL in your browser: if it returns JSON, the API works — the block is IP-based.
- In Cloud Function logs, look for `"Request blocked"` in the 403 response body — that's CloudFront's block page.

### Additional Bug Fixed
While debugging, discovered the script had a brotli compression bug. The `Accept-Encoding: gzip, deflate, br` header caused the server to return brotli-compressed responses, which the `requests` library can't decode by default. Fixed by removing `Accept-Encoding` from headers in `main_v3_github_actions.py`.

**Symptom of brotli bug:** `Expecting value: line 1 column 1 (char 0)` error with status 200.

### Resolution
IP block resolved on its own. Redeployed original `main_v1.py` to Google Cloud Function — working again.

### How to Test Locally
If the Cloud Function fails, test from your laptop (residential IP is never blocked):

```
cd "C:\Users\Roberto\Desktop\Claude\padel-function"
python run_local.py
```

If it works locally but not on Cloud Function → IP block (wait or use proxy).
If it fails locally too → something else is wrong with the script or credentials.

### File Inventory
| File | Description |
|------|-------------|
| `main.py` | Currently deployed to Google Cloud Function |
| `main_v1.py` | Original working version (backup) |
| `main_v2.py` | Added browser-like headers + response body logging |
| `main_v3.py` | ScraperAPI proxy version (not used) |
| `main_v3_github_actions.py` | Standalone script for GitHub Actions (brotli fix applied) |
| `run_local.py` | Run the script locally using env.yaml credentials |

### GitHub Actions Backup
A fully working GitHub Actions workflow is set up at `.github/workflows/padel-occupancy.yml`.
- Runs `main_v3_github_actions.py` daily at 06:30 BKK time
- Secrets already configured in the GitHub repo
- Currently **disabled** — re-enable at GitHub repo → Actions → Padel Occupancy Daily Report → Enable workflow
- Use this if Google Cloud gets blocked again
