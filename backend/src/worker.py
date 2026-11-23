import json
import logging
import time
from datetime import timedelta
from pathlib import Path

from redis.exceptions import RedisError
from sqlalchemy import or_, select

from .calculation import estimate, estimate_warning
from .db import QUEUE, Session, WARNING_QUEUE, redis, utcnow
from .ledger import checksum, effective_points, inside, reading_hash, record
from .models import (
    BatchState, CalibrationRelease, ChamberRun, DryBatch, ReleaseState, VacuumWarning, WarningState,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
HEARTBEAT = Path('/tmp/worker-heartbeat')


def claim_fit(wakeup_id):
    with Session.begin() as db:
        eligible = or_(CalibrationRelease.state == ReleaseState.QUEUED,
                       (CalibrationRelease.state == ReleaseState.RUNNING) &
                       (CalibrationRelease.touched_at < utcnow() - timedelta(minutes=5)))
        query = select(CalibrationRelease).where(eligible)
        if wakeup_id is not None:
            query = query.where(CalibrationRelease.id == wakeup_id)
        release = db.scalar(query.order_by(CalibrationRelease.id).limit(1).with_for_update(skip_locked=True))
        if release is None:
            return None
        release.state = ReleaseState.RUNNING
        release.attempts += 1
        release.touched_at = utcnow()
        return 'fit', (release.id, release.attempts, release.snapshot, release.input_hash)


def claim_warning(wakeup_id):
    with Session.begin() as db:
        eligible = (VacuumWarning.state == WarningState.PENDING) | (
            (VacuumWarning.state == WarningState.RUNNING) &
            (VacuumWarning.touched_at < utcnow() - timedelta(minutes=5)))
        query = select(VacuumWarning).where(eligible)
        if wakeup_id is not None:
            query = query.where(VacuumWarning.id == wakeup_id)
        warning = db.scalar(query.order_by(VacuumWarning.id).limit(1).with_for_update(skip_locked=True))
        if warning is None:
            return None
        warning.state = WarningState.RUNNING
        warning.attempts += 1
        warning.touched_at = utcnow()
        return 'warning', (warning.id, warning.attempts, warning.snapshot, warning.input_hash)


def settle_fit(job, result, error):
    release_id, attempt, snapshot, digest = job
    with Session.begin() as db:
        batch = db.scalar(select(DryBatch).where(DryBatch.code == snapshot['batch']).with_for_update())
        entry = db.get(CalibrationRelease, release_id, with_for_update=True)
        if entry.attempts != attempt or entry.state != ReleaseState.RUNNING:
            return
        entry.touched_at = utcnow()
        if error is not None:
            entry.state = ReleaseState.FAILED
            entry.error = error[:240]
            record(db, 'worker', 'fit.failed', entry.error, batch.code)
            return
        evidence = Path(f'/evidence/fit-{release_id}.json')
        temp = evidence.with_suffix(f'.{attempt}.tmp')
        try:
            payload = {'input_hash': digest, 'snapshot': snapshot, 'result': result}
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            temp.replace(evidence)
        finally:
            temp.unlink(missing_ok=True)
        entry.result = result
        entry.prediction_pct = result['prediction_pct']
        within_window = inside(entry.prediction_pct, snapshot['window'])
        entry.state = ReleaseState.AWAITING if within_window else ReleaseState.RETEST
        batch.state = BatchState.REVIEW
        run = db.get(ChamberRun, entry.run_id)
        record(db, 'worker', 'fit.completed' if within_window else 'retest.opened',
               f'{run.source_name} · {entry.prediction_pct:.3f}% · 工艺窗 {snapshot["window"]}', batch.code)
        log.info('calibration %s %s', release_id, entry.state.value)


def settle_warning(job, result, error):
    warning_id, attempt, snapshot, digest = job
    with Session.begin() as db:
        batch = db.scalar(select(DryBatch).where(DryBatch.code == snapshot['batch']).with_for_update())
        warning = db.get(VacuumWarning, warning_id, with_for_update=True)
        if warning.attempts != attempt or warning.state != WarningState.RUNNING:
            # A newer backfill already superseded this score; never show stale readings.
            return
        warning.touched_at = utcnow()
        if error is not None:
            warning.state = WarningState.FAILED
            warning.error = error[:240]
            record(db, 'worker', 'vacuum.warning.failed', warning.error, batch.code)
            return
        run = db.get(ChamberRun, warning.run_id)
        if reading_hash(effective_points(db, run)) != snapshot['readings_hash']:
            # A newer backfill landed while this score ran; drop it instead of showing stale readings.
            warning.state = WarningState.SUPERSEDED
            record(db, 'worker', 'vacuum.warning.stale',
                   f'{run.source_name} · 读数已更新，本次结果不展示', batch.code)
            return
        evidence = Path(f'/evidence/vacuum-warning-{warning_id}.json')
        temp = evidence.with_suffix(f'.{attempt}.tmp')
        try:
            payload = {'readings_hash': snapshot['readings_hash'], 'input_hash': digest, 'snapshot': snapshot, 'result': result}
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            temp.replace(evidence)
        except OSError:
            # Evidence storage is best-effort; the score still has to land in the database.
            log.exception('vacuum warning evidence could not be written')
        finally:
            temp.unlink(missing_ok=True)
        warning.result = result
        warning.prediction_pct = result['prediction_pct']
        warning.flagged = result['flagged']
        warning.state = WarningState.READY
        record(db, 'worker', 'vacuum.warning.flagged' if result['flagged'] else 'vacuum.warning.clear',
               f'{run.source_name} · 首小时 {result["prediction_pct"]:.3f}% · 工艺窗 {result["window"]}', batch.code)
        log.info('vacuum warning %s %s', warning_id, 'flagged' if result['flagged'] else 'clear')


SETTLERS = {'fit': settle_fit, 'warning': settle_warning}
ESTIMATORS = {'fit': estimate, 'warning': estimate_warning}


def process(job, kind):
    _, snapshot, digest = job
    try:
        if checksum(snapshot) != digest:
            raise ValueError('试算输入快照校验失败')
        result = ESTIMATORS[kind](snapshot)
    except Exception as exc:
        log.exception('%s computation failed', kind)
        SETTLERS[kind](job, None, str(exc))
    else:
        try:
            SETTLERS[kind](job, result, None)
        except OSError:
            log.exception('%s evidence could not be written', kind)
            SETTLERS[kind](job, None, '证据卷暂不可写，可按原快照重试')


def next_job(entry):
    if entry:
        queue, raw = entry
        name = queue.decode() if isinstance(queue, bytes) else queue
        wakeup_id = int(raw)
        job = claim_warning(wakeup_id) if name == WARNING_QUEUE else claim_fit(wakeup_id)
        if job is not None:
            return job
    return claim_fit(None) or claim_warning(None)


def main():
    while True:
        try:
            try:
                entry = redis.blpop([QUEUE, WARNING_QUEUE], timeout=2)
            except RedisError:
                log.warning('Redis unavailable; checking committed calibration queue')
                time.sleep(2)
                entry = None
            job = next_job(entry)
            if job:
                kind, payload = job
                process(payload, kind)
            HEARTBEAT.touch()
        except Exception:
            log.exception('worker iteration failed')
            time.sleep(3)


if __name__ == '__main__':
    main()
