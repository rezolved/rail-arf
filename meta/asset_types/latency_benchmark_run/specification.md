# Latency Benchmark Run Asset Specification

**Version**: 2

* * *

## Purpose

One execution of the Vorontsov latency harness against a specific endpoint — either an external
provider or a local vLLM/SGLang/TRT-LLM config on the 2x H100 VM. Captures per-request raw
latencies, time-to-first-token, and decode throughput. Persisted before aggregation so that
percentiles can be recomputed and warm-up windows reconsidered without re-running the benchmark.

## Producer

* `provider-benchmark` tasks (one run per external provider).
* `baseline-evaluation` tasks (the t0003 in-house baseline replication).
* `serving-config-experiment` tasks (one run per candidate config; sometimes paired runs across time
  windows).

## Consumers

* Aggregators that compute latency and TTFT percentiles across providers and configs.
* The cross-task ranking aggregator that posts the latency/accuracy table to LLM-577 — joins on
  `vllm_config_ref` to pair each run with its serving config.
* Human reviewers auditing time-window choice, concurrency, and warm-up handling.
* `chat-template-audit`-tagged tasks that correlate latency anomalies with payload patterns.

## Asset Folder Structure

```text
tasks/<task_id>/assets/latency_benchmark_run/<run_id>/
├── details.json         # Structured metadata (required)
├── raw_requests.jsonl   # One JSON object per request (required)
├── summary.json         # Computed percentiles for the run (required)
└── sample_payloads/     # Captured request/response payloads for spot audits (optional)
```

## Asset ID

The naming convention for `<run_id>` is `<endpoint>_<yyyymmdd>_<hhmm>_<suffix>`, for example
`nebius_fast_20260520_1430_a` or `vllm_fp8w8a8_tp2_v1_20260601_0930_a`. The folder name must match
`details.json` `run_id` exactly.

## Fields

`details.json` uses `snake_case` field names. All fields are required unless marked optional.

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `spec_version` | string | yes | Specification version (currently `"1"`) |
| `run_id` | string | yes | Asset ID; must match the folder name |
| `endpoint_kind` | string | yes | One of `external_provider`, `local_vllm_config` |
| `endpoint_label` | string | yes | Short human label (e.g., `nebius_fast`, `vllm_fp8w8a8_tp2_v1`) |
| `vllm_config_ref` | object \| null | yes | Required when `endpoint_kind == "local_vllm_config"`; must be `null` for external providers. See below |
| `harness` | string | yes | Harness slug (e.g., `vorontsov-latency-harness`) |
| `harness_version` | string | yes | Pinned harness version (semver or git SHA) |
| `concurrency` | int | yes | Number of in-flight requests during the run; must be ≥ 1 |
| `duration_seconds` | float \| null | yes | Wall-clock window length in fractional seconds; `null` if the harness ran fixed-count |
| `warmup_seconds` | float \| null | yes | Discarded warm-up window in fractional seconds; `null` if no warm-up was performed |
| `warmup_requests` | int \| null | yes (v2+) | Discarded warm-up request count; `null` if no warm-up was performed. Required when the task uses the `warmup_runner` protocol (see `arf/scripts/protocols/warmup_runner/README.md`) |
| `warmup_corpus_ref` | string \| null | yes (v2+) | Stable reference to the warm-up prompt corpus (dataset asset ID, HF repo, or pinned URL); `null` when no warm-up was performed |
| `engine_version` | string \| null | yes (v2+) | Engine version captured at benchmark time (e.g., `vllm==0.19.1`, `sglang==0.4.6`, `tensorrt_llm==0.20.0`); `null` only for external endpoints that do not expose `/version` |
| `cuda_version` | string \| null | yes (v2+) | CUDA toolkit version on the measurement host (e.g., `12.8`); `null` for external endpoints |
| `cudnn_version` | string \| null | yes (v2+) | cuDNN version on the measurement host (e.g., `9.4.0`); `null` for external endpoints |
| `container_image_sha` | string \| null | yes (v2+) | Container image SHA for hosted endpoints (e.g., the value from `kubectl get deployment ... -o jsonpath='{...image}'`); `null` for local VM runs |
| `prompt_dataset_ref` | string \| null | yes | Identifier for the prompt corpus used (e.g., a dataset asset id or HF repo); `null` when the harness uses built-in prompts |
| `start_time_utc` | string | yes | ISO 8601 datetime (`YYYY-MM-DDTHH:MM:SSZ`) |
| `end_time_utc` | string | yes | ISO 8601 datetime; must be ≥ `start_time_utc` |
| `total_requests` | int | yes | Number of requests issued; must be ≥ 0 |
| `successful_requests` | int | yes | Number of requests with `status == "success"`; must satisfy `0 ≤ successful_requests ≤ total_requests` |
| `summary_path` | string | yes | Relative path to the summary file (default: `summary.json`) |
| `raw_requests_path` | string | yes | Relative path to the per-request JSONL file (default: `raw_requests.jsonl`) |
| `categories` | list[string] | yes | Category slugs from `meta/categories/`; may be empty |
| `added_by_task` | string | yes | Task ID that first added this asset |
| `date_added` | string | yes | ISO 8601 date (`YYYY-MM-DD`) when the asset was added |

