# Tests

The headless unit suite for `renderdoc_graph_viewer`. It runs without a GPU,
RenderDoc, or PySide2 — `import renderdoc` is satisfied by a fake (see below),
and the few Qt tests skip when PySide2 is absent.

## Running

```
python -m pytest -q            # the whole headless suite (what CI runs)
python -m pytest tests/test_scoped.py -q
python -m tests.test_scoped    # one file standalone (uses the unittest.main footer)
```

CI runs `python -m pytest -q` on Python 3.8 / 3.10 / 3.12 (`.github/workflows/ci.yml`).

## Conventions

New test files follow the existing ones:

1. `# -*- coding: utf-8 -*-` first line, then a module docstring naming the
   module under test and what it covers.
2. Tests are `unittest.TestCase` subclasses; methods `test_<behavior>`, classes
   carry a docstring describing the contract.
3. Assertions use `self.assertEqual / assertTrue / assertIn / assertRaises …`,
   not bare `assert`. Exceptions: `with self.assertRaises(X):`.
4. All imports at the top of the file; no mid-file imports.
5. End with:
   ```python
   if __name__ == '__main__':
       unittest.main()
   ```

## Fixtures

| File | Role |
|------|------|
| `conftest.py` | Installs the fake `renderdoc` module once, before any test imports a module that does `import renderdoc`. |
| `rd_stub.py` | The fake `renderdoc` module (ResourceId, TextureSave, category enums, …). A file that needs an `rd` handle, or that must run standalone via `python -m tests.test_x` (no conftest), keeps `rd = rd_stub.install()` at the top — `install()` is idempotent. |
| `fakes.py` | Concise `LeafAction` IR builders (`draw`/`dispatch`/`clear`/`transfer`/`present`) plus `FakeRD`/`FakeAction` for exercising `_collect_leaves`. |

Small, file-local fake objects (e.g. a minimal `SDObject`) stay in the file that
uses them; shared IR/stub helpers belong in `fakes.py` / `rd_stub.py`.

## Headless suite vs the GPU end-to-end harness

`tests/e2e/` is a separate, YAML-driven end-to-end harness: it renders each
scene on a real GPU through all three APIs (Vulkan / D3D11 / D3D12), captures the
frame with RenderDoc, parses it with the viewer, and checks the parsed graph is
isomorphic to an independent oracle. See `tests/e2e/README.md`.

**`pytest` does not collect `tests/e2e/`** (`pyproject.toml` `norecursedirs`).
Only its host-side logic is exercised headless, via `tests/test_harness.py`
(schema, HLSL-text codegen, manifest, and the oracle fed a *synthetic* bundle —
which drives the real graph-assembly / portal / bundle / versioning code).

So **CI does not exercise real-capture parsing**: `extract_bundle` on a live
controller and real SPIR-V/DXBC/DXIL bytecode parsing run only when a maintainer
runs `python tests/e2e/run_all.py` locally (needs Windows + GPU + RenderDoc
+ a shader compiler). A green CI run means the pure logic is correct; the
real-capture path is validated out-of-band.
