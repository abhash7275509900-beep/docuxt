"""Cosine similarity using numpy."""
import numpy as np


def cosine_similarity(X, Y=None):
    X_arr = np.asarray(X, dtype=np.float64)
    if X_arr.ndim == 1:
        X_arr = X_arr.reshape(1, -1)
    if Y is None:
        Y_arr = X_arr
    else:
        Y_arr = np.asarray(Y, dtype=np.float64)
        if Y_arr.ndim == 1:
            Y_arr = Y_arr.reshape(1, -1)

    x_norms = np.linalg.norm(X_arr, axis=1, keepdims=True)
    x_norms[x_norms == 0.0] = 1.0
    y_norms = np.linalg.norm(Y_arr, axis=1, keepdims=True)
    y_norms[y_norms == 0.0] = 1.0

    X_unit = X_arr / x_norms
    Y_unit = Y_arr / y_norms
    return np.dot(X_unit, Y_unit.T)
