"""A deterministic stand-in for the Polygon API.

It is installed at the **HTTP** boundary (``problems.polygon_api.requests.post``)
rather than by patching ``PolygonAPI`` methods, so the real signing, envelope
parsing and plain-text handling all run in tests.

Payload shapes follow the official Polygon API documentation
(https://docs.google.com/document/d/1mb6CDWpbLQsi7F5UjAdwXdbCpyvSgWSXTJVHl52zZUQ):

* every JSON method answers ``{"status": "OK", "result": ...}``
* ``problem.testInput`` / ``problem.testAnswer`` / ``problem.viewSolution``
  answer with plain text, not JSON
* ``problem.package`` answers with raw zip bytes
* a ``Test`` object has ``index``, ``manual``, ``useInStatements`` and
  optionally ``description``; ``input`` is **absent for generated tests**
"""

import io
import json
import zipfile

PROBLEM_HTML = """<html><body>
<div class="problem-index">A</div>
<div class="title">Probe Sum</div>
<div class="legend">
  <div class="section-title">Problem</div>
  <p>Given two integers <b>a</b> and <b>b</b>, print their sum.</p>
  <p><i>Input:</i> two integers.</p>
</div>
<div class="input-specification">
  <div class="section-title">Input</div>
  <p>The first line contains two integers a and b.</p>
</div>
<div class="output-specification">
  <div class="section-title">Output</div>
  <p>Print a + b.</p>
</div>
<div class="note">
  <div class="section-title">Note</div>
  <p>Values fit in 32-bit signed integers.</p>
</div>
</body></html>
"""

SOLUTION_SOURCE = "#include <iostream>\nint main(){int a,b;std::cin>>a>>b;std::cout<<a+b;}\n"


class FakeResponse:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(self, json_data=None, text=None, content=b"", status_code=200):
        self._json = json_data
        if text is None:
            # Mirror requests: a JSON response's .text is the serialised body.
            text = "" if json_data is None else json.dumps(json_data)
        self.text = text
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(f"{self.status_code} error", response=self)

    def json(self):
        if self._json is None:
            raise json.JSONDecodeError("no json", "", 0)
        return self._json


def make_test(index, sample=False, manual=False, description=None):
    """Build a Polygon ``Test`` object.

    ``input`` is deliberately omitted for generated tests, matching the
    documented API: the listing endpoint does not carry test contents.
    """
    test = {"index": index, "manual": manual, "useInStatements": sample}
    if description is not None:
        test["description"] = description
    if manual:
        test["input"] = f"{index}\n{index}\n"
    return test


