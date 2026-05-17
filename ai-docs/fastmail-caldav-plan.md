# Plan: add read-only Fastmail CalDAV calendar support

## Goal

Add a new read-only calendar tool that lets the agent answer questions like:

- "what do I have on my calendar tomorrow"
- "What's on my agenda next week?"
- "when does school end today?"

The implementation should use **Fastmail CalDAV**, not JMAP.

Server details provided:

- Server: `https://caldav.fastmail.com/`
- TLS: required
- Username: Fastmail email address
- Password: Fastmail app-specific password

## Why CalDAV

- Fastmail JMAP does **not** expose calendars.
- The user already has an iCloud calendar subscribed inside Fastmail.
- CalDAV is the correct protocol for reading calendar events from Fastmail.
- A read-only tool is enough for agenda queries and is safer than adding write support.

## Scope

### In scope

- Read calendars and events from Fastmail via CalDAV
- Focus on time-window queries:
  - today
  - tomorrow
  - next week
  - arbitrary start/end range if needed
- Return enough event detail to answer:
  - agenda summaries
  - event start/end times
  - "when does X end today?"
- Optional calendar filtering by calendar name
- Read-only implementation

### Out of scope

- Creating, editing, deleting, or moving events
- Invitations / RSVP actions
- Recurring-event authoring
- Push sync / subscriptions / background daemons
- Free/busy writeback
- Full natural-language date parsing inside the tool itself

## Constraints from current architecture

- Tools are defined in `tools.py` and auto-register with `@tool(...)` from `registry.py`.
- `bot.py` can optionally expose slash commands for direct CLI use.
- Existing network tools use a shared module-level `httpx.Client` pattern.
- New support should fit the existing tool-calling flow and be callable by the model.

## Proposed user-facing capability

Add one primary tool:

### `list_calendar_events`

Purpose:
- Return events in a specified time range, optionally filtered by calendar name and text query.

Suggested parameters:

```json
{
  "type": "object",
  "properties": {
    "start": {
      "type": "string",
      "description": "Inclusive start timestamp in ISO 8601 format, preferably with timezone"
    },
    "end": {
      "type": "string",
      "description": "Exclusive end timestamp in ISO 8601 format, preferably with timezone"
    },
    "calendar": {
      "type": "string",
      "description": "Optional calendar name to restrict results"
    },
    "query": {
      "type": "string",
      "description": "Optional case-insensitive text filter against summary, location, and description"
    },
    "limit": {
      "type": "integer",
      "description": "Maximum number of events to return, default 50, max 200"
    }
  },
  "required": ["start", "end"]
}
```

This single primitive is sufficient for the example queries:

- "what do I have on my calendar tomorrow"
  - model computes tomorrow window and calls the tool
- "What's on my agenda next week?"
  - model computes next-week window and calls the tool
- "when does school end today?"
  - model calls the tool for today, optionally with `query: "school"`

## Optional second tool

If the first tool proves too generic for prompting, add:

### `calendar_agenda`

Parameters:
- `period`: `today | tomorrow | next_week`
- `calendar` optional
- `query` optional

This would be a convenience wrapper over `list_calendar_events`, not a separate data source.

Recommendation: start with **only `list_calendar_events`**.

## Credentials and configuration

Use environment variables, matching the pattern used by the Fastmail mail tool.

Suggested variables:

- `FASTMAIL_CALDAV_URL=https://caldav.fastmail.com/`
- `FASTMAIL_CALDAV_USERNAME=<user@example.com>`
- `FASTMAIL_CALDAV_PASSWORD=<app-specific-password>`

Notes:
- Do not hardcode credentials.
- Reuse the same Fastmail account naming convention as the mail feature.
- If desired later, the username can default to a future general `FASTMAIL_USERNAME` variable.

## Implementation options

### Option A: use a CalDAV Python library

Candidate libraries:
- `caldav`
- `icalendar` as a companion for parsing event payloads if needed

