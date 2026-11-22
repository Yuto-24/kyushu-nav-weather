# Kyushu NAV LOG weather agent guide

## Coordination and review

- GPT-6 Astra and GPT-6.1 Sol are both used in this project. The model selected for the task performs investigation, implementation, and testing directly. Preserve the user's model and reasoning-effort selection; do not delegate other work unless the user explicitly requests it.
- Review repository changes in a separate, read-only context with the other model: GPT-6.1 Sol reviews Astra's changes, and GPT-6 Astra reviews Sol's changes. Explicitly select the reviewer model; a separate agent or `/review` alone does not establish model independence. If the other model is unavailable, report the review as blocked. Self-review alone is not sufficient. See `docs/agent_workflow.md` for the review handoff and configuration boundaries.
- Before substantive work, inspect the repository documentation, Issues/PRs, prior decisions, and analogous code relevant to the task. Keep the inspection proportional to the change. Reuse prior knowledge only after checking it against the current code, specification, and dependencies.
- Keep reusable, non-obvious findings in the appropriate external documentation. Do not turn AGENTS.md into a work log or duplicate detailed evidence.
- When adding, changing, moving, or reusing user-facing UI copy, follow [the UI copy review workflow](docs/ui_copy_review.md). Confirm wording and display context with Gemini through the available CLI (including agy), retain execution evidence and adoption reasons, and check the final implementation against that record. User-provided wording and intent take precedence but do not waive confirmation; specification approval and existing wording are not substitutes either. Codex, GPT, and other implementation agents must not choose final wording themselves. Unavailable tools, failed execution, or missing evidence leave copy unconfirmed; report that in the PR and completion message, even when functional tests and independent review pass.

## Scope and simplicity

