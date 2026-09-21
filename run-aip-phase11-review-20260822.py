from dataclasses import replace
from pathlib import Path

from dual_codex.codex import run_codex_app_server
from dual_codex.config import load_config

root = Path(r"C:\Users\bielx\3D Objects\Dual-Codex-worktree")
repository = root / "aip-phase10-checkout"
config = replace(
    load_config(root / "config-fast.toml"),
    repository=repository,
    runs_dir=root / "runs-review-phase11",
    require_clean_git=False,
)
agent = replace(config.agent_for_role("reviewer"), reasoning_effort="low")
output = root / "aip-phase11-review-20260822.result.json"
result = run_codex_app_server(
    config=config,
    agent=agent,
    repository=repository,
    prompt=(root / "aip-phase11-review-20260822.md").read_text(encoding="utf-8"),
    output_path=output,
    session_id="biel4-review-phase11-f2b2f84",
    request_id="aip-phase11-review-final-20260822",
    run_id="aip-phase11-review-final-20260822",
    role="reviewer",
)
print({"returncode": result.returncode, "stderr": result.stderr, "metadata": result.metadata})
print(output.read_text(encoding="utf-8") if output.exists() else "NO_OUTPUT")
