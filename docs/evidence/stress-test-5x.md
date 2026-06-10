# Stress Test Report

- **Started**: 2026-06-08 03:47:13
- **Finished**: 2026-06-08 03:47:34
- **Mode**: tts
- **Total calls**: 5
- **Concurrency**: 3
- **Server**: http://127.0.0.1:8000

## Summary

| Metric | Value |
| --- | --- |
| Total calls | 5 |
| Success | 0 |
| Failed | 5 |
| Success rate | 0.0% |
| T first byte p50 | n/a |
| T first byte p95 | n/a |
| T call end p50 | n/a |
| T call end p95 | n/a |
| T call end max | n/a |
| **SLA (≤25 s) hits** | **5 / 5** |

## By Scenario

| Scenario | Success / Total |
| --- | --- |
| happy | 0/5 |

## Per-call Detail

| Call ID | Scenario | OK | T1st | Tend | Agent frames | Agent bytes |
| --- | --- | --- | --- | --- | --- | --- |
| `CA842516385125125669` | happy | False | 2.24s | 10.23s | 19 | 115200 |
| `CA901967170438539597` | happy | False | 2.04s | 10.23s | 17 | 104320 |
| `CA650944868055760424` | happy | False | 1.95s | 10.23s | 18 | 105600 |
| `CA891589248774839918` | happy | False | 1.73s | 10.24s | 17 | 100480 |
| `CA497495046959842802` | happy | False | 2.00s | 10.24s | 17 | 102400 |