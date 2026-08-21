"""
Testes unitários do analytics-service.

Estratégia: mockamos boto3 inteiramente via patch nas variáveis de módulo
para isolar a lógica de process_message sem SQS/DynamoDB reais.
O módulo tem sys.exit(1) durante importação caso as env vars não estejam
definidas — resolvemos isso com monkeypatch + patch em boto3.Session.
"""
import json
import uuid
import pytest
from unittest.mock import patch, MagicMock, call


# ── Fixture: isola importação ────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_SQS_URL", "https://sqs.us-east-1.amazonaws.com/123/test-queue")
    monkeypatch.setenv("AWS_DYNAMODB_TABLE", "test-table")


@pytest.fixture
def analytics_module(mock_env):
    """Importa o módulo com os clientes boto3 mockados."""
    with patch("boto3.Session") as mock_session_cls:
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_sqs = MagicMock()
        mock_dynamo = MagicMock()
        mock_session.client.side_effect = lambda svc: (
            mock_sqs if svc == "sqs" else mock_dynamo
        )

        import sys
        if "app" in sys.modules:
            del sys.modules["app"]

        import app as analytics_app
        # Substitui os clientes globais pelos mocks
        analytics_app.sqs_client = mock_sqs
        analytics_app.dynamodb_client = mock_dynamo

        yield analytics_app, mock_sqs, mock_dynamo


# ── Helpers ──────────────────────────────────────────────────────────────────

def make_message(body: dict, msg_id: str = "msg-001", receipt: str = "rcpt-001"):
    return {
        "MessageId": msg_id,
        "ReceiptHandle": receipt,
        "Body": json.dumps(body),
    }


VALID_BODY = {
    "user_id": "user-123",
    "flag_name": "dark-mode",
    "result": True,
    "timestamp": "2024-01-01T00:00:00Z",
}


# ── process_message ───────────────────────────────────────────────────────────

class TestProcessMessage:
    def test_valid_message_inserts_to_dynamodb_and_deletes_from_sqs(
        self, analytics_module
    ):
        module, mock_sqs, mock_dynamo = analytics_module
        msg = make_message(VALID_BODY)

        module.process_message(msg)

        # Verifica que put_item foi chamado
        mock_dynamo.put_item.assert_called_once()
        call_kwargs = mock_dynamo.put_item.call_args[1]
        item = call_kwargs["Item"]
        assert item["user_id"]["S"] == "user-123"
        assert item["flag_name"]["S"] == "dark-mode"
        assert item["result"]["BOOL"] is True

        # Verifica que a mensagem foi deletada da fila
        mock_sqs.delete_message.assert_called_once_with(
            QueueUrl=module.SQS_QUEUE_URL,
            ReceiptHandle="rcpt-001",
        )

    def test_invalid_json_body_does_not_delete_message(self, analytics_module):
        module, mock_sqs, mock_dynamo = analytics_module
        bad_msg = {
            "MessageId": "bad-msg",
            "ReceiptHandle": "rcpt-002",
            "Body": "not valid json {{{",
        }

        module.process_message(bad_msg)

        mock_dynamo.put_item.assert_not_called()
        mock_sqs.delete_message.assert_not_called()

    def test_dynamodb_client_error_does_not_delete_message(self, analytics_module):
        from botocore.exceptions import ClientError
        module, mock_sqs, mock_dynamo = analytics_module

        mock_dynamo.put_item.side_effect = ClientError(
            {"Error": {"Code": "InternalError", "Message": "fail"}},
            "PutItem",
        )

        msg = make_message(VALID_BODY)
        module.process_message(msg)

        # Não deve deletar se houve erro no DynamoDB
        mock_sqs.delete_message.assert_not_called()

    def test_unexpected_exception_does_not_delete_message(self, analytics_module):
        module, mock_sqs, mock_dynamo = analytics_module
        mock_dynamo.put_item.side_effect = RuntimeError("boom")

        msg = make_message(VALID_BODY)
        module.process_message(msg)

        mock_sqs.delete_message.assert_not_called()

    def test_event_id_is_unique_uuid(self, analytics_module):
        """Cada mensagem processada deve gerar um event_id UUID único."""
        module, mock_sqs, mock_dynamo = analytics_module

        ids = set()
        for i in range(3):
            module.process_message(make_message(VALID_BODY, msg_id=f"msg-{i}"))
            call_kwargs = mock_dynamo.put_item.call_args_list[i][1]
            event_id = call_kwargs["Item"]["event_id"]["S"]
            # Verifica que é um UUID válido
            uuid.UUID(event_id)
            ids.add(event_id)

        assert len(ids) == 3, "Cada mensagem deve ter um event_id único"


# ── /health ───────────────────────────────────────────────────────────────────

class TestHealth:
    def test_health_endpoint_returns_ok(self, analytics_module):
        module, *_ = analytics_module
        module.app.config["TESTING"] = True
        client = module.app.test_client()

        res = client.get("/health")
        assert res.status_code == 200
        assert res.get_json() == {"status": "ok"}
