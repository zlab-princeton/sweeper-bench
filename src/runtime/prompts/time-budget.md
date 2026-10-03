
Work duration:
- You must work until {{DEADLINE_UTC}} (Unix timestamp {{DEADLINE_EPOCH}}). Work for exactly {{WORK_MINUTES}} minutes from the start of this session.
- Use Bash to check the actual UTC time with `date -u '+%Y-%m-%dT%H:%M:%SZ'` and `date +%s`, now and periodically as you work. Do not estimate elapsed time from tokens or tool calls.
- Until that deadline, continue investigating the tasked area, reproducing issues, improving the fix, and validating it. Do not finish early or sleep merely to exhaust the budget. Keep all original task and network restrictions.
- At the deadline, stop investigating new issues and submit your existing diff by leaving changes in the working tree and providing your final response. Do not commit.
- The orchestrator allows a five-minute grace period, then stops your active process and resumes this same session to request submission. That submission turn has a final five-minute limit; afterward the process is stopped and the existing diff is collected.
