"""Fit the calibration without allowing target samples into reference training."""
import math

import numpy as np
from sklearn.linear_model import Ridge
from sklearn.preprocessing import PowerTransformer

FEATURES = ['固形物 %', '投料厚度 / 标称厚度', '真空段跨度 min', '真空段平均压力 Pa', '真空段平均板温 °C']
WARNING_FEATURES = ['固形物 %', '投料厚度 / 标称厚度', '首小时真空段跨度 min', '首小时平均压力 Pa', '首小时平均板温 °C']
ALPHA = 0.65


def ridge_fit(x, y, target, names=FEATURES):
    active = np.ptp(x, axis=0) > 1e-9
    if not active.any():
        return float(np.mean(y)), {}, {}
    variable = x[:, active]
    transformer = PowerTransformer(method='yeo-johnson', standardize=True)
    transformed = transformer.fit_transform(variable)
    model = Ridge(alpha=ALPHA, solver='svd').fit(transformed, y)
    prediction = model.predict(transformer.transform(target[None, active]))[0]
    enabled = [name for name, flag in zip(names, active, strict=True) if flag]
    return float(prediction), dict(zip(enabled, map(float, transformer.lambdas_))), dict(zip(enabled, map(float, model.coef_)))


def fit_matrix(training, target):
    x = np.asarray([item['features'] for item in training], dtype=float)
    y = np.asarray([item['observed'] for item in training], dtype=float)
    point = np.asarray(target, dtype=float)
    if not np.any(np.ptp(x, axis=0) > 1e-9):
        raise ValueError('参考批次的工况特征缺少变化，无法拟合校准模型')
    return x, y, point


def leave_one_out(x, y, names):
    residuals = []
    for index in range(len(y)):
        keep = np.arange(len(y)) != index
        prediction, _, _ = ridge_fit(x[keep], y[keep], x[index], names)
        residuals.append((prediction - y[index]) ** 2)
    return residuals


def estimate(snapshot):
    training = snapshot['training']
    x, y, target = fit_matrix(training, snapshot['features'])
    residuals = leave_one_out(x, y, FEATURES)
    prediction, lambdas, coefficients = ridge_fit(x, y, target, FEATURES)
    if not all(math.isfinite(value) for value in [prediction, *residuals, *lambdas.values(), *coefficients.values()]):
        raise ValueError('模型产生非有限值，请检查参考工况的分布')
    return {
        'prediction_pct': round(prediction, 6), 'loo_rmse': round(float(np.sqrt(np.mean(residuals))), 6),
        'n_references': len(training), 'alpha': ALPHA, 'seed': 1062, 'solver': 'svd',
        'lambdas': {name: round(value, 10) for name, value in lambdas.items()},
        'coefficients': {name: round(value, 10) for name, value in coefficients.items()},
        'feature_names': FEATURES,
    }


def estimate_warning(snapshot):
    """Score the first vacuum hour against references scored over their own first hour."""
    training = snapshot['training']
    x, y, target = fit_matrix(training, snapshot['features'])
    residuals = leave_one_out(x, y, WARNING_FEATURES)
    prediction, lambdas, coefficients = ridge_fit(x, y, target, WARNING_FEATURES)
    if not all(math.isfinite(value) for value in [prediction, *residuals, *lambdas.values(), *coefficients.values()]):
        raise ValueError('模型产生非有限值，请检查参考工况的分布')
    low, high = snapshot['window']
    flagged = not (low <= prediction <= high)
    return {
        'prediction_pct': round(prediction, 6), 'flagged': flagged, 'inside_window': not flagged,
        'window': [low, high], 'first_hour_minutes': snapshot['first_hour_minutes'],
        'loo_rmse': round(float(np.sqrt(np.mean(residuals))), 6),
        'n_references': len(training), 'alpha': ALPHA, 'seed': 1062, 'solver': 'svd',
        'lambdas': {name: round(value, 10) for name, value in lambdas.items()},
        'coefficients': {name: round(value, 10) for name, value in coefficients.items()},
        'feature_names': WARNING_FEATURES,
    }
