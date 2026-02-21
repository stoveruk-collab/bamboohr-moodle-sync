#!/usr/bin/env bash
set -euo pipefail

PROJECT_NAME="bamboohr-novalxp-sync"
AWS_REGION="us-east-1"
VPC_CIDR="10.42.0.0/16"
SUBNET_A_CIDR="10.42.1.0/24"
SUBNET_B_CIDR="10.42.2.0/24"
OUTPUT_FILE="infra/network-outputs.json"

usage() {
  cat <<USAGE
Usage: $0 [options]

Options:
  --project-name <name>      Resource prefix (default: ${PROJECT_NAME})
  --region <region>          AWS region (default: ${AWS_REGION})
  --vpc-cidr <cidr>          VPC CIDR block (default: ${VPC_CIDR})
  --subnet-a-cidr <cidr>     Public subnet A CIDR (default: ${SUBNET_A_CIDR})
  --subnet-b-cidr <cidr>     Public subnet B CIDR (default: ${SUBNET_B_CIDR})
  --output-file <path>       Output JSON path (default: ${OUTPUT_FILE})
  --help                     Show this help
USAGE
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

none_to_empty() {
  if [[ "$1" == "None" ]]; then
    echo ""
  else
    echo "$1"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-name)
      PROJECT_NAME="$2"
      shift 2
      ;;
    --region)
      AWS_REGION="$2"
      shift 2
      ;;
    --vpc-cidr)
      VPC_CIDR="$2"
      shift 2
      ;;
    --subnet-a-cidr)
      SUBNET_A_CIDR="$2"
      shift 2
      ;;
    --subnet-b-cidr)
      SUBNET_B_CIDR="$2"
      shift 2
      ;;
    --output-file)
      OUTPUT_FILE="$2"
      shift 2
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

require_cmd aws

echo "Using region=${AWS_REGION} project=${PROJECT_NAME}"
aws sts get-caller-identity --region "${AWS_REGION}" >/dev/null

