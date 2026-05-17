# Fastmail JMAP API

Reference for using the Fastmail JMAP API with a read-only API token.

## Authentication

Use a Bearer token in the Authorization header. Tokens are created in Fastmail under Settings > Privacy & Security > API tokens.

```
Authorization: Bearer <FASTMAIL_API_TOKEN>
```

## Session Discovery

Fetch the session object to discover account IDs, API URLs, and available capabilities:

```
GET https://api.fastmail.com/jmap/session
```

Key fields in the response:

| Field | Example | Purpose |
|-------|---------|---------|
| `apiUrl` | `https://api.fastmail.com/jmap/api/` | All method calls go here |
| `downloadUrl` | `https://www.fastmailusercontent.com/jmap/download/{accountId}/{blobId}/{name}?type={type}` | Download blobs (attachments, raw messages) |
| `uploadUrl` | `https://api.fastmail.com/jmap/upload/{accountId}/` | Upload blobs (write tokens only) |
| `eventSourceUrl` | `https://api.fastmail.com/jmap/event/` | Server-sent events for push notifications |
| `primaryAccounts` | `{"urn:ietf:params:jmap:mail": "ufa214098"}` | Account ID per capability |

## Available Capabilities

The session response lists which capabilities the token has access to. A read-only token exposes:

| Capability URI | Description |
|----------------|-------------|
| `urn:ietf:params:jmap:core` | Core JMAP protocol (request limits, collation) |
| `urn:ietf:params:jmap:mail` | Email, Mailbox, Thread, SearchSnippet |
| `urn:ietf:params:jmap:contacts` | AddressBook, ContactCard |
| `https://www.fastmail.com/dev/maskedemail` | Masked Email management (Fastmail extension) |

Not available via JMAP: calendars (use CalDAV), files (use WebDAV), sieve/filtering rules (web UI only), identities/submission (requires write token).

## Making API Calls

All method calls are POST requests to the `apiUrl` with a JSON body:

```json
{
  "using": [
    "urn:ietf:params:jmap:core",
    "urn:ietf:params:jmap:mail"
  ],
  "methodCalls": [
    ["Method/name", { "accountId": "...", ...args }, "callId"]
  ]
}
```

- `using`: array of capability URIs needed for the methods in this request
- `methodCalls`: array of `[methodName, arguments, clientId]` triples
- Multiple method calls can be batched in one request (up to `maxCallsInRequest`, default 50)
- Back-references let later calls use results from earlier ones via `#property` and `resultOf`

## Mail

### Mailbox/get

List all mailboxes (folders). Returns folder hierarchy, message counts, and permissions.

```json
["Mailbox/get", {"accountId": "...", "ids": null}, "0"]
```

Key properties per mailbox: `id`, `name`, `parentId`, `role` (inbox, sent, trash, drafts, junk, archive, etc.), `totalEmails`, `unreadEmails`, `totalThreads`, `unreadThreads`, `sortOrder`, `myRights`.

### Email/query

Search and filter emails. Returns an ordered list of email IDs.

```json
["Email/query", {
  "accountId": "...",
  "filter": {"inMailbox": "P-F"},
  "sort": [{"property": "receivedAt", "isAscending": false}],
  "limit": 10
}, "0"]
```

Filter operators: `inMailbox`, `inMailboxOtherThan`, `from`, `to`, `subject`, `body`, `after`, `before`, `hasKeyword`, `notKeyword`, `minSize`, `maxSize`, `hasAttachment`, `text` (full-text). Combine with `operator: "AND"/"OR"/"NOT"` and nested `conditions`.

Sort properties: `receivedAt`, `from`, `to`, `subject`, `size`, `header.x-spam-score`.

### Email/get

Fetch full email details by ID. Use back-references from Email/query to avoid two round-trips.

