import hashlib
import json
import logging
from datetime import datetime, timezone

from fastapi import HTTPException
from redis.exceptions import RedisError
from sqlalchemy import inspect, select

from .db import QUEUE, WARNING_QUEUE, redis, utcnow
from .models import (
    BatchState, CalibrationRelease, ChamberRun, DryBatch, LabEvent, LateReading, Phase, RecipeLot,
    ReleaseState, RunState, ShrinkageSample, Stage, VacuumSegment, VacuumWarning, WarningState,
)

log = logging.getLogger(__name__)
FIRST_HOUR_MINUTES = 60


def checksum(data):
    normalized = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(normalized.encode()).hexdigest()


def record(db, actor, action, detail, code=None):
    db.add(LabEvent(batch_code=code, actor=actor, action=action, detail=detail))


def blocked(db, actor, action, detail, code):
    record(db, actor, f'{action}.blocked', detail, code)
    db.commit()
    raise HTTPException(409, detail)


def row(model):
    result = {}
    for col in inspect(type(model)).column_attrs:
        value = getattr(model, col.key)
        if isinstance(value, datetime):
            value = value.replace(tzinfo=timezone.utc).isoformat()
        result[col.key] = value
    return result


def locked_batch(db, code):
    batch = db.scalar(select(DryBatch).where(DryBatch.code == code).with_for_update())
    if batch is None:
        raise HTTPException(404, '没有找到该冻干批次')
    return batch


def latest_run(db, code):
    return db.scalar(select(ChamberRun).filter_by(batch_code=code).order_by(ChamberRun.id.desc()).limit(1))


def release_for(db, run_id):
    return db.scalar(select(CalibrationRelease).filter_by(run_id=run_id))


def samples_for(db, run_id, *, retest=False):
    return db.scalars(select(ShrinkageSample).filter_by(run_id=run_id, is_retest=retest)
                      .order_by(ShrinkageSample.specimen, ShrinkageSample.id)).all()


def vacuum_points(points):
    return [point for point in points if point['phase'] == Phase.VACUUM]


def curve_issue(points):
    vacuum = vacuum_points(points)
    if len(vacuum) < 2:
        return '曲线缺少完整真空段，至少补录两个真空采集点'
    phases = [point['phase'] for point in points]
    order = {Phase.FREEZE: 0, Phase.VACUUM: 1, Phase.FINISH: 2}
    if phases[0] != Phase.FREEZE or phases[-1] != Phase.FINISH or phases != sorted(phases, key=order.get):
        return '曲线阶段应按预冻、抽真空、解析干燥顺序采集'
    return None


def training_run(db, batch):
    run = latest_run(db, batch.code)
    if not run:
        raise ValueError('批次缺少设备曲线，请先导入设备记录')
    issue = curve_issue(run.points)
    if issue:
        raise ValueError(issue)
    if run.state != RunState.FROZEN:
        raise ValueError('设备曲线尚未冻结，试算与放行必须引用冻结版本')
    return run


def paired_measurements(db, run):
    pairs = {}
    for sample in samples_for(db, run.id):
        pairs.setdefault(sample.specimen, {})[sample.stage] = round(sample.thickness_mm, 5)
    if len(pairs) < 2 or any(len(pair) != 2 for pair in pairs.values()):
        raise ValueError('样本阶段不足，至少两个试片且每个均须配齐冻干前与冻干后厚度')
    if any(pair[Stage.POST] > pair[Stage.PRE] for pair in pairs.values()):
        raise ValueError('样本阶段读数异常，请核查冻干后厚度大于前厚度的试片')
    return pairs


def add_curve(db, batch, payload, actor):
    previous = latest_run(db, batch.code)
    if previous:
        release = release_for(db, previous.id)
        if release and release.state != ReleaseState.HELD:
            blocked(db, actor, 'curve.import', '当前校准尚未结案，复测留置后才能导入新周期', batch.code)
    if batch.state in (BatchState.RELEASED, BatchState.REFERENCE):
        blocked(db, actor, 'curve.import', '已放行或归档的批次保留原曲线', batch.code)
    if db.scalar(select(ChamberRun.id).filter_by(batch_code=batch.code, source_name=payload['source_name'])):
        blocked(db, actor, 'curve.import', '同名曲线已存在，请用新文件名保留修订历史', batch.code)
    run = ChamberRun(batch_code=batch.code, **payload)
    db.add(run)
    batch.state = BatchState.COLLECTING
    record(db, actor, 'curve.import', run.source_name, batch.code)
    db.flush()
    return run


