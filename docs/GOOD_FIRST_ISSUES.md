# Good first issues (draft for maintainers)

Concrete, scoped tasks discovered in the codebase. Turn these into GitHub issues with labels such as `good first issue`.

---

### 1. Make crawler `OUT_DIR` configurable for local tests

**Files:** `src/crawler/crawl.py`, `tests/test_crawl_guard.py`

**Problem:** Importing `crawl.py` calls `os.makedirs("/opt/output", ...)` at module load, which breaks `pytest` on machines without that path.

**Acceptance criteria:**

- `SCREENWEAVE_OUT_DIR` (or similar) defaults to `/opt/output` on EC2
- `test_crawl_guard.py` passes without `sudo` on a fresh checkout
- README/CONTRIBUTING mention the env var

---

### 2. Document `generate_apple_video` in the README MCP section

**Files:** `README.md`, `src/lambda/mcpServer/videoJob.mjs`

**Problem:** Architecture docs list `generate_apple_video`; the README MCP tools table does not.

**Acceptance criteria:**

- README documents parameters (`url` or `session_id`, `duration`, `scale`) and polling via `get_session_status`
- Links to `docs/swarm-to-video.md` if that doc stays canonical

---

### 3. Fix Pillow `Image.getdata` deprecation warnings

**Files:** `src/lambda/visualQAWorker/cache.py`, `image_classifier.py`

**Problem:** Tests emit `DeprecationWarning` for `getdata()` (Pillow 14 removal).

**Acceptance criteria:**

- Use `get_flattened_data()` or equivalent
- `tests/test_visualqa_images.py` passes with zero deprecation warnings

---

### 4. Add SAM `MaxConcurrentCrawlers` to architecture doc table

**Files:** `docs/architecture.md`, `infra/main-stack.yaml`

**Problem:** `MAX_CONCURRENT_CRAWLERS` is documented in code comments but not in the architecture parameters section.

**Acceptance criteria:**

- Table row with default `3` and behavior summary

---

### 5. Align `examples/response.json` with MCP `get_full_session` shape

**Files:** `examples/response.json`, `src/lambda/mcpServer/index.mjs`

**Problem:** Example comment references `GET /session/...` which is not an exposed route; MCP uses `get_full_session`.

**Acceptance criteria:**

- JSON structure matches a real `tools/call` result (text + embedded JSON content)
- Comment points to `examples/mcp-jsonrpc.md`

---

### 6. Expose Visual QA batch size as a template parameter

**Files:** `infra/main-stack.yaml`, `src/lambda/visualQAWorker/handler.py`

**Problem:** Batch size `10` is hard-coded in the worker.

**Acceptance criteria:**

- Env var with default 10; documented in `docs/architecture.md`
- Validation rejects unreasonable values

---

### 7. Contract conformance CLI wrapper

**Files:** `contracts/conformance.mjs`, `scripts/`, `CONTRIBUTING.md`

**Problem:** Conformance logic exists but is only exercised via observatory unit tests.

**Acceptance criteria:**

- `node scripts/check-observatory-contract.mjs` loads a sample item and exits 0
- Documented in CONTRIBUTING

---

### 8. Crawler: configurable user-agent string

**Files:** `src/crawler/crawl.py`, `infra/main-stack.yaml`

**Problem:** Fixed Playwright context; some sites block default UA.

**Acceptance criteria:**

- Optional env `SCREENWEAVE_USER_AGENT`
- Documented; default unchanged

---

### 9. MCP server: integration test fixture for `tools/list` schema stability

**Files:** `src/lambda/mcpServer/test/`

**Problem:** Tool JSON Schemas are hand-written in `index.mjs`; accidental drift is easy.

**Acceptance criteria:**

- Snapshot or invariant test that every tool in `TOOL_DEFINITIONS` has `name`, `description`, `inputSchema`
- CI green

---

### 10. Cost doc: link image tier thresholds to env vars

**Files:** `docs/cost-calculation.md`, `src/lambda/visualQAWorker/image_classifier.py`

**Problem:** Thresholds are constants in Python; cost doc may not match code.

**Acceptance criteria:**

- Single table of `HEURISTIC_THRESHOLD`, `SONNET_THRESHOLD`, model IDs
- Note that changing thresholds requires code change today (or issue for parameterization)
