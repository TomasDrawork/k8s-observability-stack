import os
import time
import structlog
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException
from prometheus_fastapi_instrumentator import Instrumentator
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

# 1. Configuración de OpenTelemetry Tracer
OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger-collector:4317")
resource = Resource.create({"service.name": "wallet-ledger"})
provider = TracerProvider(resource=resource)
processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=OTEL_ENDPOINT, insecure=True))
provider.add_span_processor(processor)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("wallet-ledger")

# 2. Correlación de logs: inyectar trace_id y span_id en logs JSON
def add_trace_context(logger, method_name, event_dict):
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx.is_valid:
        event_dict["trace_id"] = f"{ctx.trace_id:032x}"
        event_dict["span_id"] = f"{ctx.span_id:016x}"
    return event_dict

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        add_trace_context,
        structlog.processors.JSONRenderer()
    ]
)
logger = structlog.get_logger()

# 3. Inicialización y Auto-Instrumentación de la API
app = FastAPI(title="Wallet Ledger Service")
FastAPIInstrumentor.instrument_app(app)
Instrumentator().instrument(app).expose(app)

class TransferPayload(BaseModel):
    account_id: str
    amount: float
    currency: str

# Base de datos simulada en memoria
mock_balances = {"ACC-001": 5000.0, "ACC-002": 1200.0}

@app.post("/debit")
def process_debit(data: TransferPayload):
    logger.info("Verificando solicitud de debito", account=data.account_id, amount=data.amount)

    # Span manual: Validar fondos
    with tracer.start_as_current_span("validar_fondos") as span:
        span.set_attribute("account.id", data.account_id)
        span.set_attribute("transfer.amount", data.amount)
        current_balance = mock_balances.get(data.account_id, 0.0)
        time.sleep(0.04)  # Simula lectura de DB

        if current_balance < data.amount:
            span.set_attribute("validation.result", "insufficient_funds")
            logger.warning("Saldo insuficiente", account=data.account_id, balance=current_balance)
            raise HTTPException(status_code=400, detail="Fondos insuficientes")

    # Span manual: Aplicar descuento
    with tracer.start_as_current_span("debitar_cuenta") as span:
        mock_balances[data.account_id] -= data.amount
        span.set_attribute("balance.new", mock_balances[data.account_id])
        time.sleep(0.03)  # Simula escritura en DB

    logger.info("Debito aplicado correctamente", account=data.account_id, new_balance=mock_balances[data.account_id])
    return {
        "status": "APPROVED",
        "account_id": data.account_id,
        "remaining_balance": mock_balances[data.account_id]
    }

@app.get("/simulate-failure")
def simulate_failure(delay: float = 0.0, force_500: bool = False):
    """Endpoint de control para disparar el escenario del RCA."""
    if delay > 0:
        logger.warn("Inyectando latencia artificial en el ledger", delay_seconds=delay)
        time.sleep(delay)
    if force_500:
        logger.error("Simulando bloqueo de base de datos transaccional")
        raise HTTPException(status_code=500, detail="Database deadlock timeout")
    return {"status": "HEALTHY"}