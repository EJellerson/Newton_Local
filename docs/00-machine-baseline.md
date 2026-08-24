# Newton_Local — Machine Baseline

Captured **2026-08-24**, before any Newton_Local installation work began.
Purpose: record the pre-existing state of the machine so that every later
change is attributable and reversible.

## Hardware

| Property | Value |
|---|---|
| Model | Apple Mac Pro (tower) |
| Chip | Apple M2 Ultra |
| Architecture | `arm64` (Apple Silicon, `hw.optional.arm64 = 1`) |
| CPU cores | 24 physical / 24 logical |
| GPU cores | 76 |
| Unified memory | 137,438,953,472 bytes = 128 GiB |
| Metal support | **Metal 3** (`Apple M2 Ultra`, built-in GPU) |
| Displays | LG 32 FHD (main, 1920x1080@60), VA3209-FHD (1920x1080@75) |

## Operating system

| Property | Value |
|---|---|
| ProductName | macOS |
| ProductVersion | 14.8.5 |
| BuildVersion | 23J423 |
| Kernel | Darwin 23.6.0 (`xnu-10063.141.1.711.7~1/RELEASE_ARM64_T6020`) |
| Hostname | Erics-Mac-Pro.local |

## Storage (before install)

| Filesystem | Size | Used | Avail | Capacity | Mount |
|---|---|---|---|---|---|
| `/dev/disk3s1s1` | 926Gi | 10Gi | 222Gi | 5% | `/` |
| `/dev/disk3s5` | 926Gi | 679Gi | 222Gi | 76% | `/System/Volumes/Data` |

**222 GiB available** — sufficient for the ~27 GiB Q8_0 weights plus the
~0.87 GiB vision projector with wide margin.

## Memory state at baseline

```
PhysMem: 102G used (2657M wired, 0B compressor), 25G unused
vm.swapusage: total = 0.00M  used = 0.00M  free = 0.00M
Pages compressed: 0        Pages decompressed: 0
System-wide memory free percentage: 97%
Load Avg: 2.47, 3.56, 5.23
```

Interpretation: the "102G used" figure is dominated by reclaimable page
cache (`Pages inactive: 4,436,446` x 16 KiB = ~68 GiB reclaimable;
`Pages free: 1,624,155` x 16 KiB = ~25 GiB free). Wired memory was only
2.6 GiB, the compressor was completely idle, and swap was zero. The
machine was under **no meaningful memory pressure**. Largest resident
process was 1.80 GB (a Python process); nothing approached model scale.

## Pre-existing development toolchain

| Tool | Status at baseline | Action taken |
|---|---|---|
| Homebrew | **5.1.15** at `/opt/homebrew/bin/brew` | Used as-is |
| Git | 2.39.5 (Apple Git-154) at `/usr/bin/git` | Used as-is |
| Python | 3.12.2 at `/usr/local/bin/python3` (+ Homebrew 3.11/3.12, system 3.9) | Used as-is; **not modified** |
| Apple clang | 16.0.0 (clang-1600.0.26.6), target `arm64-apple-darwin23.6.0` | Used as-is |
| Command Line Tools | `com.apple.pkg.CLTools_Executables` 16.2.0.0.1.1733547573 at `/Library/Developer/CommandLineTools` | Used as-is (see defect below) |
| Xcode (full) | **Not installed** — CLT only | Not required |
| make | `/usr/bin/make` | Used as-is |
| CMake | **Not installed** | **Installed** `cmake 4.4.2` via Homebrew (hard requirement to build llama.cpp) |
| llama.cpp | **Not installed** — no binaries on PATH, no brew formula, no source tree anywhere under `$HOME` | **Built from source** (see `docs/01-llamacpp.md`) |
| Hugging Face CLI | `huggingface-cli` present, `huggingface_hub` **0.21.4** (old; no `hf` command) | **Left untouched.** An isolated venv was created instead — see below |

### Toolchain defect discovered

`/Library/Developer/CommandLineTools/usr/include/c++/v1/` contains a
**stale, incomplete libc++ header set** — 11 entries dated Oct 2022 /
Mar 2024, missing `array`, `cstddef`, `mutex`, `cstdio`, `clocale`,
`csignal`, `algorithm` and most of the standard library.

Clang searches that directory *first* (`-internal-isystem
/Library/Developer/CommandLineTools/usr/bin/../include/c++/v1`). Because
the directory exists, clang never falls through to the SDK's complete
copy at `.../SDKs/MacOSX15.2.sdk/usr/include/c++/v1/` (183 entries). Every
C++ compile therefore failed with `fatal error: 'array' file not found`.

C compilation was unaffected — only C++ was broken.

**Resolution chosen:** the llama.cpp build passes
`-nostdinc++ -isystem "$(xcrun --show-sdk-path)/usr/include/c++/v1"`,
which bypasses the stale directory for that build only. The Command Line
Tools were deliberately **not** reinstalled, to avoid disturbing unrelated
development infrastructure on a working machine. See
`scripts/build-llamacpp.sh`. This defect is pre-existing and will affect
any other C++ build on this machine.

### Python isolation decision

The system `huggingface_hub` is 0.21.4 — old enough that `--local-dir`
downloads are materialised as symlinks into a second full cache copy,
which would violate the single-canonical-copy requirement. Rather than
upgrading the user's existing Python environment, a dedicated venv was
created at `~/Newton_Local/tools/hf-venv` (stdlib `venv` from
`/usr/bin/python3`). The system Python installations were not modified.

## Network / port state

Port **8080**: free (`lsof -nP -iTCP:8080` returned nothing). The other
listeners present at baseline belonged to unrelated software; none
conflicted with 8080, and all were left untouched.

## Pre-existing inference services

- **No** `llama`, `qwen`, `ollama`, `lmstudio`, or `mlx` processes running.
- **No** GGUF models anywhere on the machine.
- `/Library/LaunchDaemons/` — **empty**. Clean slate for the system daemon.
- `/Library/LaunchAgents/` — empty.
- `~/Library/LaunchAgents/` held only unrelated agents from other software;
  none were modified, stopped, or interfered with.

## Summary

The machine was a clean target for this commission: no competing
inference runtime, no port conflict, no memory pressure, ample disk, and
full Metal 3 support. The only blocking issue was the broken C++ header
path in the Command Line Tools, which was worked around without altering
system state.
