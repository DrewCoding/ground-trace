set -euo pipefail

REGION="${AWS_REGION:-us-west-1}"
PROJECT="${PROJECT_NAME:-ground-trace}"
CLUSTER="${CLUSTER:-${PROJECT}-cluster}"
TASK_DEF="${TASK_DEF:-${PROJECT}-game-server}"
CONTAINER="${CONTAINER_NAME:-game-server}"
GAME_PORT="${GAME_PORT:-7777}"

IDLE_SECONDS="${IDLE_SECONDS:-900}"

echo "Resolving networking..."
SUBNET=$(aws ec2 describe-subnets --region "$REGION" \
  --filters "Name=tag:Name,Values=${PROJECT}-vpc-public-*" \
  --query "Subnets[0].SubnetId" --output text)

SG=$(aws ec2 describe-security-groups --region "$REGION" \
  --filters "Name=group-name,Values=${PROJECT}-game-server" \
  --query "SecurityGroups[0].GroupId" --output text)

if [ "$SUBNET" = "None" ] || [ "$SG" = "None" ]; then
  echo "Could not resolve subnet or security group - is the infrastructure applied?" >&2
  exit 1
fi

echo "  subnet: $SUBNET"
echo "  sg:     $SG"

echo "Launching task..."
TASK_ARN=$(aws ecs run-task --region "$REGION" \
  --cluster "$CLUSTER" \
  --task-definition "$TASK_DEF" \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNET],securityGroups=[$SG],assignPublicIp=ENABLED}" \
  --overrides "{\"containerOverrides\":[{\"name\":\"$CONTAINER\",\"environment\":[{\"name\":\"EMPTY_SHUTDOWN_SECONDS\",\"value\":\"$IDLE_SECONDS\"}]}]}" \
  --query "tasks[0].taskArn" --output text)

TASK_ID="${TASK_ARN##*/}"
echo "  task:   $TASK_ID"

echo "Waiting for RUNNING (cold start is usually 30-60s)..."
if ! aws ecs wait tasks-running --region "$REGION" --cluster "$CLUSTER" --tasks "$TASK_ARN"; then
  echo
  echo "Task never reached RUNNING. Stop reason:" >&2
  aws ecs describe-tasks --region "$REGION" --cluster "$CLUSTER" --tasks "$TASK_ARN" \
    --query "tasks[0].{stopped:stoppedReason,containers:containers[].reason}" --output json >&2
  exit 1
fi

ENI=$(aws ecs describe-tasks --region "$REGION" --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  --query "tasks[0].attachments[0].details[?name=='networkInterfaceId'].value" --output text)

IP=$(aws ec2 describe-network-interfaces --region "$REGION" --network-interface-ids "$ENI" \
  --query "NetworkInterfaces[0].Association.PublicIp" --output text)

echo
echo "  Server ready at   ${IP}:${GAME_PORT}"
echo
echo "  Stop it with:"
echo "    aws ecs stop-task --cluster $CLUSTER --task $TASK_ID --region $REGION"
