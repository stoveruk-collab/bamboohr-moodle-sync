# BambooHR -> Moodle Sync (AWS ECS Fargate)

This repository provides a starter, all-AWS deployment for syncing BambooHR changes into Moodle using a scheduled ECS Fargate task. State is tracked in DynamoDB (watermark + offset), secrets are stored in Secrets Manager, logs go to CloudWatch Logs, and each task stop event triggers an SNS notification.

## Architecture
- **EventBridge Scheduler** runs the ECS task on a schedule.
- **ECS Fargate** executes `app/sync.py`.
- **DynamoDB** stores `{since, offset}` for catch-up processing.
- **Secrets Manager** stores BambooHR + Moodle API credentials.
- **CloudWatch Logs** stores task logs.
- **SNS** receives notifications when the ECS task stops (exit code + reason).

## Repo Layout
- `infra/template.yaml` CloudFormation stack
- `infra/params.dev.json` Sample parameters
- `app/sync.py` Sync worker (catch-up capable)
- `app/requirements.txt` Python deps
- `app/Dockerfile` Container image

## Behavior (Catch-Up)
The worker reads `{since, offset}` from DynamoDB and requests BambooHR changes since `since`. It processes only `BATCH_SIZE` records starting at `offset`, then updates the offset. Only when the final batch is processed does it advance `since` to BambooHR's latest timestamp and reset `offset` to 0.

## Secrets Format
Store JSON in Secrets Manager.

**BambooHR** (`bamboohr-moodle-sync/bamboo`):
```json
{"api_key":"YOUR_BAMBOOHR_API_KEY"}
```

**Moodle** (`bamboohr-moodle-sync/moodle`):
```json
{"token":"YOUR_MOODLE_TOKEN"}
```

## Deploy
1. Build and push the Docker image to the ECR repo created by CloudFormation.
2. Deploy the CloudFormation stack with `infra/template.yaml` and `infra/params.dev.json`.
3. Update the Secrets Manager values with real credentials.

## Notes
- `sync.py` uses a placeholder BambooHR change endpoint and a placeholder Moodle write. Update these in `app/sync.py` for your org's API requirements.
- BambooHR changed-since formats vary; adjust the `since` format if your API expects UNIX timestamps.
