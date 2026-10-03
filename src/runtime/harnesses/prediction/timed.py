"""Timed work, bounded grace, and a final same-session submission turn."""
import datetime
import hashlib
import json
import os
import re
import signal
from pathlib import Path
import subprocess
import sys
import time


def run(harness, command, *, grace_seconds=300, submission_seconds=300):
    minutes = float(os.environ['PREDICTION_WORK_MINUTES'])
    log = Path(os.environ.get('PREDICTION_LOG_DIR') or os.environ['LOG_DIR'])
    template = Path(os.environ.get('TIME_BUDGET_TEMPLATE', '/usr/local/share/time-budget.md')).read_text()
    started = time.time()
    deadline = started + minutes * 60
    deadline_text = datetime.datetime.fromtimestamp(deadline, datetime.timezone.utc).isoformat(timespec='seconds')
    suffix = template.replace('{{DEADLINE_UTC}}', deadline_text).replace('{{DEADLINE_EPOCH}}', str(deadline)).replace('{{WORK_MINUTES}}', str(minutes))
    prompt = Path(os.environ['TASK_FILE']).read_text() + suffix
    metadata = dict(started_at=started, deadline_at=deadline, time_budget=minutes,
                    template_sha256=hashlib.sha256(template.encode()).hexdigest(), session_id=None, turns=[])
    session = None
    phase = "work"
    cutoff = deadline + grace_seconds
    metadata.update(grace_seconds=grace_seconds, submission_seconds=submission_seconds, work_cutoff_at=cutoff)
    try:
        while True:
            number = len(metadata['turns']) + 1
            (log / f'timed-prompt-{number:03d}.txt').write_text(prompt)
            args = list(command)
            if harness == 'codex':
                args.insert(2, '--json')
                if session:
                    args.insert(2, 'resume')
                    args.insert(len(args) - 1, session)  # final '-' reads prompt from stdin
                input_text = prompt
            else:
                args[args.index('-p') + 1] = prompt
                if session:
                    args += ['--resume', session]
                input_text = None
            turn = dict(started_at=time.time(), prompt_file=f'timed-prompt-{number:03d}.txt', resumed=bool(session), phase=phase, cutoff_at=cutoff)
            metadata['turns'].append(turn)
            (log / 'timed-run.json').write_text(json.dumps(metadata, indent=2))
            success = False
            failed = False
            timed_out = False
            trace_path = log / f'timed-trajectory-{number:03d}.jsonl'
            # File-backed stdout avoids pipe deadlocks and retains partial JSONL on kill.
            with trace_path.open('w') as trace, (log / f'timed-prompt-{number:03d}.txt').open() as prompt_input:
                process = subprocess.Popen(args, stdin=prompt_input if input_text is not None else subprocess.DEVNULL,
                                           stdout=trace, start_new_session=True)
                try:
                    code = process.wait(timeout=max(0, cutoff - time.time()))
                except subprocess.TimeoutExpired:
                    timed_out = True
                    # Kill the process group, including tools, before resuming the workspace.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    code = process.wait()
                except BaseException:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    raise
            with trace_path.open() as trace:
                for line in trace:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    candidate = event.get('thread_id') if event.get('type') == 'thread.started' else event.get('session_id') if harness == 'claude-code' else None
                    if candidate:
                        if session and candidate != session:
                            failed = True
                        else:
                            session = candidate
                            metadata['session_id'] = session
                    if harness == 'codex':
                        success |= event.get('type') == 'turn.completed'
                        # Codex emits recoverable reconnect notices as type=error.
                        # Terminal failures and non-reconnect errors must still stop.
                        reconnect = bool(re.match(r'^Reconnecting\.\.\. \d+/\d+ \(', str(event.get('message', ''))))
                        failed |= event.get('type') == 'turn.failed' or (event.get('type') == 'error' and not reconnect)
                    elif event.get('type') == 'result':
                        success = event.get('subtype') == 'success' and not event.get('is_error')
                        failed |= not success
            turn.update(finished_at=time.time(), exit_code=code, success=success and not failed and not timed_out, timed_out=timed_out)
            if timed_out and not failed and session:
                if phase == 'submission':
                    metadata['status'] = 'submission_timeout'
                    return  # Collect the existing diff; do not claim a successful agent turn.
                phase = 'submission'
                cutoff = time.time() + submission_seconds
                metadata['submission_cutoff_at'] = cutoff
                prompt = 'Time is up, please submit diff. Stop investigating or expanding the fix. Leave your existing changes in the working tree for patch collection; do not commit. Summarize the changes now. You have at most five minutes to finish. Keep all original task and network restrictions.'
                continue
            if code or failed or not success or not session:
                raise RuntimeError(f'{harness} timed turn failed (exit={code}); see trajectory; no automatic error retry')
            if phase == 'submission' or time.time() >= deadline:
                metadata['status'] = 'completed'
                return
            remaining = (deadline - time.time()) / 60
            prompt = f'You must work until {deadline_text}. There are {remaining:.2f} minutes left. Please continue. Check the time using Bash. Keep all original task restrictions; continue useful investigation and validation rather than waiting.'
    except BaseException as error:
        metadata.update(status='failed', error=str(error))
        raise
    finally:
        metadata['finished_at'] = time.time()
        (log / 'timed-run.json').write_text(json.dumps(metadata, indent=2))


if __name__ == '__main__':
    run(sys.argv[1], sys.argv[2:])
