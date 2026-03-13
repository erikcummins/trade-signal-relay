#!/usr/bin/env bash
set -e

REGION="${AWS_REGION:-us-east-1}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
STATE_FILE="$SCRIPT_DIR/.deploy-state"

FUNCTION_NAME="trade-signal-relay"
ROLE_NAME="trade-signal-relay-lambda-role"
API_NAME="trade-signal-relay-ws"
STAGE_NAME="prod"
CONNECTIONS_TABLE="relay-connections"
ACCESS_TABLE="relay-access"
SIGNALS_TABLE="relay-signals"

load_state() {
    if [[ -f "$STATE_FILE" ]]; then
        source "$STATE_FILE"
    fi
}

save_state() {
    local key="$1" value="$2"
    if grep -q "^${key}=" "$STATE_FILE" 2>/dev/null; then
        sed -i.bak "s|^${key}=.*|${key}=${value}|" "$STATE_FILE" && rm -f "$STATE_FILE.bak"
    else
        echo "${key}=${value}" >> "$STATE_FILE"
    fi
}

package_lambda() {
    echo "Packaging Lambda code..."
    local zip_file="$SCRIPT_DIR/lambda.zip"
    rm -f "$zip_file"
    cd "$PROJECT_DIR"
    zip -qr "$zip_file" relay_server/ shared/ -x '*__pycache__*'
    cd - > /dev/null
    echo "  Created $zip_file"
}

create_iam_role() {
    load_state
    if [[ -n "${ROLE_ARN:-}" ]]; then
        echo "IAM role already exists: $ROLE_ARN"
        return
    fi

    echo "Creating IAM role..."
    local trust_policy='{
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole"
        }]
    }'

    ROLE_ARN=$(aws iam create-role \
        --role-name "$ROLE_NAME" \
        --assume-role-policy-document "$trust_policy" \
        --query 'Role.Arn' --output text)
    save_state "ROLE_ARN" "$ROLE_ARN"
    echo "  Created role: $ROLE_ARN"

    echo "Attaching policies..."
    local policy='{
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "dynamodb:PutItem",
                    "dynamodb:GetItem",
                    "dynamodb:DeleteItem",
                    "dynamodb:Scan",
                    "dynamodb:Query"
                ],
                "Resource": "arn:aws:dynamodb:'"$REGION"':*:table/relay-*"
            },
            {
                "Effect": "Allow",
                "Action": "execute-api:ManageConnections",
                "Resource": "arn:aws:execute-api:'"$REGION"':*:*/@connections/*"
            },
            {
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents"
                ],
                "Resource": "arn:aws:logs:'"$REGION"':*:*"
            }
        ]
    }'

    POLICY_ARN=$(aws iam create-policy \
        --policy-name "${ROLE_NAME}-policy" \
        --policy-document "$policy" \
        --query 'Policy.Arn' --output text)
    save_state "POLICY_ARN" "$POLICY_ARN"

    aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn "$POLICY_ARN"
    echo "  Attached inline policy"

    echo "  Waiting for role propagation..."
    sleep 10
}

create_dynamodb_tables() {
    for table in "$CONNECTIONS_TABLE" "$ACCESS_TABLE" "$SIGNALS_TABLE"; do
        if aws dynamodb describe-table --table-name "$table" --region "$REGION" &>/dev/null; then
            echo "DynamoDB table already exists: $table"
            continue
        fi

        echo "Creating DynamoDB table: $table..."
        if [[ "$table" == "$SIGNALS_TABLE" ]]; then
            aws dynamodb create-table \
                --table-name "$table" \
                --attribute-definitions \
                    AttributeName=algo_id,AttributeType=S \
                    "AttributeName=timestamp#signal_id,AttributeType=S" \
                --key-schema \
                    AttributeName=algo_id,KeyType=HASH \
                    "AttributeName=timestamp#signal_id,KeyType=RANGE" \
                --billing-mode PAY_PER_REQUEST \
                --region "$REGION" > /dev/null

            aws dynamodb wait table-exists --table-name "$table" --region "$REGION"

            aws dynamodb update-time-to-live \
                --table-name "$table" \
                --time-to-live-specification "Enabled=true,AttributeName=ttl" \
                --region "$REGION" > /dev/null
        elif [[ "$table" == "$CONNECTIONS_TABLE" ]]; then
            aws dynamodb create-table \
                --table-name "$table" \
                --attribute-definitions AttributeName=connection_id,AttributeType=S \
                --key-schema AttributeName=connection_id,KeyType=HASH \
                --billing-mode PAY_PER_REQUEST \
                --region "$REGION" > /dev/null
            aws dynamodb wait table-exists --table-name "$table" --region "$REGION"
        else
            aws dynamodb create-table \
                --table-name "$table" \
                --attribute-definitions AttributeName=subscriber_key,AttributeType=S \
                --key-schema AttributeName=subscriber_key,KeyType=HASH \
                --billing-mode PAY_PER_REQUEST \
                --region "$REGION" > /dev/null
            aws dynamodb wait table-exists --table-name "$table" --region "$REGION"
        fi
        echo "  Created $table"
    done
}