```json
["Email/get", {
  "accountId": "...",
  "#ids": {"resultOf": "0", "name": "Email/query", "path": "/ids"},
  "properties": ["id", "subject", "from", "to", "receivedAt", "size", "keywords", "bodyValues", "textBody", "htmlBody"]
}, "1"]
```

Key properties: `id`, `blobId`, `threadId`, `mailboxIds`, `from`, `to`, `cc`, `bcc`, `subject`, `date`, `receivedAt`, `size`, `preview`, `keywords` (flags like `$seen`, `$flagged`, `$hasattachment`), `bodyStructure`, `bodyValues`, `textBody`, `htmlBody`, `attachments`.

To get body content, include `bodyProperties`, `fetchTextBodyValues: true`, and/or `fetchHTMLBodyValues: true`.

### Thread/get

Fetch conversation threads by ID. Returns the ordered list of email IDs in each thread.

```json
["Thread/get", {"accountId": "...", "ids": ["..."]}, "0"]
```

### Back-reference Pattern

Efficiently query + fetch in a single request:

```json
{
  "using": ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"],
  "methodCalls": [
    ["Email/query", {
      "accountId": "ufa214098",
      "filter": {"inMailbox": "P-F"},
      "sort": [{"property": "receivedAt", "isAscending": false}],
      "limit": 5
    }, "query"],
    ["Email/get", {
      "accountId": "ufa214098",
      "#ids": {"resultOf": "query", "name": "Email/query", "path": "/ids"},
      "properties": ["id", "subject", "from", "receivedAt"]
    }, "fetch"]
  ]
}
```

## Contacts

### AddressBook/get

List address books.

```json
{
  "using": ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:contacts"],
  "methodCalls": [
    ["AddressBook/get", {"accountId": "...", "ids": null}, "0"]
  ]
}
```

Properties: `id`, `name`, `isDefault`, `isSubscribed`, `sortOrder`, `myRights`, `shareWith`.

### ContactCard/get

Fetch contacts. Uses the JSContact Card format (`@type: "Card"`).

```json
["ContactCard/get", {"accountId": "...", "ids": null}, "0"]
```

Properties: `id`, `uid`, `kind` (individual, org), `name` (with `components` for given/surname), `emails`, `phones`, `addresses`, `organizations`, `titles`, `nicknames`, `notes`, `updated`, `addressBookIds`.

## Masked Email

Fastmail extension for managing masked (alias) email addresses. Requires `https://www.fastmail.com/dev/maskedemail` in `using`.

### MaskedEmail/get

```json
{
  "using": ["urn:ietf:params:jmap:core", "https://www.fastmail.com/dev/maskedemail"],
  "methodCalls": [
    ["MaskedEmail/get", {"accountId": "...", "ids": null}, "0"]
  ]
}
```

Properties per masked email: `id`, `email` (the alias address), `forDomain`, `description`, `state` (pending, enabled, disabled, deleted), `createdAt`, `lastMessageAt`, `createdBy`, `url`.

## Limits

From the session `capabilities["urn:ietf:params:jmap:core"]`:

| Limit | Value |
|-------|-------|
| `maxCallsInRequest` | 50 |
| `maxObjectsInGet` | 4096 |
| `maxObjectsInSet` | 4096 |
| `maxConcurrentRequests` | 10 |
| `maxConcurrentUpload` | 10 |
| `maxSizeRequest` | 10 MB |
| `maxSizeUpload` | 250 MB |
| `maxSizeAttachmentsPerEmail` | 50 MB |

## Not Available

- **Sieve / mail filtering rules**: `urn:ietf:params:jmap:sieve` is rejected as unknown. ManageSieve protocol (port 4190) is not exposed. Rules can only be managed via the web UI (Settings > Filters & Rules) and exported/imported as JSON.
- **Calendars**: Not available via JMAP. Use CalDAV instead.
- **Files**: Use WebDAV.
- **Email submission / Identities**: Requires a write-capable token with `urn:ietf:params:jmap:submission`.
- **Vacation responses**: Requires write access.
