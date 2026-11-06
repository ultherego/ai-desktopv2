"""Shared finite budgets for native client registration and runtime capture.

The official Codex 0.167.0 Linux x86_64 standalone bundle contains 540,530,246
unpacked bytes, including its executable, code-mode/exec-server helpers and
voice resources. The former 512 MiB total rejected that supported installer.
Keep the per-file/structure bounds and allow growth of the complete bundle.
"""

MAX_FILE_BYTES = 384 * 1024 * 1024
MAX_TOTAL_BYTES = 768 * 1024 * 1024
MAX_ENTRIES = 4096
MAX_DEPTH = 24

# A sequential native child materializes a tmpfs copy while its supervisor
# retains the sealed snapshot. The service must also budget native/process
# memory; these values are checked against the installed systemd units.
PAYLOAD_MEMORY_COPIES = 2
MIN_NATIVE_MEMORY_BYTES = 1536 * 1024 * 1024
SERVICE_MEMORY_BYTES = 3 * 1024 * 1024 * 1024