create_lambda() {
    load_state
    local zip_file="$SCRIPT_DIR/lambda.zip"

    if [[ -n "${FUNCTION_ARN:-}" ]]; then
        echo "Lambda function already exists, updating code..."
        aws lambda update-function-code \
            --function-name "$FUNCTION_NAME" \
            --zip-file "fileb://$zip_file" \
            --region "$REGION" > /dev/null
        echo "  Updated Lambda code"
        return
    fi

    echo "Creating Lambda function..."
    FUNCTION_ARN=$(aws lambda create-function \
        --function-name "$FUNCTION_NAME" \
        --runtime python3.12 \
        --handler relay_server.server.handler \
        --role "$ROLE_ARN" \
        --zip-file "fileb://$zip_file" \
        --timeout 30 \
        --environment "Variables={CONNECTIONS_TABLE=$CONNECTIONS_TABLE,ACCESS_TABLE=$ACCESS_TABLE,SIGNALS_TABLE=$SIGNALS_TABLE}" \
        --region "$REGION" \
        --query 'FunctionArn' --output text)
    save_state "FUNCTION_ARN" "$FUNCTION_ARN"
    echo "  Created Lambda: $FUNCTION_ARN"

    aws lambda wait function-active-v2 --function-name "$FUNCTION_NAME" --region "$REGION"
}

create_api_gateway() {
    load_state
    if [[ -n "${API_ID:-}" ]]; then
        echo "API Gateway already exists: $API_ID"
        return
    fi

    echo "Creating API Gateway WebSocket API..."
    API_ID=$(aws apigatewayv2 create-api \
        --name "$API_NAME" \
        --protocol-type WEBSOCKET \
        --route-selection-expression '$request.body.type' \
        --region "$REGION" \
        --query 'ApiId' --output text)
    save_state "API_ID" "$API_ID"
    echo "  Created API: $API_ID"

    echo "Creating Lambda integration..."
    INTEGRATION_ID=$(aws apigatewayv2 create-integration \
        --api-id "$API_ID" \
        --integration-type AWS_PROXY \
        --integration-uri "arn:aws:apigateway:${REGION}:lambda:path/2015-03-31/functions/${FUNCTION_ARN}/invocations" \
        --region "$REGION" \
        --query 'IntegrationId' --output text)
    save_state "INTEGRATION_ID" "$INTEGRATION_ID"
    echo "  Created integration: $INTEGRATION_ID"

    echo "Creating routes..."
    for route in '$connect' '$disconnect' '$default'; do
        ROUTE_ID=$(aws apigatewayv2 create-route \
            --api-id "$API_ID" \
            --route-key "$route" \
            --target "integrations/$INTEGRATION_ID" \
            --region "$REGION" \
            --query 'RouteId' --output text)
        local state_key="ROUTE_ID_$(echo "$route" | tr '$' '_' | tr '-' '_')"
        save_state "$state_key" "$ROUTE_ID"
        echo "  Created route $route: $ROUTE_ID"
    done

    echo "Creating deployment and stage..."
    DEPLOYMENT_ID=$(aws apigatewayv2 create-deployment \
        --api-id "$API_ID" \
        --region "$REGION" \
        --query 'DeploymentId' --output text)
    save_state "DEPLOYMENT_ID" "$DEPLOYMENT_ID"

    aws apigatewayv2 create-stage \
        --api-id "$API_ID" \
        --stage-name "$STAGE_NAME" \
        --deployment-id "$DEPLOYMENT_ID" \
        --region "$REGION" > /dev/null
    echo "  Created stage: $STAGE_NAME"

    echo "Granting API Gateway permission to invoke Lambda..."
    local account_id
    account_id=$(aws sts get-caller-identity --query 'Account' --output text)
    aws lambda add-permission \
        --function-name "$FUNCTION_NAME" \
        --statement-id "apigateway-invoke-${API_ID}" \
        --action lambda:InvokeFunction \
        --principal apigateway.amazonaws.com \
        --source-arn "arn:aws:execute-api:${REGION}:${account_id}:${API_ID}/*" \
        --region "$REGION" > /dev/null
    echo "  Permission granted"
}

