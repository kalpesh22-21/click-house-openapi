"""Optional Phoenix/OTLP spans for Trino; never exports result rows or exceptions."""
from contextlib import contextmanager
import logging
import re
import threading

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider, SpanLimits
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

_logger = logging.getLogger(__name__)
_lock = threading.Lock()
_providers = {}


def _tracer(settings):
    key = (settings.otlp_endpoint, settings.otlp_service_name, settings.otlp_project_name)
    with _lock:
        if key not in _providers:
            provider = TracerProvider(resource=Resource.create({
                'service.name': key[1], 'openinference.project.name': key[2],
            }), span_limits=SpanLimits(max_attribute_length=32768))
            provider.add_span_processor(BatchSpanProcessor(
                OTLPSpanExporter(endpoint=key[0], timeout=5),
                max_queue_size=512, max_export_batch_size=64,
            ))
            _providers[key] = provider
        return _providers[key].get_tracer(__name__)


def sql_attribute(sql, dialect, settings):
    from app.trino_diagnostics import redacted_sql
    return (sql if settings.otlp_disable_redaction else redacted_sql(sql, dialect, pretty=True))[:32768]


@contextmanager
def trino_span(name, settings, **attributes):
    if not settings.otlp_endpoint:
        yield trace.INVALID_SPAN
        return
    try:
        tracer = _tracer(settings)
    except Exception:
        _logger.warning('Trino tracing initialization unavailable')
        yield trace.INVALID_SPAN
        return
    # Exception messages/stacks may contain SQL, credentials or backend responses.
    with tracer.start_as_current_span(name, record_exception=False,
                                      set_status_on_exception=False) as span:
        span.set_attribute('openinference.span.kind', 'CHAIN')
        span.set_attribute('db.system', 'trino')
        span.set_attributes(attributes)
        from app.principal import current_session_id
        session = current_session_id.get()
        if session:
            span.set_attribute('session.id', session)
        try:
            yield span
        except Exception as exc:
            code = getattr(exc, 'code', 'INTERNAL_ERROR')
            if not isinstance(code, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}', code):
                code = 'INTERNAL_ERROR'
            span.set_attribute('error.code', code)
            span.set_attribute('error.type', type(exc).__name__)
            span.set_status(trace.StatusCode.ERROR)
            raise
        else:
            span.set_status(trace.StatusCode.OK)


class TraceContextMiddleware:
    """Continue W3C traces only; trace headers never supply identity or permissions."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from opentelemetry.context import attach, detach, Context
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
        carrier = {key.decode("latin1"): value.decode("latin1")
                   for key, value in scope.get("headers", [])
                   if key in (b"traceparent", b"tracestate")}
        token = attach(TraceContextTextMapPropagator().extract(carrier, context=Context()))
        try:
            return await self.app(scope, receive, send)
        finally:
            detach(token)
