# Plan: split `tools.py` into separate tool modules

## Goal

Refactor the current single-file `tools.py` module into a `tools/` package where each tool lives in its own source file, while preserving the existing auto-registration behavior based on `@tool` side effects.

## Why

- Keep each tool easier to read and maintain.
- Isolate domain-specific logic by API/domain.
- Make future additions less likely to create a large grab-bag module.
- Improve testability of individual tools and helper functions.

## Constraints from current architecture

- `registry.py` owns the global `TOOLS` registry and `@tool` decorator.
- Tools register themselves when their module is imported.
- `bot.py` currently does `import tools` in `main()` specifically to trigger registration.
- Any refactor must keep `import tools` sufficient to load all tools.

## Proposed target structure

```text
registry.py
bot.py
tools/
  __init__.py
  common.py
  dice.py
  weather.py
  trains.py
```

Optional later additions:

```text
tools/
  fastmail.py
  geocoding.py
  weather_helpers.py
```

## File responsibilities

### `tools/__init__.py`

- Import all tool modules for registration side effects.
- Keep `import tools` working unchanged in `bot.py`.
- Export nothing or a minimal public surface.

Example shape:

```python
from . import dice  # noqa: F401
from . import weather  # noqa: F401
from . import trains  # noqa: F401
```

### `tools/common.py`

Move shared infrastructure here:

- shared `httpx.Client`
- shared API base URLs/constants
- common helper utilities used across multiple tools

Candidate items from current `tools.py`:

- `_HTTP`
- `_NOMINATIM`
- `_METNO`
- `_DIGITRAFFIC`
- `_FASTMAIL_SESSION_URL` if still needed
- `_FM_SESSION` and `_FM_MAILBOXES` only if a Fastmail tool is actually added soon

### `tools/dice.py`

Contains:

- `roll_die()`

### `tools/weather.py`

Contains:

- `_geocode()`
- `_normalise_condition()`
- `_day_summary()`
- `weather()`

Imports shared HTTP client/constants from `tools.common`.

### `tools/trains.py`

Contains:

- `_find_station_code()`
- `train_departures()`

Imports shared HTTP client/constants from `tools.common`.

## Migration steps

1. Create `tools/` package.
2. Add `tools/__init__.py` that imports each tool submodule.
3. Create `tools/common.py` and move shared client/constants there.
4. Move `roll_die()` to `tools/dice.py`.
5. Move weather-specific helpers and `weather()` to `tools/weather.py`.
6. Move train-specific helpers and `train_departures()` to `tools/trains.py`.
7. Delete old `tools.py`.
8. Keep `bot.py` unchanged if `import tools` still resolves to the package.
9. Run:
   - `task build`
   - `task lint`
   - `task test`

## Import/registration risks

### Risk: tools stop registering

Cause:
- A new module exists but is not imported from `tools/__init__.py`.

Mitigation:
- Treat `tools/__init__.py` as the authoritative import list.
- Add or update a test that asserts expected tool names exist in `TOOLS` after importing `tools`.

### Risk: circular imports

Cause:
- Tool modules import each other directly.

Mitigation:
- Keep shared helpers in `tools.common`.
- Avoid cross-importing tool modules.

### Risk: package/module name transition issues

Cause:
- Replacing `tools.py` with `tools/` can be confusing during the transition.

Mitigation:
- Do the refactor in one change.
- Ensure only one import target named `tools` exists in the final tree.

## Suggested test coverage

Add or update tests for:

1. **Registration smoke test**
   - Import `tools`
   - Assert `roll_die`, `weather`, and `train_departures` are present in `TOOLS`

2. **CLI command behavior**
   - Existing `/train` and `/weather` command tests should continue to pass unchanged

3. **Optional import test**
   - Verify `bot.main()` import path still works with the package

## Future-friendly extension pattern

For every new tool:

1. Create `tools/<name>.py`
2. Decorate the callable with `@tool(...)`
3. Import the module from `tools/__init__.py`
4. Add tests for registration and behavior

## Recommendation

This refactor is reasonable once tool count grows or more domains are added. If done now, keep it minimal:

- one package
- one module per tool/domain
- one small `common.py`
- no dynamic import system

A static `__init__.py` import list is the simplest option and matches the current architecture well.
