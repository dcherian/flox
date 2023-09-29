import pytest


@pytest.fixture(scope="module", params=[None, "flox", "numpy", "numba"])
def engine(request):
    if request.param == "numba":
        try:
            import numba  # noqa
        except ImportError:
            pytest.xfail()
    return request.param
