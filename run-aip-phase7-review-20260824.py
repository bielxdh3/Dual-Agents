from dataclasses import replace
from pathlib import Path

from dual_codex.codex import run_codex_app_server
from dual_codex.config import load_config

root = Path(r"C:\Users\bielx\3D Objects\Dual-Codex-worktree")
repository = root / "aip-phase10-checkout"
config = replace(
    load_config(root / "config-fast.toml"),
    repository=repository,
    runs_dir=root / "runs-review-phase7",
    require_clean_git=False,
)
agent = replace(config.agent_for_role("reviewer"), reasoning_effort="low")
output = root / "phase7-review-20260824.result.json"
result = run_codex_app_server(
    config=config,
    agent=agent,
    repository=repository,
    prompt=(root / "phase7-review-20260824.md").read_text(encoding="utf-8"),
    output_path=output,
    session_id="biel4-review-phase7-20260824-01",
    request_id="aip-phase7-review-20260824",
    run_id="aip-phase7-review-20260824",
    role="reviewer",
)
print({"returncode": result.returncode, "stderr": result.stderr, "metadata": result.metadata})
print(output.read_text(encoding="utf-8") if output.exists() else "NO_OUTPUT")