- Make the smallest coherent change that satisfies the task's Goal, Acceptance Criteria, and required correctness, security, and compatibility constraints. Prefer fixing a verified root cause over a superficial workaround; allow only the structural changes necessary to do so.
- Treat task requirements and Non-goals as the scope boundary. Include supporting changes only when necessary for the requested behavior or its safety, compatibility, or regression prevention. Do not fix unrelated discovered problems without authorization, even when serious; report them and hold merge if the risk or required gates warrant it.
- Prefer existing implementations, standard APIs, libraries, and established repository patterns. Do not add abstractions, dependencies, configuration, compatibility layers, or future extensibility without a concrete need in the current task.
- Do not bundle incidental refactoring, cleanup, optimization, or unrelated tests and documentation. Report worthwhile out-of-scope findings separately; propose follow-up work only when justified.
- Choose focused verification during implementation. Before opening or updating a PR, complete the applicable local CI checks and confirm no failures remain. Use this repository's Python/Node runtime and checks in [testing.md](docs/testing.md); NavMate's Docker suites and CI shards do not apply. Follow [the pre-PR local validation procedure](docs/agent_workflow.md#pr提出前のローカル検証). Preserve mandatory CI, independent review, and safety checks; do not rerun passed checks without a specific reason.
- Retrieve evidence incrementally, batch related fixes before expensive validation, and avoid repeatedly inspecting unchanged CI/review state. Follow [the investigation, validation, and waiting procedure](docs/agent_workflow.md#調査検証待機の進め方); efficiency does not waive required gates.
- Do not wait for or continuously monitor CI or external reviews unless the user explicitly requests monitoring. After push/PR updates, report pending checks and end the turn; fixes and new commits do not authorize restarting monitoring. A request to check results permits one snapshot only. This stop rule takes precedence over the default completion workflow; see the linked procedure for details.
- Before completion, audit the diff for scope expansion and unnecessary complexity. Remove or separate changes not justified by the task.

## Product constraints

- Avoid unnecessary secondary labels and descriptions. Let the layout do the explaining.

- Preserve the accepted paper and weather semantics in [the requirements](output/kyushu_weather_sector_requirements.md). Changes to sector selection, wind values, rounding, aircraft performance, dates, or publication status require explicit task scope and appropriate verification.
- For release-management, version metadata, documentation, or CI changes without user-facing behavior or layout changes, skip browser automation, screenshots, and visual checks unless the user explicitly requests them. Use only applicable focused source, build, or health checks.
- GitHub Actions performs acquisition and rendering; Cloudflare Pages serves completed static files; the dedicated Cloudflare Worker only supplements Actions dispatch. Follow [the operating procedure](production/README.md). Local rendering, an accepted dispatch, public readback, and a natural Cron invocation are separate observations.
- Ordinary feature/fix PRs use `Release: not-required` with a `Reason:`. Record pending release notes under `## Pending release notes`, or explicitly state `None`. Published release notes must not be rewritten. Do not import NavMate's VERSION sources, release branches, or release commands; this repository currently has no application-version release workflow.

## Workspaces and cleanup

- Keep task work isolated from unrelated changes and the existing NavMate/MSM runtime. Do not modify another repository or reuse its deployment resources merely because its implementation is a reference.
- At completion, account for task-owned checkouts, processes, caches, and temporary backups using [the cleanup procedure](docs/development_worktrees.md). Dispose only of completed, task-owned resources after checking ownership and uncommitted changes; retain active, unfinished, or uncertain work and record why. Do not use global cleanup commands.

## Change and delivery safety

- Preserve unrelated user changes; stage and modify only the requested paths. Do not use destructive commands such as `git reset --hard`, `git checkout --`, broad recursive deletion, or volume deletion unless the user explicitly authorizes the exact target.
- Before creating a temporary development backup, record its purpose, exact scope, and disposal condition in the private operation record. At completion, remove only task-owned temporary backups whose restoration need has ended; document why and until when others remain. Protect production backups, live data, and unfinished work. See [development backup retention](docs/development_worktrees.md#開発用バックアップの保持条件); age or size alone is not a deletion decision.
- Unless the user explicitly limits scope, completing requested repository edits includes applicable required checks and independent review; committing scoped changes, pushing the branch, and creating or updating the task PR. Validate the actual affected runtime; local tests do not establish Cloudflare production acceptance. Do not seek separate confirmation for these default completion actions; report the runtime validation, commit, PR URL, hosted CI, and any blocked stage accurately. Merges, remote production deploys, and external publishing outside a PR still require explicit authorization.
- If GitHub Actions cannot run or complete because of GitHub-hosted billing, spending, or usage limits, treat the current workflow definitions as the source of truth and reproduce the applicable CI checks locally as closely as practical. Post to the PR which hosted checks were blocked, the GitHub-side blocking reason, the local commands or checks run, and their results. Do not report hosted CI as passed; state that it was blocked and locally reproduced. If any hosted-only check cannot be reproduced locally, identify it explicitly and leave that gate unresolved.
- Before an explicitly authorized merge, independently confirm CI, reviews (including CodeRabbit), unresolved threads, `reviewDecision`, `mergeable`, and `mergeStateStatus`; all must be in an acceptable final state. Report any unresolved or stale gate rather than merging.

## Review findings and scope

- Apply the same scope boundary to CodeRabbit, independent reviews, and other external findings. Treat findings as evidence to assess, not authorization to expand the PR; a severity label alone does not establish that a change belongs in scope.
- Fix technically valid findings required for the requested behavior, correctness, security, compatibility, or regression prevention, including necessary root-cause fixes. Do not implement optional refactoring, architectural improvements, optimizations, speculative safeguards, or unrelated pre-existing fixes solely because a reviewer suggests them.
- Explain deferred or rejected findings with reasons and evidence in the original review thread. Report serious out-of-scope problems without modifying unrelated code, and pause merge if an unresolved risk or required review/check prevents it. Do not dismiss or resolve blocking findings merely to bypass a merge gate.
- Independent review must also identify unnecessary changes and scope expansion, not just missing fixes. Apply the same necessity test to the independent reviewer's suggestions.

## CodeRabbit reviews

- PRs require an initial automatic CodeRabbit review; if it does not start or cannot complete, report the unresolved review rather than treating it as complete. The NavMate-only VERSION/CHANGELOG release exception does not apply to this repository.
- Never request a CodeRabbit review manually, including retries or re-reviews. Do not post review commands, request reviewers, or use another manual trigger. Do not change paid plans to obtain reviews.
- Assess any automatically submitted findings against the current diff and scope. Record necessary fixes and verification in the PR or original threads. An older review does not cover newer changes without checking the intervening diff.
- Assess substantive review completion separately from GitHub merge eligibility. Check the current `reviewDecision`, unresolved threads, required checks, and actual protection rules; report any concrete unresolved gate. Do not dismiss reviews, weaken protection, or mark unreviewed work as approved to bypass a gate.
- Preserve independent review and all actual repository gates. Required latest-commit approval, required checks, unresolved conversations, and effective change requests still apply. Merge and deployment require their existing authorization.

When a repository's documentation and its existing code disagree, follow the conventions used in the existing code, and in your final reply point out each inconsistency you found between the documentation and the code

## Reusable knowledge

Before work, consult [the shared knowledge index](docs/knowledge/INDEX.md) and only the entries relevant to the task; follow [the knowledge workflow](docs/knowledge/README.md) to verify applicability and preserve non-obvious findings with evidence through incremental updates. Keep detailed lessons out of this file, reuse existing canonical documents, and do not create an entry when nothing reusable was learned.

## Reader-facing writing

Before creating or updating PR bodies, Issue bodies, comments, ROADMAPs, work reports, or final replies for this project, read and follow [the writing standard](docs/writing-style.md). This applies to GitHub and chat messages as well as repository files. Run its submission checklist before sending or publishing. Keep the standard canonical rather than duplicating its detailed rules here. Writing rules do not authorize changes, publication, or additional notifications.
