# Schedule Contract

Timezone: `Asia/Seoul`

| Job ID | Time | Days | Output |
| --- | --- | --- | --- |
| `morning-brief` | 08:30 | Monday-Friday | five candidates, evidence, warnings, additional checks |
| `evening-review` | 20:00 | Monday-Friday | candidate follow-up, holdings scenarios, alert delivery status |
| `weekly-review` | 12:00 | Saturday | point-in-time performance, errors, proposed rule changes |

The scheduler may create a run only once for each job and scheduled date. Analysis generation and Telegram delivery are separate states. A missed job is reported; it is not silently marked successful.

The weekly review evaluates every persisted candidate report at 5, 20, and 60 trading sessions using the next session open and configured round-trip cost. It groups only completed observations by `ruleset_version`, decision, and horizon; pending observations are never imputed.
