"""Shard cases across VMs. One VM per shard; cases on that VM run serially."""

import concurrent.futures
import json
import threading
import time
from pathlib import Path
from data import (
    evaluation_cells_complete,
    needs_prediction_phase,
    normalize_evaluation_phases,
    patch_path,
    prediction_patch_complete,
)


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def env_flag(value, default=True):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in ("0", "false", "no", "off"):
        return False
    if text in ("1", "true", "yes", "on"):
        return True
    return default


def split_shards(rows, n):
    if not rows:
        return []
    n = max(1, min(int(n), len(rows)))
    size = (len(rows) + n - 1) // n
    return [rows[i : i + size] for i in range(0, len(rows), size)]


class Runner:
    def __init__(self, config, backend, root):
        self.config = config
        self.backend = backend
        self.root = Path(root)
        self.lock = threading.RLock()

    def account(self, stage, index):
        names = self.config[stage]["accounts"]
        name = names[index % len(names)]
        account = self.config["accounts"][name]
        if account["harness"] != self.config[stage]["harness"]:
            raise ValueError(f"{stage}: account {name} does not match harness")
        return name, account

    def prediction_done(self, case):
        return prediction_patch_complete(
            self.config, self.root / "predictions" / case / "artifacts"
        )

    def evaluation_incoming(self, case, predictions):
        incoming = {}
        for phase in normalize_evaluation_phases(self.config):
            if not phase.startswith("prediction-"):
                continue
            path = (
                Path(predictions)
                / "predictions"
                / case
                / "artifacts"
                / f"{phase}.patch"
            )
            if path.is_file() and "diff --git " in path.read_text(errors="replace"):
                incoming[phase] = path
        if "reference" in normalize_evaluation_phases(self.config):
            ref = patch_path(self.config, case)
            if ref.is_file() and "diff --git " in ref.read_text(errors="replace"):
                incoming["reference"] = ref
        return incoming

    def evaluation_done(self, row, incoming=None):
        return evaluation_cells_complete(
            self.config,
            row,
            self.root / "evaluations" / row["id"] / "artifacts",
            incoming,
        )

    def prepare_job(self, stage, row, resume, predictions):
        case = row["id"]
        dest_root = self.root / ("predictions" if stage == "prediction" else "evaluations") / case
        dest_root.mkdir(parents=True, exist_ok=True)
        incoming = {}
        if stage == "evaluation":
            incoming = self.evaluation_incoming(case, predictions or self.root)
        if resume and (
            self.prediction_done(case)
            if stage == "prediction"
            else self.evaluation_done(row, incoming)
        ):
            saved = dest_root / "result.json"
            if saved.is_file():
                return None, json.loads(saved.read_text())
            return None, {"id": case, "status": "completed"}
        pred_incoming = {k: v for k, v in incoming.items() if k.startswith("prediction-")}
        if stage == "evaluation" and needs_prediction_phase(self.config) and not pred_incoming:
            skipped = {
                "id": case,
                "status": "skipped",
                "reason": "No valid completed prediction",
            }
            save(dest_root / "result.json", skipped)
            return None, skipped
        return (row, dest_root, incoming), None

    def finish_job(self, stage, row, dest_root, incoming, error=None):
        case = row["id"]
        result = {
            "id": case,
            "status": "running",
            "started": time.time(),
        }
        if error is not None:
            result.update(status="infrastructure_failed", error=str(error), finished=time.time())
            save(dest_root / "result.json", result)
            print(case, stage, result["status"], flush=True)
            return result
        try:
            if stage == "prediction":
                ok = self.prediction_done(case)
            else:
                ok = self.evaluation_done(row, incoming)
        except Exception as exc:
            result["status"] = "incomplete"
            result["error"] = f"completeness check failed: {exc}"
            result["finished"] = time.time()
            save(dest_root / "result.json", result)
            print(case, stage, result["status"], flush=True)
            return result
        result["status"] = "completed" if ok else "incomplete"
        if not ok:
            result["error"] = "task-complete or patch check failed"
        result["finished"] = time.time()
        save(dest_root / "result.json", result)
        print(case, stage, result["status"], flush=True)
        return result

    def stage_case_dir(self, stage, case):
        name = "predictions" if stage == "prediction" else "evaluations"
        return self.root / name / case

    def collect_stage_results(self, stage):
        root = self.root / ("predictions" if stage == "prediction" else "evaluations")
        if not root.is_dir():
            return []
        results = []
        for path in sorted(root.glob("sweeper-*/result.json")):
            saved = self.read_saved_result(stage, path.parent.name)
            if saved:
                results.append(saved)
        return results

    def read_saved_result(self, stage, case):
        path = self.stage_case_dir(stage, case) / "result.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text())
        except Exception:
            return None
        if not isinstance(data, dict) or data.get("id") != case:
            return None
        return data

    def recover_shard_outcomes(self, stage, rows, error, predictions=None):
        recovered = []
        for row in rows:
            case = row["id"]
            saved = self.read_saved_result(stage, case)
            if saved and saved.get("status") in (
                "completed",
                "skipped",
                "incomplete",
                "infrastructure_failed",
            ):
                recovered.append(saved)
                continue
            dest = self.stage_case_dir(stage, case)
            dest.mkdir(parents=True, exist_ok=True)
            incoming = {}
            if stage == "evaluation":
                incoming = self.evaluation_incoming(case, predictions or self.root)
            recovered.append(self.finish_job(stage, row, dest, incoming, error=error))
        return recovered

    def run_shard(self, stage, shard_index, rows, resume, predictions):
        name, account = self.account(stage, shard_index)
        jobs = []
        outcomes = []
        for row in rows:
            job, early = self.prepare_job(stage, row, resume, predictions)
            if early is not None:
                outcomes.append(early)
            else:
                jobs.append(job)
        if not jobs:
            return outcomes
        boot_dest = self.root / "tmp" / f"shard-{shard_index:02d}"
        print(
            f"{stage} shard {shard_index:02d} vm cases={[j[0]['id'] for j in jobs]} account={name}",
            flush=True,
        )
        sb = None
        abort_remaining = False
        abort_error = None
        try:
            sb = self.backend.create(boot_dest, account)
            self.backend.boot(sb, boot_dest)
            for row, dest, incoming in jobs:
                if abort_remaining:
                    outcomes.append(
                        self.finish_job(stage, row, dest, incoming, error=abort_error)
                    )
                    continue
                save(
                    dest / "result.json",
                    {"id": row["id"], "status": "running", "started": time.time()},
                )
                try:
                    self.backend._exec_case(sb, stage, row, account, dest, incoming)
                    result = self.finish_job(stage, row, dest, incoming)
                    outcomes.append(result)
                    if result.get("status") != "completed" and self.abort_on_case_fail(stage):
                        abort_remaining = True
                        abort_error = RuntimeError(
                            f"abort remaining after {row['id']} ({result.get('status')})"
                        )
                except Exception as exc:
                    outcomes.append(self.finish_job(stage, row, dest, incoming, error=exc))
                    if self.abort_on_case_fail(stage):
                        abort_remaining = True
                        abort_error = exc
        except Exception as exc:
            for row, dest, incoming in jobs:
                if not any(o.get("id") == row["id"] for o in outcomes):
                    outcomes.append(self.finish_job(stage, row, dest, incoming, error=exc))
        finally:
            if sb is not None:
                try:
                    sb.terminate()
                except Exception as exc:
                    print(
                        f"{stage} shard {shard_index:02d} terminate failed: {exc}",
                        flush=True,
                    )
        return outcomes

    def abort_on_case_fail(self, stage):
        return not env_flag(
            self.config.get(stage, {}).get("skip_failed_tasks", 1), True
        )

    def run(self, stage, rows, resume=False, predictions=None):
        slots = sum(
            self.config["accounts"][n].get("concurrency", 1)
            for n in self.config[stage]["accounts"]
        )
        slots = max(1, slots)
        shards = split_shards(rows, slots)
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(shards) or 1) as pool:
            futures = [
                pool.submit(self.run_shard, stage, i, shard, resume, predictions)
                for i, shard in enumerate(shards)
            ]
            outcomes = []
            for shard, future in zip(shards, futures):
                try:
                    outcomes.extend(future.result())
                except Exception as exc:
                    print(f"{stage} shard future failed: {exc}", flush=True)
                    outcomes.extend(
                        self.recover_shard_outcomes(
                            stage, shard, exc, predictions
                        )
                    )
        save(self.root / f"{stage}-summary.json", self.collect_stage_results(stage))
        return outcomes
