# BambooHR -> NovaLXP Sync Runbook

This runbook is for operators. It covers routine operations, manual runs, monitoring, and troubleshooting.

## Operator Quick Start (5 Commands)
Run these in order for a one-off health check:

```bash
# 1) Session context
export AWS_PROFILE=personal AWS_REGION=eu-west-2 AWS_PAGER="" STACK_NAME=bamboohr-novalxp-sync2 CLUSTER_ARN=arn:aws:ecs:eu-west-2:992057086831:cluster/bamboohr-novalxp-sync2-cluster SUBNET_1=subnet-0278bbe3ba8805a19 SUBNET_2=subnet-09e51d5e50868079e SG_ID=sg-0cdf2a4653c451259

# 2) Start one task
TASK_DEF=$(aws ecs list-task-definitions --region "$AWS_REGION" --family-prefix "${STACK_NAME}-task" --sort DESC --max-items 1 --query "taskDefinitionArns[0]" --output text); TASK_ARN=$(aws ecs run-task --region "$AWS_REGION" --cluster "$CLUSTER_ARN" --launch-type FARGATE --task-definition "$TASK_DEF" --network-configuration "awsvpcConfiguration={subnets=[$SUBNET_1,$SUBNET_2],securityGroups=[$SG_ID],assignPublicIp=ENABLED}" --query "tasks[0].taskArn" --output text); echo "$TASK_ARN"

# 3) Wait for stop
aws ecs wait tasks-stopped --region "$AWS_REGION" --cluster "$CLUSTER_ARN" --tasks "$TASK_ARN"

# 4) Confirm task result
aws ecs describe-tasks --region "$AWS_REGION" --cluster "$CLUSTER_ARN" --tasks "$TASK_ARN" --query "tasks[0].{ExitCode:containers[0].exitCode,LastStatus:lastStatus,StoppedReason:stoppedReason}" --output table

# 5) Check latest app summary logs
aws logs tail "/ecs/${STACK_NAME}" --since 30m --format short
```

## 1) Standard Operator Context
Set these once per terminal session:

```bash
export AWS_PROFILE=personal
export AWS_REGION=eu-west-2
export AWS_PAGER=""

export STACK_NAME=bamboohr-novalxp-sync2
export CLUSTER_ARN=arn:aws:ecs:eu-west-2:992057086831:cluster/bamboohr-novalxp-sync2-cluster
export SUBNET_1=subnet-0278bbe3ba8805a19
export SUBNET_2=subnet-09e51d5e50868079e
export SG_ID=sg-0cdf2a4653c451259
```

## 2) Daily Expected State
- Schedule: 03:00 London time daily.
- Batch mode: unlimited catch-up (`BatchSize=0`).
- Notification email:
  - success: `<ProjectName>: BambooHR->NovaLXP sync SUCCEEDED.`
  - failure: `<ProjectName>: BambooHR->NovaLXP sync FAILED.`

## 3) Manual Run (One-Off)
### 3.1 Start task
```bash
TASK_DEF=$(aws ecs list-task-definitions \
  --region "$AWS_REGION" \
  --family-prefix "${STACK_NAME}-task" \
  --sort DESC \
  --max-items 1 \
  --query "taskDefinitionArns[0]" \
  --output text)

TASK_ARN=$(aws ecs run-task \
  --region "$AWS_REGION" \
  --cluster "$CLUSTER_ARN" \
  --launch-type FARGATE \
  --task-definition "$TASK_DEF" \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNET_1,$SUBNET_2],securityGroups=[$SG_ID],assignPublicIp=ENABLED}" \
  --query "tasks[0].taskArn" \
  --output text)

echo "$TASK_ARN"
```

### 3.2 Wait for completion
```bash
aws ecs wait tasks-stopped \
  --region "$AWS_REGION" \
  --cluster "$CLUSTER_ARN" \
  --tasks "$TASK_ARN"
```

### 3.3 Check exit status
```bash
aws ecs describe-tasks \
  --region "$AWS_REGION" \
  --cluster "$CLUSTER_ARN" \
  --tasks "$TASK_ARN" \
  --query "tasks[0].{ExitCode:containers[0].exitCode,LastStatus:lastStatus,StoppedReason:stoppedReason}" \
  --output table
```

## 4) Monitoring and Validation
### 4.1 Tail app logs
```bash
aws logs tail "/ecs/${STACK_NAME}" --since 2h --format short
```

Look for the JSON summary line. Important fields:
- `errors`
- `processed`
- `created`
- `updated`
- `suspended`
- `since`, `next_since`
- `offset`, `next_offset`
- `total_changed`

### 4.2 Check state row
```bash
TABLE_NAME=$(aws cloudformation describe-stacks \
  --region "$AWS_REGION" \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='StateTableName'].OutputValue" \
  --output text)

aws dynamodb get-item \
  --region "$AWS_REGION" \
  --table-name "$TABLE_NAME" \
  --key '{"StateId":{"S":"default"}}' \
  --output table
```