### `vllm_config_ref` object

```text
{
  "task_id": "<producing task id of the vllm_config asset>",
  "config_id": "<vllm_config asset id>"
}
```

Both fields are strings and both are required when the object is present. The pair must point at an
existing `vllm_config` asset folder at `tasks/<task_id>/assets/vllm_config/<config_id>/`.

### `summary.json` schema

```text
{
  "latency_avg_seconds": float,
  "latency_p50_seconds": float,
  "latency_p95_seconds": float,
  "latency_p99_seconds": float,
  "ttft_median_seconds": float | null,
  "ttft_p95_seconds": float | null,
  "ttft_p99_seconds": float | null,
  "tokens_per_second": float,
  "input_tokens_total": int,
  "output_tokens_total": int
}
```

All latency and TTFT values are fractional seconds (floats). Percentiles must satisfy
`p50 ≤ p95 ≤ p99` for both latency and TTFT (when TTFT is non-null). TTFT fields may be `null` when
the harness or endpoint does not report streaming first-token timing. `input_tokens_total` and
`output_tokens_total` are non-negative integers.

### `raw_requests.jsonl` per-line schema

Each line is a single JSON object with the following fields:

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `prompt_id` | string | yes | Stable identifier of the prompt within the run |
| `submit_time` | string | yes | ISO 8601 datetime when the request was issued |
| `first_token_time` | string \| null | yes | ISO 8601 datetime when the first token was received; `null` for non-streaming endpoints or failed requests |
| `completion_time` | string \| null | yes | ISO 8601 datetime when the response completed; `null` for failed requests |
| `input_token_count` | int | yes | Token count of the request prompt |
| `output_token_count` | int | yes | Token count of the response; `0` for failed requests |
| `status` | string | yes | One of `success`, `failure` |

The number of non-empty JSONL lines must equal `details.json` `total_requests` (a warning is raised
otherwise).

## Verification Rules

### Errors

