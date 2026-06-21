.PHONY: run-dev stop-dev

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