def freeze_curve(db, batch, run, actor):
    if run.state != RunState.DRAFT or latest_run(db, batch.code).id != run.id:
        blocked(db, actor, 'curve.freeze', '仅当前待采曲线可以冻结', batch.code)
    issue = curve_issue(run.points)
    if issue:
        blocked(db, actor, 'curve.freeze', issue, batch.code)
    run.state = RunState.FROZEN
    run.frozen_at = utcnow()
    run.checksum = checksum(run.points)
    record(db, actor, 'curve.freeze', f'{run.source_name} · {run.checksum}', batch.code)
    # First-hour warning starts as soon as the frozen curve already covers one vacuum hour.
    warning, _ = queue_warning(db, batch, run, actor)
    return warning


def add_sample(db, batch, run, payload, actor):
    if batch.state != BatchState.COLLECTING or latest_run(db, batch.code).id != run.id or run.state != RunState.FROZEN:
        blocked(db, actor, 'sample.record', '仅采集中当前冻结曲线接受常规阶段测量', batch.code)
    matches = {item.stage: item.thickness_mm for item in samples_for(db, run.id) if item.specimen == payload['specimen']}
    if payload['stage'] in matches:
        blocked(db, actor, 'sample.record', '同一曲线、试片和阶段已有测量', batch.code)
    matches[payload['stage']] = payload['thickness_mm']
    if len(matches) == 2 and matches[Stage.POST] > matches[Stage.PRE]:
        blocked(db, actor, 'sample.record', '冻干后厚度不能大于冻干前读数，请核对试片', batch.code)
    sample = ShrinkageSample(run_id=run.id, technician=actor, **payload)
    db.add(sample)
    record(db, actor, 'sample.record', f'{sample.specimen} · {sample.stage} · {sample.thickness_mm:g} mm', batch.code)
    db.flush()
    return sample


def archive_reference(db, batch, actor, note):
    if batch.state != BatchState.COLLECTING:
        blocked(db, actor, 'reference.archive', '只有采集状态可以认证为历史标定批次', batch.code)
    try:
        pairs = paired_measurements(db, training_run(db, batch))
    except ValueError as exc:
        blocked(db, actor, 'reference.archive', str(exc), batch.code)
    batch.state = BatchState.REFERENCE
    record(db, actor, 'reference.archive', f'{len(pairs)} 组配对 · {note}', batch.code)


def feature_row(batch, recipe, run, points=None):
    vacuum = vacuum_points(points if points is not None else run.points)
    return [
        round(recipe.solids_pct, 5), round(batch.feed_mm / recipe.nominal_mm, 6),
        vacuum[-1]['minute'] - vacuum[0]['minute'],
        round(sum(point['pressure_pa'] for point in vacuum) / len(vacuum), 6),
        round(sum(point['shelf_c'] for point in vacuum) / len(vacuum), 6),
    ]


def observed(pairs):
    return round(sum(100 * (1 - pair[Stage.POST] / pair[Stage.PRE]) for pair in pairs.values()) / len(pairs), 6)


