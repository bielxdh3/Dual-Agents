from dataclasses import replace
from pathlib import Path

from dual_codex.codex import run_codex_app_server
from dual_codex.config import load_config

root = Path(r"C:\Users\bielx\3D Objects\Dual-Codex-worktree")
config = replace(load_config(root / "config.toml"), repository=Path(r"E:\AIP"), runs_dir=root / "runs-review-core", require_clean_git=False)
agent = config.agent_for_role("reviewer")
output = root / "aip-pr6-review-core-20260820.result.json"
result = run_codex_app_server(config=config, agent=agent, repository=Path(r"E:\AIP"), prompt=(root / "aip-pr6-review-core-prompt-20260820.md").read_text(encoding="utf-8"), output_path=output, session_id="biel4-review-core-bf6d332", request_id="aip-pr6-review-core-20260820", run_id="aip-pr6-review-core-20260820", role="reviewer")
print({"returncode": result.returncode, "stderr": result.stderr, "metadata": result.metadata})
print(output.read_text(encoding="utf-8") if output.exists() else "NO_OUTPUT")
