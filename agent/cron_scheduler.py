"""Per-agent cron jobs and a durable delivery queue (UTC+8)."""

import json
import logging
import secrets
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


CRON_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")
logger = logging.getLogger(__name__)


@dataclass
class CronJob:
    id: str
    cron: str
    prompt: str
    recurring: bool
    durable: bool
    pending_delivery: bool = False
    last_fired: str | None = None


def _cron_field_matches(field: str, value: int, minimum: int = 0) -> bool:
    if "," in field:
        return any(_cron_field_matches(part, value, minimum) for part in field.split(","))
    if field == "*":
        return True
    if field.startswith("*/"):
        return (value - minimum) % int(field[2:]) == 0
    if "-" in field:
        start, end = field.split("-", 1)
        return int(start) <= value <= int(end)
    return value == int(field)


def _validate_cron_field(field: str, minimum: int, maximum: int) -> str | None:
    if "," in field:
        for part in field.split(","):
            error = _validate_cron_field(part, minimum, maximum)
            if error:
                return error
        return None
    if field == "*":
        return None
    if field.startswith("*/"):
        step = field[2:]
        return None if step.isdigit() and int(step) > 0 else f"Invalid step: {field}"
    if "-" in field:
        start, end = field.split("-", 1)
        if not start.isdigit() or not end.isdigit():
            return f"Invalid range: {field}"
        if not minimum <= int(start) <= int(end) <= maximum:
            return f"Range {field} is outside [{minimum}-{maximum}] or reversed"
        return None
    if not field.isdigit() or not minimum <= int(field) <= maximum:
        return f"Invalid value {field!r}; expected [{minimum}-{maximum}]"
    return None


def validate_cron(cron_expr: str) -> str | None:
    if not isinstance(cron_expr, str):
        return "Cron must be a string"
    fields = cron_expr.split()
    if len(fields) != 5:
        return f"Expected 5 fields, got {len(fields)}"
    rules = (("minute", 0, 59), ("hour", 0, 23), ("day", 1, 31),
             ("month", 1, 12), ("weekday", 0, 6))
    for field, (name, minimum, maximum) in zip(fields, rules):
        error = _validate_cron_field(field, minimum, maximum)
        if error:
            return f"{name}: {error}"
    return None


def cron_matches(cron_expr: str, moment: datetime) -> bool:
    """Match five fields; aware datetimes are converted to the scheduler timezone."""
    if validate_cron(cron_expr):
        return False
    if moment.tzinfo is not None:
        moment = moment.astimezone(CRON_TIMEZONE)
    minute, hour, day, month, weekday = cron_expr.split()
    if not (
        _cron_field_matches(minute, moment.minute)
        and _cron_field_matches(hour, moment.hour)
        and _cron_field_matches(month, moment.month, 1)
    ):
        return False
    day_matches = _cron_field_matches(day, moment.day, 1)
    weekday_matches = _cron_field_matches(weekday, (moment.weekday() + 1) % 7)
    if day == "*":
        return weekday_matches
    if weekday == "*":
        return day_matches
    return day_matches or weekday_matches


