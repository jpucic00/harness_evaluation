# Questions

Answer in `answers.json`. Formats: *name* = a lowercase string, *number* = a JSON
integer, *numbers* = a JSON list of integers, *date* = `"YYYY-MM-DD"`,
*letter* = one of `"a"`, `"b"`, `"c"`, `"d"`.

| Key | Question | Format |
| --- | --- | --- |
| q1 | Which message broker should a new service use today? | name |
| q2 | Which ADR did ADR 0005 supersede? | number |
| q3 | Which ADRs are currently accepted (in force)? | numbers |
| q4 | Which ADR is still only proposed? | number |
| q5 | How many days are events retained on the broker? | number |
| q6 | Which field is used as the partition key for order events? | name |
| q7 | Why was the document database rejected? a) licence cost, b) the team has no experience running it and orders and catalog changes must commit in one transaction, c) it was slower in benchmarks, d) the cloud provider does not offer it | letter |
| q8 | How long (in minutes) is an access token valid? | number |
| q9 | Which database holds the product catalog? | name |
| q10 | On what date was the current messaging decision accepted? | date |
| q11 | Which ADR must you read to choose a message broker? | number |
| q12 | How many ADRs were rejected? | number |