Interpretation:
- Healthy progression: `offset` increases until end of window.
- Window completion: `offset` resets to `0` and `since` advances to Bamboo `latest`.
- Error case: `since` and `offset` stay pinned to retry same window next run.

### 4.3 Check schedule config
```bash
aws scheduler get-schedule \
  --region "$AWS_REGION" \
  --name "${STACK_NAME}-nightly" \
  --group-name default \
  --query "{Expression:ScheduleExpression,Timezone:ScheduleExpressionTimezone,State:State}" \
  --output table
```

Expected:
- `Expression = cron(0 3 * * ? *)`
- `Timezone = Europe/London`
- `State = ENABLED`

## 5) Common Operational Changes
### 5.1 Update schedule and throughput parameters
Use CloudFormation deploy and set:
- `ScheduleExpression='cron(0 3 * * ? *)'`
- `ScheduleTimezone='Europe/London'`
- `BatchSize=0`

```bash
ALERT_EMAIL='kamila.bajaria@finova.tech'
BAMBOO_DOMAIN='finova'
MOODLE_URL='https://learn.novalxp.co.uk'

aws cloudformation deploy \
  --region "$AWS_REGION" \
  --stack-name "$STACK_NAME" \
  --template-file /Users/stovermcilwain/Projects/bamboohr-moodle-sync/infra/template.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    ProjectName="$STACK_NAME" \
    VpcId=vpc-08f9c6954eb86dcc4 \
    SubnetIds=$SUBNET_1,$SUBNET_2 \
    SecurityGroupId=$SG_ID \
    ScheduleExpression='cron(0 3 * * ? *)' \
    ScheduleTimezone='Europe/London' \
    BatchSize=0 \
    InitialLookbackDays=14 \
    AlertEmail="$ALERT_EMAIL" \
    BambooCompanyDomain="$BAMBOO_DOMAIN" \
    MoodleBaseUrl="$MOODLE_URL" \
  --no-cli-pager
```

### 5.2 Rewind state by ~2 weeks (one-time catch-up)
```bash
SINCE=$(python3 -c 'from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)-timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ"))')
NOW=$(python3 -c 'from datetime import datetime,timezone; print(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))')

aws dynamodb put-item \
  --region "$AWS_REGION" \
  --table-name "$TABLE_NAME" \
  --item "{\"StateId\":{\"S\":\"default\"},\"since\":{\"S\":\"$SINCE\"},\"offset\":{\"N\":\"0\"},\"updatedAt\":{\"S\":\"$NOW\"}}"
```

## 6) Troubleshooting
### Symptom: `accessexception` from Moodle user lookup
Meaning:
- Moodle token/service lacks rights for lookup functions.

Actions:
1. Confirm enabled functions for token service:
   - `core_user_get_users`
   - `core_user_get_users_by_field`
   - `core_user_create_users`
   - `core_user_update_users`
2. Confirm token user capabilities to read/create/update users.
3. Re-run one manual task and verify summary `errors=0`.

### Symptom: Bamboo 403 on changed or directory endpoints
Meaning:
- Bamboo API key does not have required employee visibility.

Actions:
1. Grant API user access to target employee scope.
2. Confirm directory access in Bamboo.
3. Re-run one manual task.

### Symptom: No SNS email received
Actions:
1. Confirm subscription is `Confirmed`.
2. Confirm task actually reached `STOPPED`.
3. Confirm EventBridge rules exist:
   - `${STACK_NAME}-task-succeeded`
   - `${STACK_NAME}-task-failed`
4. Confirm SNS topic policy allows EventBridge publish.

### Symptom: Task fails before processing
Actions:
1. Check `/ecs/${STACK_NAME}` logs.
2. Verify secrets exist and contain correct keys:
   - Bamboo secret key: `bamboohr_api_key`
   - Moodle secret key: `moodle_token`
3. Verify task can reach public Bamboo and Moodle endpoints.

## 7) Secret Rotation
Update secret values without infrastructure redeploy:

```bash
BAMBOO_ARN=$(aws cloudformation describe-stacks \
  --region "$AWS_REGION" \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='BambooSecretArn'].OutputValue" \
  --output text)

MOODLE_ARN=$(aws cloudformation describe-stacks \
  --region "$AWS_REGION" \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='MoodleSecretArn'].OutputValue" \
  --output text)

aws secretsmanager put-secret-value --region "$AWS_REGION" --secret-id "$BAMBOO_ARN" --secret-string '{"bamboohr_api_key":"REPLACE_ME"}'
aws secretsmanager put-secret-value --region "$AWS_REGION" --secret-id "$MOODLE_ARN" --secret-string '{"moodle_token":"REPLACE_ME"}'
```