class CronScheduler:
    """Owns one agent's jobs; its thread only queues prompts, never calls a model."""

    def __init__(self, workdir: Path):
        self.path = Path(workdir).resolve() / ".scheduled_tasks.json"
        self.scheduled_jobs: dict[str, CronJob] = {}
        self._queue: list[CronJob] = []
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False
        self.tools = [
            {"type": "function", "function": {
                "name": "schedule_cron",
                "description": "Schedule a prompt using five-field cron in UTC+8; weekday 0 is Sunday.",
                "parameters": {"type": "object", "properties": {
                    "cron": {"type": "string"}, "prompt": {"type": "string"},
                    "recurring": {"type": "boolean", "default": True},
                    "durable": {"type": "boolean", "default": True},
                }, "required": ["cron", "prompt"]},
            }},
            {"type": "function", "function": {
                "name": "list_crons", "description": "List this agent's scheduled prompts.",
                "parameters": {"type": "object", "properties": {}},
            }},
            {"type": "function", "function": {
                "name": "cancel_cron", "description": "Cancel a scheduled prompt by ID.",
                "parameters": {"type": "object", "properties": {
                    "job_id": {"type": "string"},
                }, "required": ["job_id"]},
            }},
        ]
        self.handlers = {
            "schedule_cron": self._schedule_tool,
            "list_crons": self.list_jobs,
            "cancel_cron": self.cancel_job,
        }

    def _save(self) -> None:
        """Caller holds the lock; publish a complete snapshot by replacing the file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        payload = [asdict(job) for job in self.scheduled_jobs.values() if job.durable]
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                return
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                raise ValueError("Scheduled tasks must be a JSON list")
            for item in payload:
                job = CronJob(**item)
                error = validate_cron(job.cron)
                if error or not isinstance(job.prompt, str) or not job.prompt.strip():
                    raise ValueError(error or "Scheduled prompt cannot be empty")
                if not isinstance(job.id, str) or not job.id.startswith("cron_"):
                    raise ValueError("Invalid scheduled job ID")
                if job.id not in self.scheduled_jobs:
                    self.scheduled_jobs[job.id] = job
                    if job.pending_delivery:
                        self._queue.append(job)

    def schedule_job(self, cron: str, prompt: str, recurring: bool = True,
                     durable: bool = True) -> CronJob:
        error = validate_cron(cron)
        if error:
            raise ValueError(error)
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Prompt must be a non-empty string")
        if type(recurring) is not bool or type(durable) is not bool:
            raise ValueError("recurring and durable must be booleans")
        with self._lock:
            if self._closed:
                raise RuntimeError("Cron scheduler is closed")
            job_id = f"cron_{secrets.token_hex(8)}"
            while job_id in self.scheduled_jobs:
                job_id = f"cron_{secrets.token_hex(8)}"
            job = CronJob(job_id, cron, prompt, recurring, durable)
            self.scheduled_jobs[job_id] = job
            try:
                if durable:
                    self._save()
            except Exception:
                del self.scheduled_jobs[job_id]
                raise
            return job

    def _schedule_tool(self, cron: str, prompt: str, recurring: bool = True,
                       durable: bool = True) -> str:
        job = self.schedule_job(cron, prompt, recurring, durable)
        return f"Scheduled {job.id}: {cron} -> {prompt}"

    def list_jobs(self) -> str:
        with self._lock:
            return "\n".join(
                f"{job.id}: {job.cron} -> {job.prompt[:60]} "
                f"[{'recurring' if job.recurring else 'one-shot'}, "
                f"{'durable' if job.durable else 'session'}]"
                for job in self.scheduled_jobs.values()
            ) or "No cron jobs."

    def cancel_job(self, job_id: str) -> str:
        with self._lock:
            job = self.scheduled_jobs.pop(job_id, None)
            if job is None:
                return f"Job {job_id} not found"
            try:
                if job.durable:
                    self._save()
            except Exception:
                self.scheduled_jobs[job_id] = job
                raise
            self._queue[:] = [queued for queued in self._queue if queued.id != job_id]
            return f"Cancelled {job_id}"

    def poll_due_jobs(self, moment: datetime) -> None:
        if moment.tzinfo is not None:
            moment = moment.astimezone(CRON_TIMEZONE)
        marker = moment.strftime("%Y-%m-%d %H:%M")
        with self._lock:
            for job in self.scheduled_jobs.values():
                if job.pending_delivery or job.last_fired == marker or not cron_matches(job.cron, moment):
                    continue
                previous_fired = job.last_fired
                job.pending_delivery = True
                job.last_fired = marker
                try:
                    if job.durable:
                        self._save()
                except Exception:
                    job.pending_delivery = False
                    job.last_fired = previous_fired
                    raise
                self._queue.append(job)

    def consume(self) -> list[CronJob]:
        with self._lock:
            jobs = list(self._queue)
            self._queue.clear()
            return jobs

    def acknowledge(self, jobs: list[CronJob]) -> None:
        """Mark delivered prompts accepted; one-shot jobs are removed only here."""
        with self._lock:
            changed = [(self.scheduled_jobs[job.id], self.scheduled_jobs[job.id].pending_delivery)
                       for job in jobs if job.id in self.scheduled_jobs]
            for job, _ in changed:
                if job.recurring:
                    job.pending_delivery = False
                else:
                    del self.scheduled_jobs[job.id]
            try:
                if any(job.durable for job, _ in changed):
                    self._save()
            except Exception:
                for job, pending in changed:
                    job.pending_delivery = pending
                    self.scheduled_jobs[job.id] = job
                raise

    def restore(self, jobs: list[CronJob]) -> None:
        """Return unacknowledged prompts to the queue without resurrecting cancelled jobs."""
        with self._lock:
            queued_ids = {job.id for job in self._queue}
            for delivered in jobs:
                current = self.scheduled_jobs.get(delivered.id)
                if current is not None:
                    current.pending_delivery = True
                    if current.id not in queued_ids:
                        self._queue.append(current)
                        queued_ids.add(current.id)

    def start(self, stop_event: threading.Event) -> threading.Thread:
        with self._lock:
            if self._closed:
                raise RuntimeError("Cron scheduler is closed")
            if self._thread is not None:
                raise RuntimeError("Cron scheduler has already started")
            self._thread = threading.Thread(
                target=self._run, args=(stop_event,), name="cron-scheduler", daemon=True,
            )
            self._thread.start()
            return self._thread

    def _run(self, stop_event: threading.Event) -> None:
        while not self._stop.is_set() and not stop_event.is_set():
            try:
                self.poll_due_jobs(datetime.now(CRON_TIMEZONE))
            except OSError:
                logger.exception("Could not persist scheduled prompts")
            self._stop.wait(1.0)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._stop.set()
            thread = self._thread
        if thread is not None:
            thread.join()
