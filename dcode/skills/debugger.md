---
name: debugger
description: Systematically finding the root cause of bugs, crashes and failing tests.
---
# Debugger

- Reproduce first: get the exact error, command and inputs. No reproduction, no fix.
- Read the full stack trace; start at the deepest frame in project code.
- Form one hypothesis at a time and test it cheaply (logging, a small script, a focused test).
- Check recent changes (`git log`, `git diff`) — regressions usually live there.
- Fix the root cause, not the symptom. Add a test that would have caught it.
- Explain what was wrong, why, and how the fix addresses it.
