from __future__ import annotations

import json
import os
import hashlib
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


JobHandler = Callable[[dict[str, Any]], dict[str, Any]]


NON_RETRYABLE_EXCEPTIONS = (
    ValueError,
    KeyError,
    TypeError,
    AssertionError,
    FileNotFoundError,
    NotImplementedError,
)

RETRYABLE_KEYWORDS = (
    'timeout',
    'tempor',
    'temporar',
    'connection',
    'connect',
    'broken pipe',
    'try again',
    '503',
    '502',
    '500',
    '429',
    'rate limit',
    'service unavailable',
    'too many requests',
)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def coerce_text(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='ignore')
    try:
        return str(value)
    except Exception:
        return ''


@dataclass
class JobStoreSettings:
    root: Path
    kb_rebuild_debounce_seconds: float = 45.0
    fetch_max_attempts: int = 3
    article_max_attempts: int = 2
    internal_max_attempts: int = 2
    retry_backoff_seconds: float = 1.0
    retry_backoff_multiplier: float = 2.0


class JobStore:
    def __init__(self, settings: JobStoreSettings):
        self.settings = settings
        self.root = settings.root
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs_dir = self.root / 'jobs'
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir = self.root / 'state'
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.kb_dirty_path = self.state_dir / 'kb-dirty.json'
        self._handlers: dict[str, JobHandler] = {}
        self._queue: queue.Queue[str] = queue.Queue()
        self._queued_ids: set[str] = set()
        self._lock = threading.RLock()
        self._worker_started = False
        self._stop = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._load_pending_jobs()

    def register(self, action: str, handler: JobHandler) -> None:
        self._handlers[action] = handler

    def start(self) -> None:
        with self._lock:
            if self._worker_started and self._worker_thread and self._worker_thread.is_alive():
                return
            self._load_pending_jobs()
            self._worker_started = True
            self._stop.clear()
            self._worker_thread = threading.Thread(target=self._worker_loop, name='content-memory-mcp-worker', daemon=True)
            self._worker_thread.start()

    def _job_path(self, job_id: str) -> Path:
        return self.jobs_dir / f'{job_id}.json'

    def _read_json(self, path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            return default

    def _write_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.parent / f'.{path.name}.{uuid.uuid4().hex}.tmp'
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        with temp_path.open('w', encoding='utf-8') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)

    def _queue_job_id(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._queued_ids:
                return
            self._queued_ids.add(job_id)
            self._queue.put(job_id)

    def _dequeue_job_id(self, job_id: str) -> None:
        with self._lock:
            self._queued_ids.discard(job_id)

    def _load_pending_jobs(self) -> None:
        for path in sorted(self.jobs_dir.glob('*.json')):
            job = self._read_json(path, {})
            if not isinstance(job, dict) or not job.get('job_id'):
                continue
            status = job.get('status')
            if status == 'running':
                job['status'] = 'queued'
                job.setdefault('warnings', []).append({
                    'stage': 'job_recovery',
                    'message': '服务重启后已重新入队',
                })
                self._write_json(path, job)
            if job.get('status') == 'queued':
                self._queue_job_id(job['job_id'])

    def _job_max_attempts(self, action: str) -> int:
        if action.startswith('internal.'):
            return max(1, int(self.settings.internal_max_attempts))
        if action.startswith('weixin.fetch_') or action == 'weixin.batch_fetch':
            return max(1, int(self.settings.fetch_max_attempts))
        if action.startswith('articles.ingest_'):
            return max(1, int(self.settings.article_max_attempts))
        return 1

    def _compact_payload_for_dedupe(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        compact = dict(payload or {})
        if action == 'articles.ingest_base64':
            content = coerce_text(compact.pop('content_base64', ''))
            compact['content_base64_sha256'] = hashlib.sha256(content.encode('utf-8')).hexdigest() if content else ''
            compact['content_base64_length'] = len(content)
        return compact

    def _dedupe_key(self, action: str, payload: dict[str, Any], *, requested_by: str, internal: bool) -> str:
        if not (action.startswith('weixin.fetch_') or action == 'weixin.batch_fetch' or action.startswith('internal.weixin.') or action.startswith('articles.ingest_')):
            return ''
        normalized = json.dumps(
            {
                'action': action,
                'payload': self._compact_payload_for_dedupe(action, payload),
                'requested_by': requested_by,
                'internal': bool(internal),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        )
        return normalized

    def _find_active_duplicate(self, dedupe_key: str) -> dict[str, Any] | None:
        if not dedupe_key:
            return None
        for path in sorted(self.jobs_dir.glob('*.json'), reverse=True):
            job = self._read_json(path, {})
            if not isinstance(job, dict):
                continue
            if job.get('dedupe_key') != dedupe_key:
                continue
            if job.get('status') in {'queued', 'running'}:
                return job
        return None

    def submit(self, action: str, payload: dict[str, Any], *, requested_by: str = 'tool', internal: bool = False) -> dict[str, Any]:
        self.start()
        dedupe_key = self._dedupe_key(action, payload, requested_by=requested_by, internal=internal)
        with self._lock:
            existing = self._find_active_duplicate(dedupe_key)
            if existing is not None:
                existing = dict(existing)
                existing['_deduped'] = True
                return existing
            job_id = f'job_{datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")}_{uuid.uuid4().hex[:10]}'
            job = {
                'job_id': job_id,
                'action': action,
                'payload': payload,
                'status': 'queued',
                'requested_by': requested_by,
                'internal': bool(internal),
                'created_at': now_iso(),
                'started_at': None,
                'finished_at': None,
                'result': None,
                'error': None,
                'warnings': [],
                'attempts': 0,
                'max_attempts': self._job_max_attempts(action),
                'dedupe_key': dedupe_key,
            }
            self._write_json(self._job_path(job_id), job)
            self._queue_job_id(job_id)
            return job

    def get(self, job_id: str) -> dict[str, Any]:
        self.start()
        path = self._job_path(job_id)
        if not path.exists():
            raise KeyError(f'unknown job: {job_id}')
        return self._read_json(path, {})

    def list(self, *, status: str = '', limit: int = 50, include_internal: bool = False) -> dict[str, Any]:
        self.start()
        rows = []
        for path in sorted(self.jobs_dir.glob('*.json'), reverse=True):
            job = self._read_json(path, {})
            if not include_internal and job.get('internal'):
                continue
            if status and job.get('status') != status:
                continue
            rows.append(self._present_job(job, with_result=False))
            if len(rows) >= max(1, min(limit, 200)):
                break
        return {'ok': True, 'action': 'jobs.list', 'count': len(rows), 'items': rows}

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self.get(job_id)
            if job.get('status') not in {'queued'}:
                return {'ok': False, 'action': 'jobs.cancel', 'job_id': job_id, 'status': job.get('status'), 'error': 'job_not_cancellable'}
            job['status'] = 'cancelled'
            job['finished_at'] = now_iso()
            self._write_json(self._job_path(job_id), job)
            return {'ok': True, 'action': 'jobs.cancel', 'job_id': job_id, 'status': 'cancelled'}

    def mark_kb_dirty(self, account_slug: str) -> None:
        account_slug = (account_slug or '').strip()
        if not account_slug:
            return
        with self._lock:
            data = self._read_json(self.kb_dirty_path, {})
            data[account_slug] = {'marked_at': now_iso()}
            self._write_json(self.kb_dirty_path, data)

    def kb_dirty_state(self) -> dict[str, Any]:
        return self._read_json(self.kb_dirty_path, {})

    def clear_kb_dirty(self, account_slug: str) -> None:
        with self._lock:
            data = self._read_json(self.kb_dirty_path, {})
            if account_slug in data:
                data.pop(account_slug, None)
                self._write_json(self.kb_dirty_path, data)

    def _pending_or_running_for(self, action_prefix: str, account_slug: str) -> bool:
        for path in self.jobs_dir.glob('*.json'):
            job = self._read_json(path, {})
            if job.get('internal') and not action_prefix.startswith('internal.'):
                continue
            if job.get('action') != action_prefix:
                continue
            if (job.get('payload') or {}).get('account_slug') != account_slug:
                continue
            if job.get('status') in {'queued', 'running'}:
                return True
        return False

    def _maybe_enqueue_due_kb_jobs(self) -> None:
        dirty = self._read_json(self.kb_dirty_path, {})
        if not dirty:
            return
        now = datetime.now(timezone.utc)
        debounce = max(1.0, float(self.settings.kb_rebuild_debounce_seconds))
        for slug, info in list(dirty.items()):
            marked_at_raw = (info or {}).get('marked_at') or ''
            try:
                marked_at = datetime.fromisoformat(marked_at_raw.replace('Z', '+00:00'))
            except Exception:
                marked_at = now
            age = (now - marked_at).total_seconds()
            if age < debounce:
                continue
            if self._pending_or_running_for('internal.weixin.rebuild_kb', slug):
                continue
            if self._pending_or_running_for('weixin.fetch_article', slug) or self._pending_or_running_for('weixin.fetch_album', slug) or self._pending_or_running_for('weixin.fetch_history', slug) or self._pending_or_running_for('weixin.batch_fetch', slug):
                continue
            self.submit('internal.weixin.rebuild_kb', {'account_slug': slug}, requested_by='scheduler', internal=True)

    def _set_job(self, job: dict[str, Any]) -> None:
        self._write_json(self._job_path(job['job_id']), job)

    def _present_job(self, job: dict[str, Any], *, with_result: bool = True) -> dict[str, Any]:
        payload = {
            'ok': True,
            'action': 'jobs.get',
            'job_id': job.get('job_id'),
            'job_action': job.get('action'),
            'status': job.get('status'),
            'created_at': job.get('created_at'),
            'started_at': job.get('started_at'),
            'finished_at': job.get('finished_at'),
            'requested_by': job.get('requested_by'),
            'attempts': int(job.get('attempts') or 0),
            'max_attempts': int(job.get('max_attempts') or 1),
            'warnings': job.get('warnings') or [],
        }
        if job.get('error'):
            payload['error'] = job['error']
            payload['ok'] = False
        if with_result and job.get('result') is not None:
            payload['result'] = job['result']
        return payload

    def _error_payload(self, exc: Exception) -> dict[str, Any]:
        return {'type': type(exc).__name__, 'message': str(exc)}

    def _is_retryable_exception(self, action: str, exc: Exception) -> bool:
        if not (action.startswith('weixin.fetch_') or action == 'weixin.batch_fetch' or action.startswith('internal.weixin.') or action.startswith('articles.ingest_')):
            return False
        if isinstance(exc, NON_RETRYABLE_EXCEPTIONS):
            return False
        module_name = type(exc).__module__.lower()
        type_name = type(exc).__name__.lower()
        message = str(exc).lower()
        if 'requests' in module_name or 'urllib3' in module_name:
            return True
        if 'timeout' in type_name or 'connection' in type_name:
            return True
        if 'fetcherror' in type_name:
            return True
        if isinstance(exc, (TimeoutError, ConnectionError, OSError, RuntimeError)):
            return True
        return any(keyword in message for keyword in RETRYABLE_KEYWORDS)

    def _retry_sleep(self, attempt: int) -> float:
        base = max(0.1, float(self.settings.retry_backoff_seconds))
        multiplier = max(1.0, float(self.settings.retry_backoff_multiplier))
        return round(base * (multiplier ** max(0, attempt - 1)), 3)

    def health(self) -> dict[str, Any]:
        self.start()
        worker_alive = bool(self._worker_thread and self._worker_thread.is_alive())
        return {
            'root': str(self.root),
            'worker_started': self._worker_started,
            'worker_alive': worker_alive,
            'queued_in_memory': len(self._queued_ids),
            'kb_dirty_count': len(self.kb_dirty_state()),
            'fetch_max_attempts': self.settings.fetch_max_attempts,
            'article_max_attempts': self.settings.article_max_attempts,
            'internal_max_attempts': self.settings.internal_max_attempts,
        }

    def _handle_retryable_result(self, job: dict[str, Any], result: dict[str, Any], attempt: int) -> tuple[bool, dict[str, Any] | None]:
        if bool(result.get('ok', True)):
            return False, result
        if not bool(result.get('retryable')):
            return False, result
        if attempt >= int(job.get('max_attempts') or 1):
            return False, result
        delay = self._retry_sleep(attempt)
        job.setdefault('warnings', []).append({
            'stage': 'job_retry',
            'attempt': attempt,
            'delay_seconds': delay,
            'message': result.get('message') or result.get('error') or '任务返回 retryable=false',
        })
        self._set_job(job)
        time.sleep(delay)
        return True, None

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._maybe_enqueue_due_kb_jobs()
                try:
                    job_id = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                self._dequeue_job_id(job_id)
                try:
                    job = self.get(job_id)
                except KeyError:
                    self._queue.task_done()
                    continue
                if job.get('status') == 'cancelled':
                    self._queue.task_done()
                    continue
                action = job.get('action') or ''
                handler = self._handlers.get(action)
                if handler is None:
                    job['status'] = 'failed'
                    job['finished_at'] = now_iso()
                    job['error'] = {'type': 'UnknownJobAction', 'message': f'No handler registered for {action}'}
                    self._set_job(job)
                    self._queue.task_done()
                    continue
                job['status'] = 'running'
                job['started_at'] = job.get('started_at') or now_iso()
                self._set_job(job)
                max_attempts = max(1, int(job.get('max_attempts') or 1))
                while True:
                    attempt = int(job.get('attempts') or 0) + 1
                    job['attempts'] = attempt
                    job['error'] = None
                    self._set_job(job)
                    try:
                        result = handler(job.get('payload') or {})
                        should_retry, final_result = self._handle_retryable_result(job, result, attempt)
                        if should_retry:
                            continue
                        result = final_result or result
                        job['result'] = result
                        job['status'] = 'completed' if bool(result.get('ok', True)) else 'failed'
                        job['warnings'] = (job.get('warnings') or []) + list(result.get('warnings') or [])
                        if action == 'internal.weixin.rebuild_kb' and job['status'] == 'completed':
                            slug = (job.get('payload') or {}).get('account_slug') or ''
                            self.clear_kb_dirty(slug)
                        break
                    except Exception as exc:  # noqa: BLE001
                        if self._is_retryable_exception(action, exc) and attempt < max_attempts:
                            delay = self._retry_sleep(attempt)
                            job.setdefault('warnings', []).append({
                                'stage': 'job_retry',
                                'attempt': attempt,
                                'delay_seconds': delay,
                                'error': type(exc).__name__,
                                'message': str(exc),
                            })
                            job['error'] = self._error_payload(exc)
                            self._set_job(job)
                            time.sleep(delay)
                            continue
                        job['status'] = 'failed'
                        job['error'] = self._error_payload(exc)
                        break
                job['finished_at'] = now_iso()
                self._set_job(job)
                self._queue.task_done()
            except Exception as exc:  # noqa: BLE001
                # 保底自恢复，避免 worker 因调度侧异常直接死亡。
                time.sleep(0.2)
                fallback = {
                    'job_id': f'worker_fault_{uuid.uuid4().hex[:8]}',
                    'stage': 'worker_loop',
                    'error': type(exc).__name__,
                    'message': str(exc),
                }
                self._write_json(self.state_dir / 'worker-last-error.json', fallback)
                continue

    def resource_read(self, job_id: str) -> dict[str, Any]:
        self.start()
        job = self.get(job_id)
        return {'contents': [{'uri': f'content-memory://jobs/{job_id}', 'mimeType': 'application/json', 'text': json.dumps(self._present_job(job, with_result=True), ensure_ascii=False, indent=2)}]}
