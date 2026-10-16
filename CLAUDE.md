# CLAUDE.md

## Project Stack
Backend: FastAPI (Python). Frontend: vanilla JavaScript (ES modules) + HTML/CSS, served by `Frontend/serve.py` (no build step — no React/TypeScript). Always run pattern/unit tests after modifying scanner logic, and run an end-to-end smoke test before declaring the build complete.

## Response Style
Keep responses concise and chunked; when generating large multi-file output, write files incrementally rather than emitting everything in one oversized response.
