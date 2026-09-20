import os
import httpx
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
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

# 1. Variables de entorno de conexión
LEDGER_SERVICE_URL = os.getenv("LEDGER_SERVICE_URL", "http://wallet-ledger:8000")
OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger-collector:4317")

# 2. OpenTelemetry Tracer y Batch Processor
resource = Resource.create({"service.name": "transfer-api"})
provider = TracerProvider(resource=resource)
processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=OTEL_ENDPOINT, insecure=True))
provider.add_span_processor(processor)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("transfer-api")

# 3. Correlación de logs estructurados en JSON
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

# 4. Inicialización de la API e Instrumentaciones
app = FastAPI(title="Transfer API Gateway")
FastAPIInstrumentor.instrument_app(app)
HTTPXClientInstrumentor().instrument()
Instrumentator().instrument(app).expose(app)

class CreateTransferRequest(BaseModel):
    account_id: str
    amount: float
    currency: str = "USD"

@app.post("/transfers")
async def initiate_transfer(req: CreateTransferRequest):
    logger.info("Recibida intencion de transferencia", account=req.account_id, amount=req.amount)

    # Llamada al microservicio de cuenta/saldo downstream
    async with httpx.AsyncClient() as client:
        try:
            # httpx inyecta traceparent en los headers automáticamente gracias al instrumentador
            response = await client.post(
                f"{LEDGER_SERVICE_URL}/debit",
                json=req.model_dump(),
                timeout=5.0
            )
            response.raise_for_status()
            ledger_data = response.json()

            logger.info("Transferencia confirmada por el ledger", account=req.account_id, status="SUCCESS")
            return {
                "message": "Transferencia procesada con exito",
                "receipt": ledger_data
            }

        except httpx.HTTPStatusError as exc:
            logger.warning("Downstream respondio con error de negocio", status_code=exc.response.status_code)
            raise HTTPException(status_code=exc.response.status_code, detail=exc.response.json().get("detail"))
        except httpx.RequestError as exc:
            logger.error("Error de comunicacion con wallet-ledger", error=str(exc))
            raise HTTPException(status_code=503, detail="Servicio de saldo temporalmente no disponible")

@app.get("/incident/trigger")
async def trigger_incident(delay: float = 0.0, force_500: bool = False):
    """Llama al endpoint de falla del ledger para simular incidentes en cascada."""
    logger.warning("Propagando simulacion de incidente hacia el backend", delay=delay, force_500=force_500)
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{LEDGER_SERVICE_URL}/simulate-failure?delay={delay}&force_500={force_500}",
            timeout=10.0
        )
        response.raise_for_status()
        return response.json()