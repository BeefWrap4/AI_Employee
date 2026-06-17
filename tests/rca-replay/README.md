# rca-replay

Historical or simulated alarm replay fixtures and RCA evaluation test runners.

## JSONL Format

Each line is one replay case:

```json
{"case_id":"case_001","expected_root_cause_type":"transmission_link_degradation","alarms":[{"alarm_id":"a_001","alarm_code":"LINK_DEGRADE","alarm_name":"Transmission link degradation","vendor":"huawei","site_id":"SITE-001","cell_id":"CELL-001","ne_id":"NE-001","severity":"critical","start_time":"2026-06-17T10:00:00+08:00","raw_payload":{}}]}
```

Run:

```powershell
conda activate ai-employee
python -m ai_employee.rca_agent.replay tests/rca-replay/sample_cases.jsonl --json
```
