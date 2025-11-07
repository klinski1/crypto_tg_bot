from aws_cdk import (
    Stack, Duration,
    aws_lambda as lambda_, aws_apigateway as apigw,
    aws_dynamodb as ddb, aws_iam, aws_logs
)
from constructs import Construct

class CryptoBotStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        table = ddb.Table(self, "Users",
            partition_key=ddb.Attribute(name="telegram_id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True)

        fn = lambda_.Function(self, "Webhook",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="webhook.handler",
            code=lambda_.Code.from_asset("cdk/lambda_functions"),
            memory_size=512,
            timeout=Duration.seconds(10),
            environment={"TABLE_NAME": table.table_name},
            log_retention=aws_logs.RetentionDays.ONE_WEEK)

        table.grant_read_write_data(fn)
        fn.add_to_role_policy(aws_iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue"],
            resources=["arn:aws:secretsmanager:*:*:secret:TELEGRAM_BOT_TOKEN*",
                       "arn:aws:secretsmanager:*:*:secret:XAI_API_KEY*",
                       "arn:aws:secretsmanager:*:*:secret:ENCRYPTION_KEY*"]))

        api = apigw.RestApi(self, "Api", deploy_options=apigw.StageOptions(stage_name="prod"))
        webhook = api.root.add_resource("webhook")
        webhook.add_method("POST", apigw.LambdaIntegration(fn))

        self.webhook_url = api.url_for_path("/webhook")
