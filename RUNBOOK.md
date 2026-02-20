# BambooHR → Moodle Sync (AWS) — Runbook

## Goal
Deploy all-AWS sync: ECS Fargate scheduled task + CloudFormation + DynamoDB state + Secrets Manager + CloudWatch Logs + SNS email (success/fail per run).

## Repo
https://github.com/stoveruk-collab/bamboohr-moodle-sync

## Current AWS Environment
- Region: eu-west-2
- VPC_ID: vpc-09dc2f79fc671982f
- SUBNET_1: subnet-017b0553ae048af14
- SUBNET_2: subnet-00bd80c186be351d8
- SG_ID: sg-0a7c0cc7f31a6cd92
- Stack name: bamboohr-moodle-sync-dev
- Alert email: kamila.bajaria@finova.tech

## Current Status
- CloudFormation deploy previously failed.
- Next step is to validate `infra/template.yaml` to find the exact template error.

## Next Command (CloudShell)
aws cloudformation validate-template --region eu-west-2 --template-body file://infra/template.yaml

## Last Output / Error
(Paste validate-template output here)
