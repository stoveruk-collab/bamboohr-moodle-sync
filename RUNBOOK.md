# BambooHR -> Moodle Sync Runbook

This runbook is for operators. It covers routine operations, manual runs, monitoring, and troubleshooting.

## Operator Quick Start

Set placeholder values for your own environment before running any commands:

```bash
export AWS_PROFILE=your-profile
export AWS_REGION=eu-west-2
export AWS_PAGER=""
export STACK_NAME=bamboohr-moodle-sync-prod
export CLUSTER_ARN=arn:aws:ecs:<region>:<account-id>:cluster/<cluster-name>
export SUBNET_1=subnet-aaaaaaaa
export SUBNET_2=subnet-bbbbbbbb
export SG_ID=sg-xxxxxxxx
```

Start one task:

```bash
TASK_DEF=$(aws ecs list-task-definitions --region "$AWS_REGION" --family-prefix "${STACK_NAME}-task" --sort DESC --max-items 1 --query "taskDefinitionArns[0]" --output text)
TASK_ARN=$(aws ecs run-task --region "$AWS_REGION" --cluster "$CLUSTER_ARN" --launch-type FARGATE --task-definition "$TASK_DEF" --network-configuration "awsvpcConfiguration={subnets=[$SUBNET_1,$SUBNET_2],securityGroups=[$SG_ID],assignPublicIp=ENABLED}" --query "tasks[0].taskArn" --output text)
echo "$TASK_ARN"
```

Wait for completion:

```bash
aws ecs wait tasks-stopped --region "$AWS_REGION" --cluster "$CLUSTER_ARN" --tasks "$TASK_ARN"
```

Check exit status:

```bash
aws ecs describe-tasks --region "$AWS_REGION" --cluster "$CLUSTER_ARN" --tasks "$TASK_ARN" --query "tasks[0].{ExitCode:containers[0].exitCode,LastStatus:lastStatus,StoppedReason:stoppedReason}" --output table
```

Tail logs:

```bash
aws logs tail "/ecs/${STACK_NAME}" --since 30m --format short
```

## Standard Operator Context

Use environment variables similar to:

```bash
export AWS_PROFILE=your-profile
export AWS_REGION=eu-west-2
export AWS_PAGER=""
export STACK_NAME=bamboohr-moodle-sync-prod
export CLUSTER_ARN=arn:aws:ecs:<region>:<account-id>:cluster/<cluster-name>
export SUBNET_1=subnet-aaaaaaaa
export SUBNET_2=subnet-bbbbbbbb
export SG_ID=sg-xxxxxxxx
```

## Expected State

- Schedule may be enabled or disabled depending on release state.
- Batch size may be capped or unlimited.
- Notification messages are emitted through SNS/EventBridge wiring defined in infrastructure.

## Monitoring and Validation

Check the state row:

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

Check the scheduler:

```bash
aws scheduler get-schedule \
  --region "$AWS_REGION" \
  --name "${STACK_NAME}-nightly" \
  --group-name default \
  --query "{Expression:ScheduleExpression,Timezone:ScheduleExpressionTimezone,State:State}" \
  --output table
```

## Operational Changes

Redeploy CloudFormation with your own parameter values:

```bash
ALERT_EMAIL='admin@example.com'
BAMBOO_DOMAIN='your-bamboo-domain'
MOODLE_URL='https://moodle.example.com'

aws cloudformation deploy \
  --region "$AWS_REGION" \
  --stack-name "$STACK_NAME" \
  --template-file infra/template.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    ProjectName="$STACK_NAME" \
    VpcId=vpc-xxxxxxxx \
    SubnetIds=$SUBNET_1,$SUBNET_2 \
    SecurityGroupId=$SG_ID \
    ScheduleExpression='cron(0 3 * * ? *)' \
    ScheduleTimezone='Europe/London' \
    ScheduleState=DISABLED \
    BatchSize=0 \
    InitialLookbackDays=14 \
    MoodleUsernameSource=email \
    AllowEmailFallback=false \
    EnforceCanonicalUsername=true \
    EnforceAuthOnUpdate=true \
    AlertEmail="$ALERT_EMAIL" \
    BambooCompanyDomain="$BAMBOO_DOMAIN" \
    MoodleBaseUrl="$MOODLE_URL" \
  --no-cli-pager
```

## Secret Rotation

Update secret values without redeploying infrastructure:

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

## Troubleshooting

Typical checks:

1. Verify ECS task exit status.
2. Verify `/ecs/${STACK_NAME}` logs.
3. Confirm Secrets Manager entries exist and contain expected keys.
4. Confirm the BambooHR API user has access to the relevant directory and changed-employee endpoints.
5. Confirm the Moodle token has rights for the required user lookup and user mutation functions.