Pros:
- Faster to implement
- Handles some CalDAV details for us
- Less XML plumbing

Cons:
- Adds dependency surface
- Need to verify library behavior against Fastmail

### Option B: implement against CalDAV/WebDAV directly with `httpx`

Required pieces:
- WebDAV `PROPFIND` to discover principal/calendars
- CalDAV `REPORT` with time-range filters
- XML parsing of responses
- iCalendar parsing from event bodies

Pros:
- Full control
- Consistent with existing `httpx` approach
- Fewer heavy dependencies if implemented carefully

Cons:
- More protocol work
- More edge cases to handle manually

## Recommendation

Use **Option A** if the dependency is reliable and small enough. Otherwise use **Option B** with direct `httpx` + XML + iCalendar parsing.

Either way, keep the rest of the tool interface and returned data shape the same.

## Data flow

### 1. Authenticate

Connect to `https://caldav.fastmail.com/` with basic auth using:
- Fastmail email address
- Fastmail app-specific password

### 2. Discover calendars

Obtain the list of calendars visible to the account.

We need to verify whether the subscribed iCloud calendar appears like a normal readable calendar through Fastmail CalDAV. If it does not, the plan must stop here and fall back to direct iCloud CalDAV or ICS.

### 3. Query events in a time range

For a requested `[start, end)` window:
- fetch matching events from all calendars or one named calendar
- expand recurring events when possible via server-side calendar query behavior or library support
- normalize timezone-aware timestamps

### 4. Normalize output

Return a model-friendly JSON shape.

Suggested return shape:

```json
{
  "source": {
    "name": "Fastmail CalDAV",
    "realtime": true,
    "note": "Calendar events read from Fastmail via CalDAV"
  },
  "range": {
    "start": "2026-04-08T00:00:00+03:00",
    "end": "2026-04-09T00:00:00+03:00",
    "timezone": "Europe/Helsinki"
  },
  "calendar_filter": "School",
  "query": "school",
  "events": [
    {
      "calendar": "School",
      "uid": "...",
      "title": "Math",
      "start": "2026-04-08T08:30:00+03:00",
      "end": "2026-04-08T14:00:00+03:00",
      "all_day": false,
      "location": "Room 201",
      "description": "Optional notes"
    }
  ]
}
```

## Event fields to extract

Minimum useful fields:

- calendar name
- UID
- title / summary
- start
- end
- all-day flag
- location
- description / notes

Nice to have later:

- organizer
- attendees
- recurrence marker
- status / cancelled
- URL

## Time handling requirements

This feature is mainly about time-based questions, so time handling must be explicit.

Requirements:

- Preserve timezone-aware datetimes end-to-end
- Normalize all-day events sensibly
- Sort events by start time ascending
- Include both start and end timestamps in responses
- Treat query window as `[start, end)`

Examples:

- **today**: local midnight to next local midnight
- **tomorrow**: next local midnight to following local midnight
- **next week**: next Monday 00:00 to Monday after that 00:00, or a clearly documented 7-day rolling window

Recommendation:
- In the tool, accept only concrete ISO timestamps.
- Let the model or CLI command resolve natural-language periods into exact timestamps.

## Query behavior for example prompts

### "what do I have on my calendar tomorrow"

Expected tool call:
- `start=<tomorrow local midnight>`
- `end=<day after tomorrow local midnight>`

The assistant summarizes returned events in natural language.

### "What's on my agenda next week?"

Expected tool call:
- `start=<next week start>`
- `end=<next week end>`

The assistant groups by day if helpful.

### "when does school end today?"

Expected tool call:
- `start=<today local midnight>`
- `end=<tomorrow local midnight>`
- optional `query="school"`

Assistant logic:
- find matching event(s)
- answer with the latest or most relevant end time
- if ambiguous, say so and list candidates

## Proposed file changes

### `tools.py`

Add:
- Fastmail CalDAV constants and helpers
- the new `list_calendar_events()` tool
- small helper functions for:
  - env var loading
  - CalDAV client/session creation
  - calendar discovery
  - event normalization

