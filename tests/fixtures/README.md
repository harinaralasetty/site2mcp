# HAR fixtures

`chrome.har` and `firefox.har` are hand-authored, synthetic browser-shaped HAR 1.2
files, not real captures of a third-party service. Their creator versions label
representative formats and do not claim a browser was run.

Field names were checked against the [HAR 1.2 specification](https://raw.githubusercontent.com/ahmadnassri/har-spec/master/versions/1.2.md).
Both use `log.version`, `creator`, `entries`, `startedDateTime`, `time`, `request`,
`response`, `cache`, and `timings`; request objects include headers, cookies,
queryString, HTTP version and byte sizes. Response objects include content,
headers, cookies, status, redirectURL and sizes. The Chrome-shaped fixture adds
pages, pageref, `_initiator`, and `_resourceType`. The Firefox-shaped fixture uses
`_cause` and base64 response content. Underscore extensions are not HAR core fields.

They intentionally contain recognizable fake cookie, bearer, API-key, CSRF,
nested-body credentials and personal values. The artifact test checks that these
values never appear anywhere in a fresh generated output directory. The separate
local recorder writes a fresh HAR from actual loopback HTTP requests on each demo
run. No browser automation or proxy is involved.