AZS=()
while IFS= read -r az; do
  [[ -n "${az}" ]] || continue
  AZS+=("${az}")
  [[ ${#AZS[@]} -ge 2 ]] && break
done < <(
  aws ec2 describe-availability-zones \
    --region "${AWS_REGION}" \
    --filters Name=state,Values=available \
    --query 'AvailabilityZones[].ZoneName' \
    --output text | tr '\t' '\n' | sed '/^$/d'
)

if [[ ${#AZS[@]} -lt 2 ]]; then
  echo "Need at least 2 available AZs in ${AWS_REGION}" >&2
  exit 1
fi

AZ_A="${AZS[0]}"
AZ_B="${AZS[1]}"

echo "AZs: ${AZ_A}, ${AZ_B}"

VPC_ID=$(none_to_empty "$(aws ec2 describe-vpcs \
  --region "${AWS_REGION}" \
  --filters "Name=tag:Name,Values=${PROJECT_NAME}-vpc" "Name=state,Values=available" \
  --query 'Vpcs[0].VpcId' \
  --output text)")

if [[ -z "${VPC_ID}" ]]; then
  VPC_ID=$(aws ec2 create-vpc \
    --region "${AWS_REGION}" \
    --cidr-block "${VPC_CIDR}" \
    --tag-specifications "ResourceType=vpc,Tags=[{Key=Name,Value=${PROJECT_NAME}-vpc},{Key=Project,Value=${PROJECT_NAME}}]" \
    --query 'Vpc.VpcId' \
    --output text)
  aws ec2 wait vpc-available --region "${AWS_REGION}" --vpc-ids "${VPC_ID}"
  aws ec2 modify-vpc-attribute --region "${AWS_REGION}" --vpc-id "${VPC_ID}" --enable-dns-support '{"Value":true}'
  aws ec2 modify-vpc-attribute --region "${AWS_REGION}" --vpc-id "${VPC_ID}" --enable-dns-hostnames '{"Value":true}'
  echo "Created VPC ${VPC_ID}"
else
  echo "Reusing VPC ${VPC_ID}"
fi

IGW_ID=$(none_to_empty "$(aws ec2 describe-internet-gateways \
  --region "${AWS_REGION}" \
  --filters "Name=attachment.vpc-id,Values=${VPC_ID}" \
  --query 'InternetGateways[0].InternetGatewayId' \
  --output text)")

if [[ -z "${IGW_ID}" ]]; then
  IGW_ID=$(aws ec2 create-internet-gateway \
    --region "${AWS_REGION}" \
    --tag-specifications "ResourceType=internet-gateway,Tags=[{Key=Name,Value=${PROJECT_NAME}-igw},{Key=Project,Value=${PROJECT_NAME}}]" \
    --query 'InternetGateway.InternetGatewayId' \
    --output text)
  aws ec2 attach-internet-gateway --region "${AWS_REGION}" --internet-gateway-id "${IGW_ID}" --vpc-id "${VPC_ID}"
  echo "Created and attached IGW ${IGW_ID}"
else
  echo "Reusing IGW ${IGW_ID}"
fi

ROUTE_TABLE_ID=$(none_to_empty "$(aws ec2 describe-route-tables \
  --region "${AWS_REGION}" \
  --filters "Name=vpc-id,Values=${VPC_ID}" "Name=tag:Name,Values=${PROJECT_NAME}-public-rt" \
  --query 'RouteTables[0].RouteTableId' \
  --output text)")

if [[ -z "${ROUTE_TABLE_ID}" ]]; then
  ROUTE_TABLE_ID=$(aws ec2 create-route-table \
    --region "${AWS_REGION}" \
    --vpc-id "${VPC_ID}" \
    --tag-specifications "ResourceType=route-table,Tags=[{Key=Name,Value=${PROJECT_NAME}-public-rt},{Key=Project,Value=${PROJECT_NAME}}]" \
    --query 'RouteTable.RouteTableId' \
    --output text)
  echo "Created route table ${ROUTE_TABLE_ID}"
else
  echo "Reusing route table ${ROUTE_TABLE_ID}"
fi

DEFAULT_ROUTE=$(none_to_empty "$(aws ec2 describe-route-tables \
  --region "${AWS_REGION}" \
  --route-table-ids "${ROUTE_TABLE_ID}" \
  --query "RouteTables[0].Routes[?DestinationCidrBlock=='0.0.0.0/0'].GatewayId | [0]" \
  --output text)")

if [[ -z "${DEFAULT_ROUTE}" ]]; then
  aws ec2 create-route \
    --region "${AWS_REGION}" \
    --route-table-id "${ROUTE_TABLE_ID}" \
    --destination-cidr-block 0.0.0.0/0 \
    --gateway-id "${IGW_ID}" >/dev/null
  echo "Added 0.0.0.0/0 route via ${IGW_ID}"
else
  echo "Default route already exists via ${DEFAULT_ROUTE}"
fi

create_or_get_subnet() {
  local subnet_name="$1"
  local az="$2"
  local cidr="$3"

  local subnet_id
  subnet_id=$(none_to_empty "$(aws ec2 describe-subnets \
    --region "${AWS_REGION}" \
    --filters "Name=vpc-id,Values=${VPC_ID}" "Name=tag:Name,Values=${subnet_name}" \
    --query 'Subnets[0].SubnetId' \
    --output text)")

  if [[ -z "${subnet_id}" ]]; then
    subnet_id=$(aws ec2 create-subnet \
      --region "${AWS_REGION}" \
      --vpc-id "${VPC_ID}" \
      --availability-zone "${az}" \
      --cidr-block "${cidr}" \
      --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=${subnet_name}},{Key=Project,Value=${PROJECT_NAME}}]" \
      --query 'Subnet.SubnetId' \
      --output text)
    aws ec2 modify-subnet-attribute \
      --region "${AWS_REGION}" \
      --subnet-id "${subnet_id}" \
      --map-public-ip-on-launch
    echo "Created subnet ${subnet_id} (${subnet_name})" >&2
  else
    echo "Reusing subnet ${subnet_id} (${subnet_name})" >&2
  fi

  local assoc_id
  assoc_id=$(none_to_empty "$(aws ec2 describe-route-tables \
    --region "${AWS_REGION}" \
    --route-table-ids "${ROUTE_TABLE_ID}" \
    --query "RouteTables[0].Associations[?SubnetId=='${subnet_id}'].RouteTableAssociationId | [0]" \
    --output text)")

  if [[ -z "${assoc_id}" ]]; then
    aws ec2 associate-route-table \
      --region "${AWS_REGION}" \
      --route-table-id "${ROUTE_TABLE_ID}" \
      --subnet-id "${subnet_id}" >/dev/null
  fi

  echo "${subnet_id}"
}

SUBNET_A_ID=$(create_or_get_subnet "${PROJECT_NAME}-public-a" "${AZ_A}" "${SUBNET_A_CIDR}")
SUBNET_B_ID=$(create_or_get_subnet "${PROJECT_NAME}-public-b" "${AZ_B}" "${SUBNET_B_CIDR}")

SG_ID=$(none_to_empty "$(aws ec2 describe-security-groups \
  --region "${AWS_REGION}" \
  --filters "Name=vpc-id,Values=${VPC_ID}" "Name=group-name,Values=${PROJECT_NAME}-ecs-sg" \
  --query 'SecurityGroups[0].GroupId' \
  --output text)")

if [[ -z "${SG_ID}" ]]; then
  SG_ID=$(aws ec2 create-security-group \
    --region "${AWS_REGION}" \
    --vpc-id "${VPC_ID}" \
    --group-name "${PROJECT_NAME}-ecs-sg" \
    --description "ECS task SG for ${PROJECT_NAME}; outbound internet only" \
    --tag-specifications "ResourceType=security-group,Tags=[{Key=Name,Value=${PROJECT_NAME}-ecs-sg},{Key=Project,Value=${PROJECT_NAME}}]" \
    --query 'GroupId' \
    --output text)
  echo "Created security group ${SG_ID}"
else
  echo "Reusing security group ${SG_ID}"
fi

mkdir -p "$(dirname "${OUTPUT_FILE}")"
cat > "${OUTPUT_FILE}" <<JSON
{
  "ProjectName": "${PROJECT_NAME}",
  "Region": "${AWS_REGION}",
  "VpcId": "${VPC_ID}",
  "SubnetIds": ["${SUBNET_A_ID}", "${SUBNET_B_ID}"],
  "SecurityGroupId": "${SG_ID}",
  "AvailabilityZones": ["${AZ_A}", "${AZ_B}"]
}
JSON

echo "Wrote ${OUTPUT_FILE}"

cat <<SUMMARY

Use these CloudFormation parameters:
  ProjectName=${PROJECT_NAME}
  VpcId=${VPC_ID}
  SubnetIds=${SUBNET_A_ID},${SUBNET_B_ID}
  SecurityGroupId=${SG_ID}
SUMMARY
