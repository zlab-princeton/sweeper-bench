"""Capture the runner and prompts at launch, before either remote stage starts."""
import hashlib
import subprocess
import uuid
from pathlib import Path


def capture(repo, config, rows):
    repo = Path(repo)
    def git(*args):
        try:
            return subprocess.check_output(
                ['git', *args], cwd=repo, text=True, stderr=subprocess.DEVNULL
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            return None
    commit = git('rev-parse', 'HEAD')
    dirty = git('status', '--porcelain', '--untracked-files=no')
    runtime = repo / 'src/runtime'
    templates = {str(p.relative_to(runtime)): p.read_text() for p in (runtime / 'prompts').glob('*.md')}
    pred = config.get('prediction', {})
    key = 'prompts/task-templates-' + pred.get('prompt_templates', 'browser') + '.md'
    template = templates.get(key, '')
    minutes = pred.get('time_budget', 'unlimited')
    if isinstance(minutes, (int, float)) and not isinstance(minutes, bool) and minutes > 0:
        template += templates['prompts/time-budget.md'].replace('{{WORK_MINUTES}}', str(float(minutes)))
    return {
        'experiment_id': str(uuid.uuid4()),
        'runner_commit': commit or '',
        'runner_dirty': None if commit is None or dirty is None else bool(dirty),
        'runtime_sha256': {str(p.relative_to(runtime)): hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in sorted(runtime.rglob('*')) if p.is_file() and '__pycache__' not in p.parts},
        'prompt_templates': templates,
        'prompt_template_sha256': {k: hashlib.sha256(v.encode()).hexdigest() for k, v in templates.items()},
        'prediction_prompt_template_sha256': hashlib.sha256(template.encode()).hexdigest(),
        'prediction_prompts': {r['id']: template.replace('{{TASK_DESCRIPTION}}', r['task'][pred.get('level', 'scoped')]) for r in rows},
        'note': 'Actual judge prompts and instructions are also retained in evaluation artifacts. Image tags may be mutable; worker logs record available pull digests.',
    }
