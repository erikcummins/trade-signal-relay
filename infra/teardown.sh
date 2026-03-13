#!/usr/bin/env bash
set -e

REGION="${AWS_REGION:-us-east-1}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_FILE="$SCRIPT_DIR/.deploy-state"

FUNCTION_NAME="trade-signal-relay"
ROLE_NAME="trade-signal-relay-lambda-role"
STAGE_NAME="prod"
CONNECTIONS_TABLE="relay-connections"
ACCESS_TABLE="relay-access"
SIGNALS_TABLE="relay-signals"

if [[ -f "$STATE_FILE" ]]; then
    source "$STATE_FILE"
fi

echo "=== Trade Signal Relay — Teardown ==="
echo "This will DELETE all deployed resources in region $REGION:"
echo "  - API Gateway: ${API_ID:-unknown}"
echo "  - Lambda: $FUNCTION_NAME"
echo "  - IAM role: $ROLE_NAME"
echo "  - DynamoDB tables: $CONNECTIONS_TABLE, $ACCESS_TABLE, $SIGNALS_TABLE"
echo ""
read -rp "Type 'yes' to confirm: " confirm
if [[ "$confirm" != "yes" ]]; then
    echo "Aborted."
    exit 0
fi

echo ""

if [[ -n "${API_ID:-}" ]]; then
    echo "Deleting API Gateway stage..."
    aws apigatewayv2 delete-stage \
        --api-id "$API_ID" --stage-name "$STAGE_NAME" \
        --region "$REGION" 2>/dev/null || true

    echo "Deleting API Gateway deployment..."
    if [[ -n "${DEPLOYMENT_ID:-}" ]]; then
        aws apigatewayv2 delete-deployment \
            --api-id "$API_ID" --deployment-id "$DEPLOYMENT_ID" \
            --region "$REGION" 2>/dev/null || true
    fi

    echo "Deleting API Gateway..."
    aws apigatewayv2 delete-api --api-id "$API_ID" --region "$REGION" 2>/dev/null || true
    echo "  Deleted API Gateway"
else
    echo "No API Gateway found, skipping"
fi

echo "Deleting Lambda function..."
aws lambda delete-function \
    --function-name "$FUNCTION_NAME" \
    --region "$REGION" 2>/dev/null || true
echo "  Deleted Lambda"

if [[ -n "${POLICY_ARN:-}" ]]; then
    echo "Detaching and deleting IAM policy..."
    aws iam detach-role-policy \
        --role-name "$ROLE_NAME" \
        --policy-arn "$POLICY_ARN" 2>/dev/null || true
    aws iam delete-policy --policy-arn "$POLICY_ARN" 2>/dev/null || true
    echo "  Deleted policy"
fi

echo "Deleting IAM role..."
aws iam delete-role --role-name "$ROLE_NAME" 2>/dev/null || true
echo "  Deleted role"

echo "Deleting DynamoDB tables..."
for table in "$CONNECTIONS_TABLE" "$ACCESS_TABLE" "$SIGNALS_TABLE"; do
    aws dynamodb delete-table --table-name "$table" --region "$REGION" 2>/dev/null || true
    echo "  Deleted $table"
done

echo "Cleaning up state file..."
rm -f "$STATE_FILE"
rm -f "$SCRIPT_DIR/lambda.zip"

echo ""
echo "=== Teardown Complete ==="
