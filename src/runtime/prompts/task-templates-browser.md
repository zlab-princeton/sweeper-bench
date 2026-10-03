You are working in `/workspace/product`.

Please perform the following task:

{{TASK_DESCRIPTION}}
Important notes:
- Use only this checkout and these instructions. Do not search external repositories or known fixes.
- Do not fetch Git history, add remotes, or commit.
- This environment has no outbound internet for packages or source. npm, PyPI, GitHub, CDNs, and similar hosts are blocked and will return 403. Do not install, fetch, download, or rebuild anything from the network. Dependencies and tools are already on disk. If a command fails with a network or 403 error, treat that as final: do not retry, switch mirrors, or keep waiting. Continue with the local checkout and already-installed tools only.
- The product server is published on this machine at 127.0.0.1:13200.
- Use the browser to do end-to-end testing to discover and test issues. Drive the product UI like a real user in the tasked area and look for bugs that show up during normal use. Do not stop after the first issue you can patch; keep exploring other screens and controls in the same area.
- Chromium and Playwright are preinstalled.

How to verify your work:
- Product commands must run in the product container through `/workspace/task-shell`.
- Open the product UI at http://127.0.0.1:13200 with the preinstalled Playwright/Chromium in this environment. Do not launch the browser through `/workspace/task-shell`.
- Do not call `yarn`, `pnpm`, or `npm` directly; use the `product-*` wrappers in the container PATH.
- Find the right test or build scripts from package.json, README, or existing CI, then run them through `/workspace/task-shell`.

Deliverable:
- Modify product source in place and leave a clean, reviewable diff with at least one real code change.
- An empty diff is not acceptable. Do not commit.