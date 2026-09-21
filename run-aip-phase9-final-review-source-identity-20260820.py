from dataclasses import replace
from pathlib import Path

from dual_codex.codex import run_codex_app_server
from dual_codex.config import load_config

root = Path(r"C:\Users\bielx\3D Objects\Dual-Codex-worktree")
config = replace(
    load_config(root / "config-fast.toml"),
    repository=Path(r"E:\AIP"),
    runs_dir=root / "runs-review-phase9-source-identity",
    require_clean_git=False,
)
agent = replace(config.agent_for_role("reviewer"), reasoning_effort="low")
output = root / "aip-phase9-final-review-source-identity-result-20260820.json"
result = run_codex_app_server(
    config=config,
    agent=agent,
    repository=Path(r"E:\AIP"),
    prompt=(root / "aip-phase9-final-review-source-identity-20260820.md").read_text(encoding="utf-8"),
    output_path=output,
    session_id="biel4-review-phase9-source-identity-5864d52",
    request_id="aip-phase9-final-review-source-identity-20260820",
    run_id="aip-phase9-final-review-source-identity-20260820",
    role="reviewer",
)
print({"returncode": result.returncode, "stderr": result.stderr, "metadata": result.metadata})
print(output.read_text(encoding="utf-8") if output.exists() else "NO_OUTPUT")
