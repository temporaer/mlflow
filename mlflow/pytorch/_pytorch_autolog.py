import time
import mlflow
from mlflow.tracking import MlflowClient
from mlflow.entities import Param, Metric
from mlflow.tensorflow import (
    _flush_queue,
    _metric_queue,
    _metric_queue_lock,
    _thread_pool,
    _MAX_METRIC_QUEUE_SIZE,
)
import mlflow.pytorch._lightning_autolog as pl_autolog


def _add_to_queue(key, value, step, time, run_id):
    """
    Add a metric to the metric queue. Flush the queue if it exceeds the
    max queue size.
    """
    met = Metric(key=key, value=value, timestamp=time, step=step)
    with _metric_queue_lock:
        _metric_queue.append((run_id, met))
        if len(_metric_queue) > _MAX_METRIC_QUEUE_SIZE:
            _thread_pool.submit(_flush_queue)


FAILED = object()
autolog_run = None


def _get_run():
    global autolog_run
    if autolog_run is None:
        autolog_run = mlflow.active_run()
    if autolog_run is None:
        try:
            autolog_run = mlflow.start_run()
        except Exception:
            # don't try again.
            autolog_run = FAILED
    if autolog_run not in (None, FAILED):
        return autolog_run
    return None


def patched_add_hparams(original, self, hparam_dict, metric_dict, *args, **kwargs):
    """use a synchronous call here since this is going to get called very infrequently."""

    run = _get_run()

    if not pl_autolog.IN_FIT and run is not None and hparam_dict:
        run_id = run.info.run_id
        # str() is required by mlflow :(
        params_arr = [Param(key, str(value)) for key, value in hparam_dict.items()]
        metrics_arr = [
            Metric(key, value, int(time.time() * 1000), 0) for key, value in metric_dict.items()
        ]
        MlflowClient().log_batch(run_id=run_id, metrics=metrics_arr, params=params_arr, tags=[])

    original(self, hparam_dict, metric_dict, *args, **kwargs)


def patched_add_event(original, self, event, *args, mlflow_log_every_n_step, **kwargs):
    run = _get_run()
    if (
        not pl_autolog.IN_FIT
        and run is not None
        and event.WhichOneof("what") == "summary"
        and mlflow_log_every_n_step
    ):
        summary = event.summary
        global_step = args[0] if len(args) > 0 else kwargs.get("global_step", None)
        global_step = global_step or 0
        for v in summary.value:
            if v.HasField("simple_value"):
                if global_step % mlflow_log_every_n_step == 0:
                    _add_to_queue(
                        key=v.tag,
                        value=v.simple_value,
                        step=global_step,
                        time=int((event.wall_time or time.time()) * 1000),
                        run_id=run.info.run_id,
                    )

    return original(self, event, *args, **kwargs)


def patched_add_summary(original, self, *args, **kwargs):
    result = original(self, *args, **kwargs)
    _flush_queue()
    return result
