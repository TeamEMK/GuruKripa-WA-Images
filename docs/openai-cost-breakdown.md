# OpenAI Cost Breakdown — WA Images (Jewelry Finder)

## Models in Use

| Purpose | Model |
|---|---|
| Image profiling & query analysis | **gpt-4.1** |
| Semantic search embeddings | **text-embedding-3-large** |

---

## Current OpenAI Pricing

| Model | Input | Output |
|---|---|---|
| gpt-4.1 | $2.00 / 1M tokens | $8.00 / 1M tokens |
| text-embedding-3-large | $0.13 / 1M tokens | — (no output tokens) |

---

## Cost Per Operation

### 1. Customer sends an IMAGE query
Triggers `analyze_image_profile` (gpt-4.1, `detail: "high"`) + `embed_text`.

| Component | Tokens | Cost |
|---|---|---|
| PROFILE_PROMPT (system) | ~900 input | — |
| Image at high detail (typical jewelry photo) | ~765–1,100 input | — |
| JSON profile output | ~400 output | — |
| **Total per image query** | ~1,700 in / 400 out | **~$0.0065** |
| embed_text | ~350 input | ~$0.00005 |
| **Total** | | **≈ $0.007** |

### 2. Customer sends a TEXT query
Triggers `analyze_query` (gpt-4.1, text only, max 30 output tokens) + `embed_text`.

| Component | Tokens | Cost |
|---|---|---|
| SYSTEM_PROMPT + user text | ~370 input | — |
| Classification output | ~10 output | — |
| embed_text | ~350 input | ~$0.00005 |
| **Total per text query** | | **≈ $0.0009** |

### 3. Indexing a new image (one-time per image)
Same pipeline as an image query — `analyze_image_profile` + `embed_text`.

| | Cost |
|---|---|
| **Per image indexed** | **≈ $0.007** |

---

## Monthly Cost Estimates

| Scale | Catalog Size | Queries / Day | One-Time Indexing | Monthly Recurring |
|---|---|---|---|---|
| Small | 500 images | 30 image + 50 text | ~$3.50 | **~$8–10** |
| Medium | 2,000 images | 100 image + 100 text | ~$14 | **~$25–30** |
| Large | 5,000 images | 300 image + 200 text | ~$35 | **~$70–80** |

> The dominant cost is **image queries** (~$0.007 each). Text queries are ~8× cheaper.