If the implementation grows too large, later refactor into a separate module as described in `ai-docs/tools-package-split-plan.md`.

### `bot.py`

Optional but recommended: add a `/calendar` command.

Examples:

- `/calendar today`
- `/calendar tomorrow`
- `/calendar next-week`
- `/calendar --from 2026-04-08T00:00:00+03:00 --to 2026-04-09T00:00:00+03:00`
- `/calendar --calendar School tomorrow`

This is convenience only; the model can still call the tool directly.

### `tests/`

Add focused tests for:
- registration
- event normalization
- time-range filtering behavior
- formatter behavior if `/calendar` is added

## Testing strategy

### Unit tests

Mock the CalDAV layer and test:
- credential validation
- calendar name filtering
- text query filtering
- event sorting
- all-day event normalization
- empty results
- error messages

### Integration tests

If practical, gated by env vars:
- run only when Fastmail CalDAV credentials are present
- verify that calendars can be listed
- verify that a known date range returns parseable events

### Manual verification checklist

1. Credentials work against `https://caldav.fastmail.com/`
2. The subscribed iCloud calendar is visible
3. Today query returns events in local time
4. Tomorrow query returns expected events
5. Next-week query spans the correct interval
6. Query filter like `school` narrows results
7. All-day events are displayed sensibly

## Error handling

Tool should return clear user-facing errors for:

- missing env vars
- authentication failure
- no calendars visible
- named calendar not found
- invalid datetime format
- server timeout / temporary network failure
- subscribed calendar not exposed through Fastmail CalDAV

Example error messages:
- `FASTMAIL_CALDAV_USERNAME environment variable not set`
- `calendar 'School' not found`
- `Fastmail CalDAV authentication failed`
- `No calendars are available via Fastmail CalDAV`

## Security notes

- Use an app-specific password only
- Never log credentials
- Avoid returning raw event bodies if unnecessary
- Keep the tool read-only by design
- Do not add write methods or mutation code paths

## Main technical risk

### Risk: subscribed iCloud calendar is not readable through Fastmail CalDAV

This is the most important unknown.

Mitigation:
1. First build a tiny spike that authenticates and lists calendars
2. Confirm the subscribed iCloud calendar appears
3. Confirm events can be fetched from it in a date range
4. Only then implement the full tool and CLI surface

If this fails, fallback options are:
- direct iCloud CalDAV
- ICS feed access if available

## Implementation sequence

1. **Spike: connectivity and discovery**
   - authenticate to Fastmail CalDAV
   - list calendars
   - confirm the subscribed iCloud calendar is visible

2. **Spike: event fetch**
   - fetch events for a small date range
   - verify recurring and all-day events are readable enough

3. **Tool implementation**
   - add `list_calendar_events()` in `tools.py`
   - normalize output
   - add text filtering and calendar filtering

4. **Bot integration**
   - optionally add `/calendar`
   - add output formatting for human-readable agenda views

5. **Tests and docs**
   - add unit tests
   - add setup notes to README later if the feature lands

6. **Validation**
   - run:
     - `task build`
     - `task lint`
     - `task test`

## Suggested minimal v1

The smallest useful version is:

- one tool: `list_calendar_events(start, end, calendar=None, query=None, limit=50)`
- Fastmail CalDAV auth via env vars
- read events from visible calendars
- return normalized JSON with title/start/end/location/description/all_day
- no slash command initially unless desired

That is enough for the target questions and keeps the scope controlled.

## Recommendation

Proceed in two phases:

### Phase 1: prove Fastmail exposes the subscribed iCloud calendar via CalDAV

This is the critical assumption.

### Phase 2: build one generic read-only event-range tool

Do not start with multiple calendar tools. One good range-based tool is enough for:
- tomorrow agenda
- next week agenda
- "when does X end today?"

If Phase 1 succeeds, this feature is a good fit for Metalclaw’s existing tool architecture.