def queue_fit(db, batch, actor):
    if batch.state != BatchState.COLLECTING:
        blocked(db, actor, 'fit.queue', '该批次当前状态不可提交新试算', batch.code)
    recipe = db.get(RecipeLot, batch.recipe_code)
    try:
        run = training_run(db, batch)
        target_pairs = paired_measurements(db, run)
    except ValueError as exc:
        blocked(db, actor, 'fit.queue', str(exc), batch.code)
    reference = db.scalars(select(DryBatch).filter_by(recipe_code=recipe.code, state=BatchState.REFERENCE)
                           .order_by(DryBatch.code)).all()
    if len(reference) < 3:
        blocked(db, actor, 'fit.queue', '至少需要三个同配方的已认证标定参考批次', batch.code)
    training = []
    for historical in reference:
        archived = latest_run(db, historical.code)
        pairs = paired_measurements(db, archived)
        training.append({
            'batch': historical.code, 'curve_hash': archived.checksum,
            'features': feature_row(historical, recipe, archived, effective_points(db, archived)),
            'observed': observed(pairs), 'pairs': pairs,
        })
    feature_columns = zip(*(item['features'] for item in training), strict=True)
    if not any(max(column) - min(column) > 1e-9 for column in feature_columns):
        blocked(db, actor, 'fit.queue', '参考批次工况一致，请认证有工况变化的同配方历史批次', batch.code)
    target = effective_points(db, run)
    snapshot = {
        'batch': batch.code, 'recipe': recipe.code, 'curve_hash': run.checksum,
        'readings_revision': run.readings_revision,
        'features': feature_row(batch, recipe, run, target), 'sample_pairs': target_pairs,
        'window': [round(recipe.window_low, 5), round(recipe.window_high, 5)], 'training': training,
    }
    release = CalibrationRelease(run_id=run.id, requested_by=actor, input_hash=checksum(snapshot), snapshot=snapshot)
    db.add(release)
    batch.state = BatchState.CALIBRATING
    record(db, actor, 'fit.queue', f'{run.source_name} · {release.input_hash}', batch.code)
    db.flush()
    return release


def wake_worker(release_id):
    try:
        redis.rpush(QUEUE, release_id)
    except RedisError:
        log.exception('Redis wake-up delayed; worker will scan committed calibrations')


def retry_fit(db, batch, release, actor):
    if release.state != ReleaseState.FAILED:
        blocked(db, actor, 'fit.retry', '仅计算失败的试算可按原输入重试', batch.code)
    release.state = ReleaseState.QUEUED
    release.error = None
    release.touched_at = utcnow()
    record(db, actor, 'fit.retry', f'以原输入 {release.input_hash} 重新计算', batch.code)


def inside(value, window):
    return window[0] <= value <= window[1]


def measure_retest(db, batch, run, release, payload, actor):
    if release.state not in (ReleaseState.AWAITING, ReleaseState.RETEST):
        blocked(db, actor, 'retest.record', '当前校准状态不接受复测', batch.code)
    if any(item.specimen == payload.specimen for item in samples_for(db, run.id)):
        blocked(db, actor, 'retest.record', '复测须取独立试片，试片编号不能与首测重复', batch.code)
    db.add_all([
        ShrinkageSample(run_id=run.id, technician=actor, specimen=payload.specimen, stage=stage,
                        thickness_mm=reading, is_retest=True)
        for stage, reading in [(Stage.PRE, payload.before_mm), (Stage.POST, payload.after_mm)]
    ])
    release.recheck_pct = round(100 * (1 - payload.after_mm / payload.before_mm), 6)
    release.recheck_note = payload.note
    acceptable = release.state == ReleaseState.AWAITING and inside(release.recheck_pct, release.snapshot['window'])
    release.state = ReleaseState.VERIFIED if acceptable else ReleaseState.HELD
    release.touched_at = utcnow()
    action = 'retest.verified' if acceptable else 'retest.held'
    record(db, actor, action, f'{payload.specimen} · {release.recheck_pct:.2f}% · {payload.note}', batch.code)


def sign_release(db, batch, run, release, actor, note):
    if release.state != ReleaseState.VERIFIED or run.state != RunState.FROZEN or latest_run(db, batch.code).id != run.id:
        blocked(db, actor, 'release.sign', '仅窗内且已复测确认的当前冻结曲线允许签发', batch.code)
    release.state = ReleaseState.RELEASED
    release.signed_at = utcnow()
    release.signed_by = actor
    release.release_note = note
    release.touched_at = release.signed_at
    batch.state = BatchState.RELEASED
    record(db, actor, 'release.sign', f'{run.source_name} · {note}', batch.code)


# --- 抽真空首小时超窗预警 -------------------------------------------------

def ordered_points(points):
    order = {Phase.FREEZE: 0, Phase.VACUUM: 1, Phase.FINISH: 2}
    return sorted(points, key=lambda point: (point['minute'], order.get(point['phase'], 9)))


def reading_point(reading):
    return {'minute': reading.minute, 'phase': reading.phase.value,
            'shelf_c': reading.shelf_c, 'pressure_pa': reading.pressure_pa}