full_deploy() {
    echo "=== Trade Signal Relay — Full Deploy ==="
    package_lambda
    create_iam_role
    create_dynamodb_tables
    create_lambda
    create_api_gateway
    load_state
    echo ""
    echo "=== Deploy Complete ==="
    echo "WebSocket URL: wss://${API_ID}.execute-api.${REGION}.amazonaws.com/${STAGE_NAME}"
}

cmd_update() {
    echo "=== Updating Lambda Code ==="
    package_lambda
    load_state
    aws lambda update-function-code \
        --function-name "$FUNCTION_NAME" \
        --zip-file "fileb://$SCRIPT_DIR/lambda.zip" \
        --region "$REGION" > /dev/null
    echo "Lambda code updated"
}

cmd_add_subscriber() {
    local key="$1" algos="$2"
    if [[ -z "$key" || -z "$algos" ]]; then
        echo "Usage: $0 add-subscriber <subscriber_key> <algo_id1,algo_id2>"
        exit 1
    fi

    IFS=',' read -ra algo_list <<< "$algos"
    local algo_json
    algo_json=$(printf '"%s",' "${algo_list[@]}")
    algo_json="[${algo_json%,}]"

    aws dynamodb put-item \
        --table-name "$ACCESS_TABLE" \
        --item "{\"subscriber_key\": {\"S\": \"$key\"}, \"allowed_algos\": {\"L\": $(echo "$algo_json" | sed 's/"\([^"]*\)"/{"S": "\1"}/g')}}" \
        --region "$REGION"
    echo "Added subscriber $key with algos: $algos"
}

cmd_remove_subscriber() {
    local key="$1"
    if [[ -z "$key" ]]; then
        echo "Usage: $0 remove-subscriber <subscriber_key>"
        exit 1
    fi

    aws dynamodb delete-item \
        --table-name "$ACCESS_TABLE" \
        --key "{\"subscriber_key\": {\"S\": \"$key\"}}" \
        --region "$REGION"
    echo "Removed subscriber $key"
}

cmd_add_publisher() {
    local algo_id="$1"
    if [[ -z "$algo_id" ]]; then
        echo "Usage: $0 add-publisher <algo_id>"
        exit 1
    fi

    local random_part
    random_part=$(LC_ALL=C tr -dc 'a-zA-Z0-9' < /dev/urandom | head -c 8)
    local key="pub_${algo_id}_${random_part}"
    echo "Publisher key: $key"
}

cmd_status() {
    echo "=== Trade Signal Relay — Status ==="
    load_state

    echo ""
    echo "Region: $REGION"

    if [[ -n "${FUNCTION_ARN:-}" ]]; then
        echo "Lambda: $FUNCTION_ARN"
    else
        echo "Lambda: not deployed"
    fi

    if [[ -n "${API_ID:-}" ]]; then
        echo "API Gateway: $API_ID"
        echo "WebSocket URL: wss://${API_ID}.execute-api.${REGION}.amazonaws.com/${STAGE_NAME}"
    else
        echo "API Gateway: not deployed"
    fi

    echo ""
    echo "DynamoDB Tables:"
    for table in "$CONNECTIONS_TABLE" "$ACCESS_TABLE" "$SIGNALS_TABLE"; do
        if aws dynamodb describe-table --table-name "$table" --region "$REGION" &>/dev/null; then
            local count
            count=$(aws dynamodb scan --table-name "$table" --select COUNT --region "$REGION" --query 'Count' --output text)
            echo "  $table: $count items"
        else
            echo "  $table: not created"
        fi
    done

    if aws dynamodb describe-table --table-name "$CONNECTIONS_TABLE" --region "$REGION" &>/dev/null; then
        local connections
        connections=$(aws dynamodb scan \
            --table-name "$CONNECTIONS_TABLE" \
            --select COUNT \
            --region "$REGION" \
            --query 'Count' --output text)
        echo ""
        echo "Active connections: $connections"
    fi
}

case "${1:-}" in
    update)
        cmd_update
        ;;
    add-subscriber)
        cmd_add_subscriber "${2:-}" "${3:-}"
        ;;
    remove-subscriber)
        cmd_remove_subscriber "${2:-}"
        ;;
    add-publisher)
        cmd_add_publisher "${2:-}"
        ;;
    status)
        cmd_status
        ;;
    *)
        full_deploy
        ;;
esac
