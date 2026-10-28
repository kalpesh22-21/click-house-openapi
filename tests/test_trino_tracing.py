import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app.config import Settings
from app.errors import QueryValidationError
from app import trino_tracing as tracing
from app.trino_diagnostics import trace_conversion, trace_query, record_query_id
from app.principal import current_session_id


@pytest.fixture
def telemetry(monkeypatch, tmp_path):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, '_tracer', lambda settings: provider.get_tracer('test'))
    settings = Settings(_env_file=None, trino_enabled=True, trino_host="localhost", otlp_endpoint='http://unused/v1/traces',
                        trino_diagnostics_dir=str(tmp_path))
    yield settings, exporter
    provider.shutdown()


def test_conversion_is_child_and_sql_literals_are_redacted(telemetry):
    settings, exporter = telemetry
    @trace_conversion
    def convert(sql, settings):
        return "SELECT * FROM employee WHERE clientcode = 'private-client' AND n = 42"
    token = current_session_id.set('session-test')
    try:
        with trace_query("SELECT 42", settings):
            convert("SELECT 'private-client', 42", settings)
            record_query_id('20261028_test')
    finally:
        current_session_id.reset(token)
    child, parent = exporter.get_finished_spans()
    assert child.parent.span_id == parent.context.span_id
    assert child.name == 'trino.convert'
    assert parent.attributes['trino.query_id'] == '20261028_test'
    assert child.attributes['session.id'] == 'session-test'
    assert 'private-client' not in str(child.attributes)
    assert '\nFROM employee\nWHERE' in child.attributes['output.value']
    assert '42' not in child.attributes['input.value']
    assert child.status.status_code == StatusCode.OK


def test_failure_has_code_without_exception_content(telemetry):
    settings, exporter = telemetry
    @trace_conversion
    def convert(sql, settings):
        raise QueryValidationError('SECRET SQL AND TOKEN', 'SQL_UNSUPPORTED_CONSTRUCT')
    with pytest.raises(QueryValidationError):
        convert('SELECT 1', settings)
    span, = exporter.get_finished_spans()
    assert span.attributes['error.code'] == 'SQL_UNSUPPORTED_CONSTRUCT'
    assert span.status.status_code == StatusCode.ERROR
    assert not span.events
    assert 'SECRET' not in str(span.attributes)
    assert span.status.description is None


def test_disabled_and_explicit_unredacted_modes(telemetry):
    settings, exporter = telemetry
    @trace_conversion
    def convert(sql, settings):
        return sql
    settings.otlp_endpoint = ''
    assert convert('SELECT 42', settings) == 'SELECT 42'
    assert not exporter.get_finished_spans()
    settings.otlp_endpoint = 'http://unused/v1/traces'
    settings.otlp_disable_redaction = True
    convert('SELECT 42', settings)
    assert exporter.get_finished_spans()[0].attributes['input.value'] == 'SELECT 42'


def test_tracing_initialization_failure_does_not_block_conversion(telemetry, monkeypatch):
    settings, exporter = telemetry
    def unavailable(settings):
        raise RuntimeError('collector setup failure')
    monkeypatch.setattr(tracing, '_tracer', unavailable)
    @trace_conversion
    def convert(sql, settings):
        return sql
    assert convert('SELECT 1', settings) == 'SELECT 1'


def test_incoming_trace_context_isolated_and_restored(telemetry):
    import asyncio
    from opentelemetry import trace
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
    settings, exporter = telemetry
    provider = TracerProvider()
    tracer = provider.get_tracer('agent')
    async def exercise():
        async def inner(scope, receive, send):
            await asyncio.sleep(0)
            with tracing.trino_span('trino.convert', settings):
                await asyncio.sleep(0)
        middleware = tracing.TraceContextMiddleware(inner)
        async def request():
            with tracer.start_as_current_span('agent.tool') as parent:
                carrier = {}
                TraceContextTextMapPropagator().inject(carrier)
                await middleware({'type':'http', 'headers':[(k.encode(),v.encode()) for k,v in carrier.items()]}, None, None)
                assert trace.get_current_span() is parent
                return parent.get_span_context().span_id
        parents = await asyncio.gather(request(), request())
        assert {s.parent.span_id for s in exporter.get_finished_spans()} == set(parents)
        assert not trace.get_current_span().get_span_context().is_valid
        await middleware({'type':'http','headers':[(b'traceparent',b'invalid')]}, None, None)
        assert exporter.get_finished_spans()[-1].parent is None
    asyncio.run(exercise())
    provider.shutdown()
