"""Cron matching, independent stores and delivery lifecycle without model calls."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest

from agent.cron_scheduler import CRON_TIMEZONE, CronScheduler, cron_matches, validate_cron


class CronSchedulerTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="agent-cron-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def scheduler(self, name="worker"):
        scheduler = CronScheduler(self.root / name)
        self.addCleanup(scheduler.close)
        return scheduler

    def test_day_and_month_steps_start_at_one(self):
        for value in range(1, 7):
            with self.subTest(value=value):
                self.assertEqual(
                    cron_matches("0 9 */2 * *", datetime(2026, 10, value, 9)),
                    value % 2 == 1,
                )
                self.assertEqual(
                    cron_matches("0 9 1 */2 *", datetime(2026, value, 1, 9)),
                    value % 2 == 1,
                )

    def test_lists_can_include_steps_and_ranges(self):
        expression = "*/20,7 0-3 * * 1-5"
        self.assertIsNone(validate_cron(expression))
        for minute in (0, 7, 20, 40):
            self.assertTrue(cron_matches(expression, datetime(2026, 10, 26, 2, minute)))
        self.assertFalse(cron_matches(expression, datetime(2026, 10, 26, 2, 8)))
        self.assertFalse(cron_matches(expression, datetime(2026, 10, 25, 2, 7)))
        self.assertFalse(cron_matches(expression, datetime(2026, 10, 26, 4, 7)))

    def test_aware_moments_use_utc_plus_eight(self):
        self.assertTrue(cron_matches("0 9 * * *", datetime(2026, 10, 24, 1, tzinfo=timezone.utc)))
        self.assertFalse(cron_matches("0 1 * * *", datetime(2026, 10, 24, 1, tzinfo=timezone.utc)))

    def test_invalid_schedules_do_not_create_jobs(self):
        scheduler = self.scheduler()
        for expression in ("*/0 * * * *", "0 9 32 * *", "0 9 * 13 *", "*/2, * * * *", "* * *"):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                scheduler.schedule_job(expression, "work")
        with self.assertRaises(ValueError):
            scheduler.schedule_job("* * * * *", " ")
        self.assertEqual(scheduler.scheduled_jobs, {})
        self.assertFalse(scheduler.path.exists())

    def test_instances_and_tool_handlers_are_isolated(self):
        first, second = self.scheduler("first"), self.scheduler("second")
        result = first.handlers["schedule_cron"]("0 9 * * *", "first only")
        self.assertIn("Scheduled cron_", result)
        self.assertIn("first only", first.handlers["list_crons"]())
        self.assertEqual(second.handlers["list_crons"](), "No cron jobs.")
        self.assertEqual(second.consume(), [])
        self.assertFalse(second.path.exists())
        for tool in first.tools:
            self.assertEqual(tool["type"], "function")
            self.assertIn(tool["function"]["name"], first.handlers)
            self.assertIn("parameters", tool["function"])

    def test_pending_one_shot_is_restored_until_acknowledged(self):
        first = self.scheduler()
        job = first.schedule_job("0 9 * * *", "deliver once", recurring=False)
        first.schedule_job("0 9 * * *", "session only", durable=False)
        first.poll_due_jobs(datetime(2026, 10, 24, 9, tzinfo=CRON_TIMEZONE))
        self.assertEqual(len(first.consume()), 2)
        self.assertIn(job.id, first.scheduled_jobs)
        first.close()

        restored = self.scheduler()
        restored.load()
        restored.load()  # Loading again must not queue a second delivery.
        pending = restored.consume()
        self.assertEqual([item.id for item in pending], [job.id])
        self.assertEqual(pending[0].last_fired, "2026-10-24 09:00")
        restored.acknowledge(pending)
        self.assertEqual(restored.scheduled_jobs, {})
        self.assertEqual(json.loads(restored.path.read_text()), [])
        self.assertEqual(restored.consume(), [])

    def test_recurring_job_waits_for_ack_and_fires_once_per_minute(self):
        scheduler = self.scheduler()
        job = scheduler.schedule_job("* * * * *", "repeat", durable=False)
        first = datetime(2026, 10, 24, 9, tzinfo=CRON_TIMEZONE)
        scheduler.poll_due_jobs(first)
        pending = scheduler.consume()
        self.assertEqual([item.id for item in pending], [job.id])
        scheduler.poll_due_jobs(first + timedelta(minutes=1))
        self.assertEqual(scheduler.consume(), [])
        scheduler.acknowledge(pending)
        scheduler.poll_due_jobs(first)
        self.assertEqual(scheduler.consume(), [])
        scheduler.poll_due_jobs(first + timedelta(minutes=1))
        self.assertEqual([item.id for item in scheduler.consume()], [job.id])

    def test_restore_deduplicates_and_cancellation_does_not_resurrect(self):
        scheduler = self.scheduler()
        job = scheduler.schedule_job("* * * * *", "pending", durable=False)
        scheduler.poll_due_jobs(datetime(2026, 10, 24, 9))
        pending = scheduler.consume()
        scheduler.restore(pending)
        scheduler.restore(pending)
        self.assertEqual([item.id for item in scheduler.consume()], [job.id])
        scheduler.cancel_job(job.id)
        scheduler.restore(pending)
        self.assertEqual(scheduler.consume(), [])
        self.assertNotIn(job.id, scheduler.scheduled_jobs)

    def test_close_stops_only_its_own_thread_and_is_idempotent(self):
        scheduler = self.scheduler()
        shared_stop = threading.Event()
        thread = scheduler.start(shared_stop)
        self.assertTrue(thread.is_alive())
        scheduler.close()
        scheduler.close()
        self.assertFalse(thread.is_alive())
        self.assertFalse(shared_stop.is_set())
        with self.assertRaises(RuntimeError):
            scheduler.start(shared_stop)

    def test_external_stop_finishes_scheduler_thread(self):
        scheduler = self.scheduler()
        shared_stop = threading.Event()
        thread = scheduler.start(shared_stop)
        shared_stop.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
