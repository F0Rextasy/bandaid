---
name: bandaid
description: Forces root-cause fixes instead of symptom patches. Use when debugging a bug, a failing or flaky test, a CI run that went red, or any "just make it pass" request. Demands a reproduction before editing, isolates the real cause, and gates the diff with scripts/bandaid.py so swallowed errors, removed guards, disabled tests, and silenced checkers cannot land.
license: MIT
compatibility: Requires git and Python 3.8+. Runs in Claude Code, Codex, Cursor, and any Agent Skills compatible client.
metadata:
  author: F0Rextasy
  version: "1.0"
---

# bandaid

Stop patching symptoms. A fix that hides the error is not a fix; it is the bug wearing a costume.

## The one rule

You may not edit code until you can state, in one sentence:

> "<exact command or input> fails with <exact error>, because <location> does <wrong thing>."

If you cannot fill in all three blanks, you are still investigating. Do not write code.

## Protocol

### 1. Reproduce (mandatory, before any edit)

Run the failing path and capture the real error. Prefer the project's own test runner.

- Write a failing test that exercises the reported path, or run the exact command the user gave.
- Confirm it fails for the reason described, not a different reason.
- If it does not reproduce, stop and report that. A non-reproducing bug is not a license to guess.

### 2. Isolate the cause

Follow the error backwards until it stops being a symptom.

- Read the traceback to the deepest frame that is in this codebase, not the framework.
- Narrow: bisect the inputs, comment out branches, print the intermediate values.
- Name the one line where correct data becomes incorrect data.
- Form a single hypothesis and test it. Do not edit "just in case" several places at once.

State the cause as a location: `src/checkout.py:6` — not "a race condition somewhere".

### 3. Fix at the cause

Fix the line you named in step 2. The fix must make the wrong state impossible, not make the error invisible.

Hard bans (the scanner rejects these):

| Tempting move | Why it is a bandaid | Do this instead |
| --- | --- | --- |
| `except: pass`, `catch (e) {}` | The error stops existing for everyone downstream | Handle the specific error, or let it propagate |
| `raise` / `assert` downgraded to `return None` | The guard that caught the bug is gone | Keep the guard; fix what violated it |
| `except ValueError` widened to `except Exception` | The real error type can never reach you | Catch the type you actually handle |
| `test.skip(...)`, `xit(...)`, `@pytest.mark.skip` | The regression test is now off | Fix the test, or fix the code the test points at |
| `\|\| true`, `continue-on-error: true` | CI cannot go red anymore | Let the step fail |
| `# type: ignore`, `eslint-disable`, `as any` | The checker saw the bug and was told to be quiet | Fix the finding |
| `time.sleep(...)`, `retries=3` | Waits out the symptom instead of the ordering bug | Wait on the real condition, or fix the ordering |

Legitimate exceptions exist (a deprecated API you cannot change yet). If one applies, write the justification on the line itself:

```python
except LegacyTimeout:  # bandaid: allow -- upstream issue #42, remove after v3
    pass  # upstream SDK no-ops here until v3
```

A justification without a reason and a tracking reference is not a justification.

### 4. Verify

- The reproduction from step 1 now passes.
- Run the test suite (or the narrowest suite that covers the touched code).
- If you touched shared behavior, run the broader suite too.

### 5. Gate the diff (mandatory)

Scan your own diff before reporting success:

```bash
python scripts/bandaid.py            # working tree vs HEAD
python scripts/bandaid.py --staged   # staged changes
python scripts/bandaid.py --base main  # whole branch
```

Exit code 0 means clean. Exit code 1 means you produced bandaids: read each finding, go back to step 3, and fix the cause. Do not report success over a non-zero exit.

For CI or a hook, add `--strict` to fail on suspects too, or `--format json` for machine output.

See [references/RULES.md](references/RULES.md) for the full rule catalogue.

## Reporting back

After the gate passes, report in this order:

1. **Reproduction** — the command and the error it used to produce.
2. **Cause** — file:line and one sentence.
3. **Fix** — what changed at that location and why it makes the wrong state impossible.
4. **Evidence** — test results and the `bandaid` scan output (exit 0).

Never write "should be fixed now" without the four lines above.
