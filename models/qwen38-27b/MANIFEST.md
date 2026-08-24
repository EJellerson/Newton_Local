# Canonical Model Artifacts — Qwen-A

The weights themselves are **not** committed (see `.gitignore`). This manifest
is the provenance record for the exact artifacts installed on this machine.

## Source

| Field | Value |
|---|---|
| Model | Qwen 3.8 27B — Q8_0 GGUF + F16 vision projector |
| Download source | Hugging Face; repository and revision in `config/model-sources.local.json` (machine-local, untracked) |
| Downloaded on | 2026-08-24 |
| License | apache-2.0 |

## Artifacts

### `Qwen3.8-27B-Q8_0.gguf`

| Field | Value |
|---|---|
| Local path | `/Users/ericjellerson/Newton_Local/models/qwen38-27b/Qwen3.8-27B-Q8_0.gguf` |
| Size | **29,047,084,416 bytes** (29.05 GB / 27.05 GiB) |
| **SHA-256** | `31756fca94beca71ea4b8706d6fdc896dab2a3c6376ab0c1863b98512a24f8d6` |
| GGUF version | 3 |
| Tensors | 866 |
| `general.architecture` | `qwen35` |
| `general.file_type` | `7` = **Q8_0** |
| `general.size_label` | 27B |
| `qwen35.context_length` | **262144** |
| `qwen35.block_count` | 65 |
| `qwen35.embedding_length` | 5120 |
| `qwen35.attention.head_count` / `_kv` | 24 / 4 |
| `qwen35.attention.key_length` / `value_length` | 256 / 256 |
| `qwen35.nextn_predict_layers` | 1 (MTP) |
| `qwen35.full_attention_interval` | 4 (hybrid: 1-in-4 layers full attention) |
| `qwen35.ssm.state_size` / `conv_kernel` / `group_count` | 128 / 4 / 16 (gated delta net) |
| `qwen35.rope.freq_base` | 10,000,000 |

### `mmproj-Qwen3.8-27B-f16.gguf`

| Field | Value |
|---|---|
| Local path | `/Users/ericjellerson/Newton_Local/models/qwen38-27b/mmproj-Qwen3.8-27B-f16.gguf` |
| Size | **931,145,984 bytes** (0.93 GB) |
| **SHA-256** | `add205b7bfdb3f71f6da36b0a82aa20928dd829a920878c602628cdfbebc5288` |
| `general.architecture` | `clip` |
| `general.type` | `mmproj` |
| `general.file_type` | `32` = F16 |
| `clip.projector_type` | `qwen3vl_merger` |
| `clip.has_vision_encoder` | true |
| `clip.vision.image_size` | 768 |
| `clip.vision.patch_size` | 16 |
| `clip.vision.block_count` | 27 |
| `clip.vision.embedding_length` | 1152 |
| `clip.vision.projection_dim` | **5120** |

**Projector/model pairing proof:** the projector's
`clip.vision.projection_dim = 5120` equals the main model's
`qwen35.embedding_length = 5120`, and both files share
`general.basename = Qwen3.8` and the same model lineage.
This is the matching pair, not an approximate substitute.

## Integrity verification

Both files were verified two independent ways:

1. **Byte-length** against the Hugging Face tree API — exact match.
2. **SHA-256** computed locally with `shasum -a 256` and compared to the
   `lfs.oid` published by the Hugging Face API — exact match on both files.

```
31756fca94beca71ea4b8706d6fdc896dab2a3c6376ab0c1863b98512a24f8d6  Qwen3.8-27B-Q8_0.gguf
add205b7bfdb3f71f6da36b0a82aa20928dd829a920878c602628cdfbebc5288  mmproj-Qwen3.8-27B-f16.gguf
```

## Single-copy guarantee

Exactly **one** copy of the Q8_0 weights exists on this machine.

- Downloaded with `hf download --local-dir`, which on `huggingface_hub` 1.x
  writes directly into the destination — no second cache copy.
- `HF_HUB_CACHE` was additionally redirected to
  `~/Newton_Local/state/hf-cache` so nothing could land in the user's home
  cache. That directory finished at **0 B**.
- `~/.cache/huggingface/hub` measured **4.0 KB** after the download.
- A filesystem-wide search found no `.gguf` outside this directory other
  than llama.cpp's own kilobyte-sized vocabulary test fixtures.
- Residual staging metadata in `models/qwen38-27b/.cache` is 12 KB.
