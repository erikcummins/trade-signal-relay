# Deployment

## Deploy

```bash
./infra/deploy.sh                # full deploy (idempotent)
./infra/deploy.sh update         # update Lambda code only
```

Creates: IAM role, 3 DynamoDB tables, Lambda function (Python 3.12), API Gateway WebSocket API with `$connect`/`$disconnect`/`$default` routes, `prod` stage.

State tracked in `infra/.deploy-state` (gitignored).

Region: `AWS_REGION` env var, default `us-east-1`.

## Manage Access

```bash
./infra/deploy.sh add-publisher myalgo           # prints: pub_myalgo_<random8>
./infra/deploy.sh add-subscriber sub_alice_x8k2 algo1,algo2
./infra/deploy.sh remove-subscriber sub_alice_x8k2
./infra/deploy.sh status                          # shows resources + connection count
```

## Teardown

```bash
./infra/teardown.sh    # prompts for "yes" confirmation
```

Deletes API Gateway, Lambda, IAM role+policy, DynamoDB tables, state file.

## Lambda Packaging

`deploy.sh` zips `relay_server/` + `shared/` (excluding `__pycache__`). `boto3` is available in the Lambda runtime. Handler: `relay_server.server.handler`.