| Code | Description |
| --- | --- |
| `LBR-E001` | `details.json` is missing or not valid JSON |
| `LBR-E002` | `summary.json` (or the path declared in `summary_path`) is missing or not valid JSON |
| `LBR-E003` | `raw_requests.jsonl` (or the path declared in `raw_requests_path`) is missing |
| `LBR-E004` | `run_id` in `details.json` does not match the folder name |
| `LBR-E005` | Required field missing in `details.json` |
| `LBR-E006` | `spec_version` is missing from `details.json` or `summary.json` |
| `LBR-E007` | `endpoint_kind == "local_vllm_config"` but `vllm_config_ref` is `null` or missing fields |
| `LBR-E008` | `endpoint_kind == "external_provider"` but `vllm_config_ref` is not `null` |
| `LBR-E009` | `endpoint_kind` is not one of the allowed values |
| `LBR-E010` | Latency percentiles in `summary.json` are not monotonic (`p50 ≤ p95 ≤ p99`) |
| `LBR-E011` | `successful_requests > total_requests` |
| `LBR-E012` | `vllm_config_ref` points at a task or config that does not exist |
| `LBR-E013` | `concurrency` is not a positive integer |
| `LBR-E014` | `end_time_utc < start_time_utc` |
| `LBR-E015` | Required summary field missing or wrong type |
| `LBR-E016` | TTFT percentiles in `summary.json` are not monotonic when non-null |

### Warnings

| Code | Description |
| --- | --- |
| `LBR-W001` | `raw_requests.jsonl` line count does not equal `total_requests` |
| `LBR-W002` | `ttft_median_seconds` is `null` while `output_tokens_total > 0` |
| `LBR-W003` | `warmup_seconds` is `null` (warm-up handling should be explicit) |
| `LBR-W004` | A category in `details.json` does not exist in `meta/categories/` |
| `LBR-W005` | `successful_requests / total_requests < 0.95` (more than 5% of requests failed) |
| `LBR-W006` | `prompt_dataset_ref` is `null` (run is not reproducible from a recorded prompt corpus) |
| `LBR-W007` | At least one `raw_requests.jsonl` line is malformed JSON (count surfaced in the message) |
| `LBR-W101` | `warmup_requests` is missing or null (LESSONS.md Lesson 1; enforced by `verify_latency_benchmark_run_warmup`) |
| `LBR-W102` | `warmup_requests < 1` (warmup phase did not run a real request) |
| `LBR-W103` | `warmup_corpus_ref` is missing or empty (warm-up corpus is not reproducible) |
| `LBR-W104` | Any of `engine_version`/`cuda_version`/`cudnn_version`/`container_image_sha` is null for a non-external endpoint (LESSONS.md Lesson 4; infrastructure-version capture) |

* * *

## Complete Example

### `details.json`

```json
{
  "spec_version": "1",
  "run_id": "vllm_fp8w8a8_tp2_v1_20260601_0930_a",
  "endpoint_kind": "local_vllm_config",
  "endpoint_label": "vllm_fp8w8a8_tp2_v1",
  "vllm_config_ref": {
    "task_id": "t0004_vllm_fp8_baseline",
    "config_id": "vllm_fp8w8a8_tp2_v1"
  },
  "harness": "vorontsov-latency-harness",
  "harness_version": "0.4.1",
  "concurrency": 16,
  "duration_seconds": 600.0,
  "warmup_seconds": 60.0,
  "prompt_dataset_ref": "rail_internal_toolcalling_eval_v2",
  "start_time_utc": "2026-06-01T09:30:00Z",
  "end_time_utc": "2026-06-01T09:40:00Z",
  "total_requests": 1280,
  "successful_requests": 1278,
  "summary_path": "summary.json",
  "raw_requests_path": "raw_requests.jsonl",
  "categories": [
    "latency-benchmark",
    "fp8-quantization"
  ],
  "added_by_task": "t0004_vllm_fp8_baseline",
  "date_added": "2026-06-01"
}
```

### `summary.json`

```json
{
  "spec_version": "1",
  "latency_avg_seconds": 1.842,
  "latency_p50_seconds": 1.611,
  "latency_p95_seconds": 3.214,
  "latency_p99_seconds": 4.875,
  "ttft_median_seconds": 0.184,
  "ttft_p95_seconds": 0.523,
  "ttft_p99_seconds": 0.812,
  "tokens_per_second": 73.4,
  "input_tokens_total": 412160,
  "output_tokens_total": 211968
}
```
