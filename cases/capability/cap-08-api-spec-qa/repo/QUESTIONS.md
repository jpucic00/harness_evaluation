# Questions about the Inventory API

Answer in `answers.json`. Formats: *number* = a JSON integer, *string* = a JSON
string, *strings* = a JSON list of strings, *endpoint* = `"METHOD /path"` exactly as
the path appears in the spec, e.g. `"GET /items"`.

| Key | Question | Format |
| --- | --- | --- |
| q1 | Which status code does creating a reservation return on success? | number |
| q2 | Which status code do I get when I try to reserve more units than are available? | number |
| q3 | Which fields are required in the body of a new reservation? | strings |
| q4 | What is the largest page size I can ask for when listing items? | number |
| q5 | Which query parameters does listing items accept? | strings |
| q6 | Which OAuth scope do I need to cancel a reservation? | string |
| q7 | Which header must every reservation-creating request carry? | string |
| q8 | Which status code comes back when I reuse that header's value with a different body? | number |
| q9 | If I don't set an expiry, after how many minutes does a reservation expire? | number |
| q10 | Which values can a reservation's status have? | strings |
| q11 | Which endpoint is deprecated? | endpoint |
| q12 | How many requests per minute may one client make? | number |
| q13 | What is the base URL of the production server? | string |
