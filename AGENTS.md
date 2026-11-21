# Scope
- Applies to: all repositories by default, unless a repo-local `AGENTS.md` overrides specific rules.

# Precedence
- Unless a repo overrides this model, documentation authority order SHALL be: `requests.md` > `design.md` > `architecture.md` > `issues.md`.

# Global instructions
- MUST list options and ask the user only when a blocking decision exists and cannot be inferred from context.
- When multiple blocking decisions are needed, MUST ask exactly one multiple-choice decision at a time and proceed to the next only after the prior answer.
- At the end of a task, you MUST 
  - propose the next few steps and let the user choose the next one.
- MUST keep documentation concise and avoid duplicating requirement text across multiple docs; prefer references to canonical sections.
- MUST remove duplicate draft text from temporary/draft notes once a spec is promoted into canonical `docs/` sections.
- For major new or changed functionality, MUST first promote the requirement/behavior to `docs/design.md` before implementation, and MUST record the implemented outcome in `docs/architecture.md` afterward (same task).
- For repos that use `requests.md`, `design.md`, `architecture.md`, and `issues.md`, MUST apply this default model unless the repo overrides it:
  - `issues.md` SHALL contain active queue only (`Active issues` and `Deferred backlog`; no completed-history log).
  - accepted requirement decision SHALL update `requests.md`, normalize in `design.md`, and align `architecture.md` if implementation is impacted.
  - design-only clarification SHALL update `design.md`; update `architecture.md` only when implementation constraints change.
  - implementation behavior change SHALL update `architecture.md`; if behavior/semantics changed, also update `design.md` and possibly `requests.md`.
  - issue lifecycle SHALL fix first open issue, then remove resolved issue after implementation + verification.
  - doc updates SHALL happen in the same task as the code/behavior change.

- Before making code changes, the agent MUST consult applicable project files for guidance and context:
  - `docs/design.md` — canonical design decisions
  - `docs/architecture.md` — implemented architecture and constraints
  - `docs/requests.md` — accepted requirement decisions and clarifications
  - `previous_work/` — prior examples when relevant
  - `tmp/` — temporary workspace; do not persist changes unless instructed
- The agent MUST read and apply authoritative files that are relevant to the task, and MUST NOT generate code that contradicts them.


# Preferences
- MUST prefer simple solutions over theoretically elegant ones.
- MUST keep functions, classes, methods, and variables documented.

# Creating pull requests
- MUST use the git remote origin as an orientation.
- MUST target branch `main` for pull requests.
- MUST NOT push directly to `main`; push changes to a non-`main` branch and open a PR into `main`.
- MUST use credentials from `~/.git-credentials`.

# Workflow: "wrap up work"
- "wrap up work" SHALL follow these rules:
- MUST double-check whether the current implementation violates any design specifications due to user requests; if so, point them out.
- MUST git stage all modified files.
- MUST make a git commit with a concise summary of current status as the commit message.
- These requirements apply only when the user explicitly invokes the `wrap up work` workflow.

# Quick checklist
- Confirm whether repo-local `AGENTS.md` overrides global rules.
- Identify whether a blocking decision exists; ask one multiple-choice question at a time only when needed.
- Align docs using authority order: `requests.md` > `design.md` > `architecture.md` > `issues.md`.
- For major behavior changes: update `design.md` before implementation and `architecture.md` after implementation.
- Keep docs concise; avoid duplicating requirement text across multiple files.

