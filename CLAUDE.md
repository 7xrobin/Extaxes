## Dev Server Policy

Never start a new dev server to preview changes.

Before any preview or verification step:
1. Check if a dev server is already running (e.g. `lsof -i :3000` or `curl -s http://localhost:3000`)
2. If running → use that port

Do not assume a default port. Check what is actually listening.

## Post-Work Logging

After completing any task that involves an important decision, architectural choice, or non-obvious implementation, or fixing important issues, write a file to `/.decisions/<short-slug>_YYYY-MM-DD.md` with:

- **What** was done
- **Why** this approach was chosen over alternatives
- **Trade-offs** or caveats to be aware of
- **Files changed**