import pytest

pytest_plugins = ('anyio',)

# Run anyio-marked tests with asyncio only (skip trio).
@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param