def late_readings_for(db, run_id, *, after_seal=None):
    query = select(LateReading).filter_by(run_id=run_id)
    if after_seal is not None:
        query = query.filter_by(after_seal=after_seal)
    return db.scalars(query.order_by(LateReading.minute)).all()


def reading_origin(db, run_id):
    """Map minute -> segment for readings that arrived through pre-seal backfill."""
    return {reading.minute: reading.segment for reading in late_readings_for(db, run_id, after_seal=False)}


def effective_points(db, run):
    """Frozen curve plus pre-seal backfill. The frozen curve itself stays untouched."""
    readings = {}
    for point in run.points:
        readings[(point['minute'], point['phase'])] = dict(point)
    for reading in late_readings_for(db, run.id, after_seal=False):
        readings[(reading.minute, reading.phase.value)] = reading_point(reading)
    return ordered_points(list(readings.values()))


def first_hour(points):
    """Vacuum readings up to one hour after the vacuum segment starts."""
    vacuum = vacuum_points(points)
    if not vacuum:
        return []
    start = vacuum[0]['minute']
    return [point for point in vacuum if point['minute'] <= start + FIRST_HOUR_MINUTES]


def vacuum_elapsed(points):
    vacuum = vacuum_points(points)
    if not vacuum:
        return 0
    return vacuum[-1]['minute'] - vacuum[0]['minute']


def first_hour_features(batch, recipe, points):
    hour = first_hour(points)
    return [
        round(recipe.solids_pct, 5), round(batch.feed_mm / recipe.nominal_mm, 6),
        hour[-1]['minute'] - hour[0]['minute'],
        round(sum(point['pressure_pa'] for point in hour) / len(hour), 6),
        round(sum(point['shelf_c'] for point in hour) / len(hour), 6),
    ]


def reading_hash(points):
    return checksum([{key: point[key] for key in ('minute', 'phase', 'shelf_c', 'pressure_pa')}
                     for point in ordered_points(points)])


def warning_for(db, run_id, readings_hash):
    return db.scalar(select(VacuumWarning).filter_by(run_id=run_id, readings_hash=readings_hash))


def latest_warning(db, run_id):
    return db.scalar(select(VacuumWarning).filter_by(run_id=run_id).order_by(VacuumWarning.id.desc()).limit(1))


def supersede_warnings(db, run_id, *, keep=None):
    query = select(VacuumWarning).where(VacuumWarning.run_id == run_id,
                                        VacuumWarning.state != WarningState.SUPERSEDED)
    if keep is not None:
        query = query.where(VacuumWarning.id != keep)
    stale = db.scalars(query).all()
    for warning in stale:
        warning.state = WarningState.SUPERSEDED
        warning.touched_at = utcnow()
    return len(stale)


def warning_readiness(points, run):
    if run is None or run.state != RunState.FROZEN:
        return '设备曲线尚未冻结，抽真空首小时预警需要引用冻结版本'
    if not vacuum_points(points):
        return '曲线缺少抽真空读数，无法计算首小时预警'
    if vacuum_elapsed(points) < FIRST_HOUR_MINUTES:
        return f'抽真空尚未满 {FIRST_HOUR_MINUTES} min，暂不计算首小时预警'
    return None


def training_rows(db, recipe):
    reference = db.scalars(select(DryBatch).filter_by(recipe_code=recipe.code, state=BatchState.REFERENCE)
                           .order_by(DryBatch.code)).all()
    training = []
    for historical in reference:
        archived = latest_run(db, historical.code)
        points = effective_points(db, archived) if archived else []
        if archived is None or archived.state != RunState.FROZEN or vacuum_elapsed(points) < FIRST_HOUR_MINUTES:
            continue
        try:
            pairs = paired_measurements(db, archived)
        except ValueError:
            continue
        training.append({
            'batch': historical.code, 'curve_hash': archived.checksum,
            'features': first_hour_features(historical, recipe, points), 'observed': observed(pairs),
        })
    return training