def build_package(problem_html=PROBLEM_HTML, revision=42):
    """Return a standard problem package (zip bytes) containing problem.html."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{revision}/problem.html", problem_html)
        zf.writestr(f"{revision}/problem.xml", "<problem><title>Probe Sum</title></problem>")
    return buffer.getvalue()


class PolygonStub:
    """Callable replacement for ``requests.post`` inside problems.polygon_api.

    Attributes:
        calls: list of :class:`RecordedCall` in invocation order.
    """

    def __init__(self, tests=None, problem_html=PROBLEM_HTML, checker="ncmp",
                 solutions=None, contents=None, info=None, package_bytes=None,
                 fail_methods=(), raise_methods=(), status_code=200):
        if not isinstance(raise_methods, dict):
            raise_methods = {name: TimeoutError(f"simulated timeout for {name}")
                             for name in raise_methods}
        self.tests = list(tests) if tests is not None else []
        self.problem_html = problem_html
        self.checker = checker
        self.solutions = solutions if solutions is not None else [
            {"name": "probe.cpp", "tag": "MA", "length": 42, "sourceType": "cpp"},
        ]
        # contents maps (testset, testIndex) -> (input, output); built from
        # `self.tests` when not supplied.
        self.contents = contents
        self.info = info if info is not None else {
            "inputFile": "input.txt", "outputFile": "output.txt",
            "interactive": False, "wellFormed": True,
            "timeLimit": 1000, "memoryLimit": 256,
        }
        self.package_bytes = package_bytes if package_bytes is not None else build_package(problem_html)
        self.fail_methods = set(fail_methods)
        self.raise_methods = raise_methods
        self.status_code = status_code
        self.calls = []

    # -- helpers -----------------------------------------------------------
    def default_contents(self):
        if self.contents is not None:
            return self.contents
        table = {}
        for test in self.tests:
            idx = test["index"]
            table[(idx, "input")] = test.get("input", f"{idx}\n{idx}\n")
            table[(idx, "output")] = f"{sum(int(v) for v in table[(idx, 'input')].split())}\n"
        return table

    def methods_called(self):
        return [c.method for c in self.calls]

    def calls_to(self, method):
        return [c for c in self.calls if c.method == method]

    # -- requests.post replacement ----------------------------------------
    def __call__(self, url, data=None, timeout=None, **kwargs):
        method = url.rsplit("/", 1)[-1]
        params = dict(data or {})
        self.calls.append(RecordedCall(
            method=method,
            # Never retain the signature or the secret; keep only what tests assert on.
            public_params={k: v for k, v in params.items() if k not in ("apiSig",)},
            had_signature=bool(params.get("apiSig")),
            timeout=timeout,
        ))

        if method in self.raise_methods:
            raise self.raise_methods[method] if isinstance(
                self.raise_methods, dict) else TimeoutError(f"simulated timeout for {method}")
        if method in self.fail_methods:
            return FakeResponse(json_data={"status": "FAILED", "comment": f"{method} denied"},
                                status_code=self.status_code)
        if self.status_code >= 400:
            return FakeResponse(json_data={"status": "FAILED", "comment": "boom"},
                                status_code=self.status_code)

        handler = getattr(self, f"_m_{method.replace('.', '_')}", None)
        if handler is None:
            return FakeResponse(json_data={"status": "FAILED",
                                           "comment": f"stub has no handler for {method}"})
        return handler(params)

    # -- JSON methods ------------------------------------------------------
    def _ok(self, result):
        return FakeResponse(json_data={"status": "OK", "result": result})

    def _m_problem_info(self, params):
        return self._ok(dict(self.info))

    def _m_problem_updateWorkingCopy(self, params):
        return self._ok({"revision": 43})

    def _m_problem_checker(self, params):
        return self._ok(self.checker)

    def _m_problem_tests(self, params):
        return self._ok([dict(t) for t in self.tests])

    def _m_problem_solutions(self, params):
        return self._ok([dict(s) for s in self.solutions])

    def _m_problem_packages(self, params):
        return self._ok([{"id": "pkg-1", "revision": 42, "type": "standard",
                          "creationTimeSeconds": 1700000000, "state": "READY"}])

    # -- plain-text methods ------------------------------------------------
    def _m_problem_viewSolution(self, params):
        return FakeResponse(text=SOLUTION_SOURCE)

    def _m_problem_testInput(self, params):
        table = self.default_contents()
        key = (int(params.get("testIndex")), "input")
        return FakeResponse(text=table.get(key, ""))

    def _m_problem_testAnswer(self, params):
        table = self.default_contents()
        key = (int(params.get("testIndex")), "output")
        return FakeResponse(text=table.get(key, ""))

    # -- binary method -----------------------------------------------------
    def _m_problem_package(self, params):
        return FakeResponse(content=self.package_bytes)


class RecordedCall:
    """One HTTP call, recorded without secrets."""

    __slots__ = ("method", "public_params", "had_signature", "timeout")

    def __init__(self, method, public_params, had_signature, timeout):
        self.method = method
        self.public_params = public_params
        self.had_signature = had_signature
        self.timeout = timeout

    def __repr__(self):
        return f"<RecordedCall {self.method} params={self.public_params}>"


def default_tests(sample_count=3, regular_count=12):
    """Sample tests first (index 1..n) then generated tests, Polygon-style."""
    tests = []
    idx = 1
    for _ in range(sample_count):
        tests.append(make_test(idx, sample=True, manual=True,
                               description=f"sample {idx}"))
        idx += 1
    for _ in range(regular_count):
        tests.append(make_test(idx, sample=False, manual=False))
        idx += 1
    return tests