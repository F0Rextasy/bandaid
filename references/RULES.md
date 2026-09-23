# Rule catalogue

`scripts/bandaid.py` scans only the **added** lines of a diff. Every rule answers one question: *does this change make an error stop being visible, or does it make the error stop happening?*

Two severities:

- **bandaid** — the error is being hidden. Fails the scan (exit 1).
- **suspect** — smells like a shortcut; may be legitimate. Passes by default, fails with `--strict`.

## bandaid

| Rule | Catches | Example that fails |
| --- | --- | --- |
| `bare-except` | catches every error including the one you were fixing | `except:` |
| `except-pass` | exception swallowed, failure vanishes | `except Exception:` then `pass` |
| `empty-catch` | error discarded before anyone sees it | `catch (e) {}` |
| `catch-noop` | rejected promise dropped | `.catch(() => null)` |
| `go-err-drop` | error checked, then thrown away | `if err != nil { return nil }` |
| `guard-removed` | a `raise`/`assert`/`throw` in the same hunk replaced by a quiet exit | `- raise ValueError(...)` / `+ return None` |
| `catch-broadened` | the real error type can no longer reach the handler | `- except ValueError:` / `+ except Exception:` |
| `test-off` | the regression test was switched off | `test.skip(...)`, `xit(...)`, `@pytest.mark.skip` |
| `failure-hidden` | CI can no longer go red | `\|\| true`, `continue-on-error: true`, `set +e` |

## suspect

| Rule | Catches | Example |
| --- | --- | --- |
| `except-exit` | leaves the except block without saying it failed | `except E:` then `return` |
| `checker-silenced` | linter/type checker told to be quiet | `# type: ignore`, `eslint-disable` |
| `ts-any` | type escape hatch | `value as any` |
| `suppress` | policy-level suppression at the call site | `contextlib.suppress(...)` |
| `sleep-retry` | waits out the symptom instead of the ordering bug | `time.sleep(1)`, `retries=3` |

## Two rule families worth knowing

**Adjacency rules** (`except-pass`, `except-exit`, `go-err-drop`) need the head line and the following line to have consecutive line numbers, so they only fire when the swallow was written as one unit.

**Pairing rules** (`guard-removed`, `catch-broadened`) compare removed lines against added lines *inside the same hunk*. This is what catches a fix that quietly deletes the guard that surfaced the bug — no single added line looks wrong on its own.

## The escape hatch

An intentional shortcut must justify itself where it happens:

```python
except LegacyTimeout:  # bandaid: allow -- upstream issue #42, remove after v3
    pass  # upstream SDK no-ops here until v3
```

The comment is stripped before matching (so the rule still sees what you wrote) and the finding is counted as `suppressed`, never silently dropped. `bandaid: allow` with no reason attached still counts against you in the report.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | clean (or suspects only, without `--strict`) |
| `1` | bandaids found — do not merge, do not report success |
| `2` | usage error — git missing, no commits yet, unreadable patch |