def queue_warning(db, batch, run, actor):
    """Score the first vacuum hour from the newest readings and queue the worker."""
    points = effective_points(db, run)
    issue = warning_readiness(points, run)
    if issue:
        return None, issue
    recipe = db.get(RecipeLot, batch.recipe_code)
    digest = reading_hash(points)
    existing = warning_for(db, run.id, digest)
    if existing is not None:
        supersede_warnings(db, run.id, keep=existing.id)
        if existing.state in (WarningState.PENDING, WarningState.RUNNING, WarningState.READY):
            return existing, None
        existing.state = WarningState.PENDING
        existing.error = None
        existing.touched_at = utcnow()
        record(db, actor, 'vacuum.warning.queue', f'{run.source_name} · 按原读数重算 · {digest[:12]}', batch.code)
        db.flush()
        return existing, None
    training = training_rows(db, recipe)
    if len(training) < 3:
        return None, '至少需要三个同配方且抽真空满一小时的可比标定参考批次'
    columns = zip(*(item['features'] for item in training), strict=True)
    if not any(max(column) - min(column) > 1e-9 for column in columns):
        return None, '参考批次首小时工况一致，请补充有工况变化的同配方历史批次'
    prior = db.scalar(select(VacuumWarning.id).filter_by(run_id=run.id).limit(1))
    snapshot = {
        'batch': batch.code, 'recipe': recipe.code, 'run_id': run.id, 'curve_hash': run.checksum,
        'readings_hash': digest, 'readings_revision': run.readings_revision, 'recomputed': prior is not None,
        'first_hour_minutes': FIRST_HOUR_MINUTES, 'first_hour': first_hour(points),
        'features': first_hour_features(batch, recipe, points),
        'window': [round(recipe.window_low, 5), round(recipe.window_high, 5)],
        'training': training,
    }
    supersede_warnings(db, run.id)
    warning = VacuumWarning(run_id=run.id, readings_hash=digest, requested_by=actor,
                            input_hash=checksum(snapshot), snapshot=snapshot)
    db.add(warning)
    record(db, actor, 'vacuum.warning.queue',
           f'{run.source_name} · 读数版本 {run.readings_revision} · {digest[:12]}', batch.code)
    db.flush()
    return warning, None


def wake_warning(warning_id):
    try:
        redis.rpush(WARNING_QUEUE, warning_id)
    except RedisError:
        log.exception('Redis wake-up delayed; worker will scan committed warnings')


def request_warning(db, batch, actor):
    run = latest_run(db, batch.code)
    if run is None:
        blocked(db, actor, 'vacuum.warning.queue', '批次缺少设备曲线，请先导入设备记录', batch.code)
    warning, issue = queue_warning(db, batch, run, actor)
    if warning is None:
        blocked(db, actor, 'vacuum.warning.queue', issue, batch.code)
    return warning


def _conflicting_reading(points, incoming):
    known = {(point['minute'], point['phase']): point for point in points}
    for point in incoming:
        original = known.get((point['minute'], point['phase']))
        if original and (original['shelf_c'], original['pressure_pa']) != (point['shelf_c'], point['pressure_pa']):
            return point['minute']
    return None


def _phase_value(phase):
    return phase if isinstance(phase, str) else phase.value


