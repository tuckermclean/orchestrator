.PHONY: run-dev stop-dev check-coverage e2e-verify

SESSION := orchestrator

run-dev:
	tmux new-session -d -s $(SESSION) -x 220 -y 50 2>/dev/null || true
	tmux send-keys -t $(SESSION) \
		"uvicorn src.api.main:app --reload --port 8000" Enter
	tmux split-window -h -t $(SESSION)
	tmux send-keys -t $(SESSION) \
		"cd ui && npm run dev" Enter
	tmux select-pane -t $(SESSION):0.0
	tmux attach -t $(SESSION)

stop-dev:
	tmux kill-session -t $(SESSION) 2>/dev/null || true

# Validate coverage_map.yaml — every listed test must resolve to a collected node.
# Exits non-zero if any dangling test names or uncovered rows are detected.
# Mirror of the proposed CI step; run locally before opening a PR.
check-coverage:
	python tools/check_coverage_map.py

# Live end-to-end verification against the deployed k8s orchestrator: opens a real
# issue in the sandbox repo and watches the run pipeline record runs, advance status,
# and stream transcript events; prints the resulting PR. One self-contained command.
# Requires kubectl (cluster context) + gh (authed); run from repo root.
# Override: NS, REPO, PORT, ISSUE_TITLE, ISSUE_BODY, POLL_SECS, NO_ISSUE=1.
e2e-verify:
	bash scripts/e2e_live_verify.sh