def receive_vacuum_segment(db, batch, run, payload, actor):
    """Accept one offline backfill chunk. Retrying the same segment adds nothing."""
    if latest_run(db, batch.code).id != run.id:
        blocked(db, actor, 'vacuum.backfill', '仅当前设备周期接收真空分段补传', batch.code)
    if run.state != RunState.FROZEN:
        blocked(db, actor, 'vacuum.backfill', '请先冻结设备曲线再补传真空分段', batch.code)
    data = payload.model_dump() if hasattr(payload, 'model_dump') else payload
    segment_key = str(data['segment']).strip()
    incoming = [{'minute': point['minute'], 'phase': _phase_value(point['phase']),
                 'shelf_c': float(point['shelf_c']), 'pressure_pa': float(point['pressure_pa'])}
                for point in data['points']]
    digest = checksum(incoming)
    segment = db.scalar(select(VacuumSegment).filter_by(run_id=run.id, segment=segment_key))
    if segment is not None:
        if segment.content_hash != digest:
            blocked(db, actor, 'vacuum.backfill', '同一分段已接收不同读数，请改用新分段标识', batch.code)
        return segment, 'duplicate', None
    if run.sealed_at is not None:
        stored = VacuumSegment(run_id=run.id, segment=segment_key, content_hash=digest, readings=incoming,
                               reading_count=len(incoming), sealed=True, received_by=actor)
        db.add(stored)
        for point in incoming:
            db.add(LateReading(run_id=run.id, segment=segment_key, after_seal=True, received_by=actor, **point))
        record(db, actor, 'vacuum.backfill.sealed',
               f'{segment_key} · {len(incoming)} 点另存，冻结曲线与放行不变', batch.code)
        db.flush()
        return stored, 'sealed', None
    conflict = _conflicting_reading(effective_points(db, run), incoming)
    if conflict is not None:
        blocked(db, actor, 'vacuum.backfill', f'第 {conflict} min 的补传读数与冻结曲线冲突，请核对设备导出', batch.code)
    known = {(point['minute'], point['phase']) for point in effective_points(db, run)}
    added = [point for point in incoming if (point['minute'], point['phase']) not in known]
    stored = VacuumSegment(run_id=run.id, segment=segment_key, content_hash=digest, readings=incoming,
                           reading_count=len(incoming), sealed=False, received_by=actor)
    db.add(stored)
    for point in added:
        db.add(LateReading(run_id=run.id, segment=segment_key, after_seal=False, received_by=actor, **point))
    if not added:
        record(db, actor, 'vacuum.backfill', f'{segment_key} · 无新增读数，预警保持不变', batch.code)
        db.flush()
        return stored, 'unchanged', None
    run.readings_revision += 1
    record(db, actor, 'vacuum.backfill',
           f'{segment_key} · {len(added)} 点并入原炉 · 读数版本 {run.readings_revision}', batch.code)
    db.flush()
    warning, _ = queue_warning(db, batch, run, actor)
    return stored, 'merged', warning


def seal_run(db, batch, run, actor):
    if latest_run(db, batch.code).id != run.id:
        blocked(db, actor, 'chamber.seal', '仅当前设备周期可以封炉', batch.code)
    if run.state != RunState.FROZEN:
        blocked(db, actor, 'chamber.seal', '冻结曲线后才能封炉', batch.code)
    if run.sealed_at is not None:
        blocked(db, actor, 'chamber.seal', '该设备周期已封炉，后续补传读数另存', batch.code)
    run.sealed_at = utcnow()
    record(db, actor, 'chamber.seal', f'{run.source_name} · 冻结曲线与放行沿用读数版本 {run.readings_revision}', batch.code)


def warning_view(db, run):
    """Show only the warning scored from the run's newest readings; otherwise mark it stale."""
    if run is None:
        return None, '批次缺少设备曲线'
    latest = latest_warning(db, run.id)
    points = effective_points(db, run)
    current = reading_hash(points)
    if latest is None:
        if run.state != RunState.FROZEN:
            return None, '设备曲线尚未冻结，抽真空满一小时后可看首小时预警'
        if vacuum_elapsed(points) < FIRST_HOUR_MINUTES:
            return None, f'抽真空未满 {FIRST_HOUR_MINUTES} min'
        return None, '尚未计算首小时预警'
    if latest.state == WarningState.SUPERSEDED or latest.readings_hash != current:
        return {'id': latest.id, 'state': 'stale', 'pending': True, 'stale': True,
                'message': '读数已更新，预警待更新', 'readings_revision': run.readings_revision,
                'window': latest.snapshot['window'], 'superseded_id': latest.id}, None
    base = {'id': latest.id, 'readings_revision': run.readings_revision, 'readings_hash': latest.readings_hash,
            'window': latest.snapshot['window'], 'first_hour_minutes': latest.snapshot['first_hour_minutes'],
            'readings': latest.snapshot['first_hour'], 'recomputed': latest.snapshot.get('recomputed', False)}
    if latest.state in (WarningState.PENDING, WarningState.RUNNING):
        message = '读数已更新，预警待更新' if latest.snapshot.get('recomputed') else '首小时预警计算中'
        return base | {'state': latest.state, 'pending': True, 'stale': False, 'message': message}, None
    if latest.state == WarningState.FAILED:
        return base | {'state': latest.state, 'pending': False, 'stale': False, 'message': latest.error}, None
    return base | {
        'state': latest.state, 'pending': False, 'stale': False, 'flagged': latest.flagged,
        'prediction_pct': latest.prediction_pct,
        'message': '首小时预测超出工艺窗，请提前关注' if latest.flagged else '首小时预测处于工艺窗',
        'result': latest.result,
    }, None
